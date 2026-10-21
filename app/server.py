"""HTTP API and the test UI.

The API key comes from the X-API-Key header (the UI field) or, if absent, from the
ANTHROPIC_API_KEY environment variable. It is never logged or stored server side.
Set SOP_LLM=fake to run with the deterministic FakeLLM (no key needed).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Literal

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import llm as llm_mod
from . import tts
from .agent import Agent, state
from .llm import AnthropicLLM, ConfigError, FakeLLM, OpenAILLM, model_names
from .models import Session
from .store import DataStore

load_dotenv()

STATIC = Path(__file__).parent.parent / "static"

app = FastAPI(title="Insurance SOP agent")
store = DataStore()
outbox: list[dict] = []  # append-only mock email outbox
agent = Agent(store, outbox)
sessions: OrderedDict[str, Session] = OrderedDict()
locks: dict[str, threading.Lock] = {}
registry_lock = threading.Lock()
MAX_SESSIONS = 500  # in-memory demo store: evict the least recently used beyond this
log = logging.getLogger("sop-agent")


class ChatIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=100)
    message: str = Field(..., min_length=1, max_length=2000)
    channel: Literal["text", "voice"] = "text"
    heard: str | None = Field(None, max_length=2000)  # reply text the caller heard before cutting in


class TtsIn(BaseModel):
    text: str = Field("")


class ResetIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=100)


def _session(session_id: str, create: bool = True) -> tuple[Session, threading.Lock]:
    with registry_lock:
        if session_id not in sessions:
            if not create:
                raise HTTPException(404, "Unknown session.")
            sessions[session_id] = Session(session_id)
            locks[session_id] = threading.Lock()
            while len(sessions) > MAX_SESSIONS:
                old_id, _ = sessions.popitem(last=False)
                locks.pop(old_id, None)
        sessions.move_to_end(session_id)
        return sessions[session_id], locks[session_id]


def _provider() -> str:
    if os.environ.get("SOP_LLM") == "fake":
        return "fake"
    return os.environ.get("CLAIMS_PROVIDER", "anthropic").strip().lower() or "anthropic"


def _llm(api_key: str | None):
    provider = _provider()
    if provider == "fake":
        return FakeLLM()
    if provider not in ("anthropic", "openai"):
        raise HTTPException(500, "CLAIMS_PROVIDER must be anthropic, openai or fake.")
    env = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    key = (api_key or "").strip() or os.environ.get(env, "").strip()
    if not key:
        raise HTTPException(
            401,
            f"No API key. Paste a {provider} API key into the UI, send it as the X-API-Key "
            f"header, or set {env} before starting the server.",
        )
    try:
        return OpenAILLM(key) if provider == "openai" else AnthropicLLM(key)
    except ConfigError as e:
        raise HTTPException(500, str(e))


# llm.ProviderError wraps SDK errors (agent H); imported defensively until it exists.
_ProviderError = getattr(llm_mod, "ProviderError", None) or type("_NoProviderError", (Exception,), {})


@app.post("/api/chat")
def chat(body: ChatIn, x_api_key: str | None = Header(default=None)):
    llm = _llm(x_api_key)
    s, lock = _session(body.session_id)
    with lock:
        before = copy.deepcopy(s)
        try:
            return agent.handle(s, body.message.strip(), llm, channel=body.channel, heard=body.heard)
        except anthropic.APIError as e:
            sessions[body.session_id] = before  # a failed turn leaves no trace
            if isinstance(e, anthropic.AuthenticationError):
                raise HTTPException(401, "The API key was rejected by the model provider.")
            if isinstance(e, anthropic.RateLimitError):
                raise HTTPException(429, "The model provider is rate limiting requests. Try again shortly.")
            raise HTTPException(502, f"Model provider error ({type(e).__name__}). Please resend.")
        except _ProviderError as e:
            sessions[body.session_id] = before
            status = getattr(e, "status", 502)
            if status == 401:
                raise HTTPException(401, "The API key was rejected by the model provider.")
            if status == 429:
                raise HTTPException(429, "The model provider is rate limiting requests. Try again shortly.")
            raise HTTPException(502, f"Model provider error ({type(e).__name__}). Please resend.")
        except Exception:
            sessions[body.session_id] = before
            log.exception("turn failed; session restored")
            raise HTTPException(500, "Something went wrong on our side. The message was not applied; please resend.")


def _http_error(e: Exception) -> HTTPException | None:
    """The HTTP error /api/chat gives for a provider failure, or None for anything else."""
    if isinstance(e, anthropic.APIError):
        status = 401 if isinstance(e, anthropic.AuthenticationError) else 429 if isinstance(e, anthropic.RateLimitError) else 502
    elif isinstance(e, _ProviderError):
        status = getattr(e, "status", 502)
    else:
        return None
    if status == 401:
        return HTTPException(401, "The API key was rejected by the model provider.")
    if status == 429:
        return HTTPException(429, "The model provider is rate limiting requests. Try again shortly.")
    return HTTPException(502, f"Model provider error ({type(e).__name__}). Please resend.")


def _line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn, x_api_key: str | None = Header(default=None)):
    """Same turn as /api/chat, sent as NDJSON: one {"type":"sentence"} line per sentence as it
    passes the gates, then {"type":"done", ...the /api/chat fields}. The session lock is held until
    the stream ends. Failures before the first sentence are the same HTTP errors as /api/chat;
    after that they are a final {"type":"error"} line."""
    llm = _llm(x_api_key)
    s, lock = _session(body.session_id)
    lock.acquire()
    before = copy.deepcopy(s)

    def events():
        started = False
        turn = agent.handle_stream(s, body.message.strip(), llm, channel=body.channel, heard=body.heard)
        try:
            for kind, payload in turn:
                started = True
                yield {"type": "sentence", "text": payload} if kind == "sentence" else {"type": "done", **payload}
        except GeneratorExit:
            raise
        except Exception as e:
            sessions[body.session_id] = before  # a failed turn leaves no trace
            http = _http_error(e)
            if not http:
                log.exception("streamed turn failed; session restored")
            if not started:
                raise http or HTTPException(500, "Something went wrong on our side. The message was not applied; please resend.")
            yield {"type": "error", "detail": http.detail if http else "Something went wrong on our side. The message was not applied; please resend."}
        finally:
            turn.close()  # records what was emitted if the caller went away
            lock.release()

    gen = events()
    try:
        first = next(gen)  # runs the turn up to its first sentence, so early failures are HTTP errors
    except StopIteration:
        first = None
    except BaseException:
        gen.close()
        raise

    def body_iter():
        try:
            if first is not None:
                yield _line(first)
            for ev in gen:
                yield _line(ev)
        finally:
            gen.close()

    return StreamingResponse(body_iter(), media_type="application/x-ndjson")


@app.post("/api/reset")
def reset(body: ResetIn):
    _, lock = _session(body.session_id)
    with lock:  # never swap a session out from under a running turn
        sessions[body.session_id] = Session(body.session_id)
        return state(sessions[body.session_id])


@app.get("/api/state")
def get_state(session_id: str):
    s, _ = _session(session_id, create=False)
    return state(s)


@app.get("/api/outbox")
def get_outbox(session_id: str):
    return [e for e in outbox if e["session_id"] == session_id]


@app.post("/api/tts")
def speak(body: TtsIn):
    """Speak a reply with Kokoro. The text is never logged."""
    text = body.text.strip()
    if not text or len(text) > 2000:
        raise HTTPException(400, "Text must be 1 to 2000 characters.")
    if not tts.available():
        raise HTTPException(503, "Server speech is not available; use the browser voice.")
    try:
        return Response(tts.synthesize(text), media_type="audio/wav")
    except Exception:
        log.error("speech synthesis failed")  # no text, no traceback (it could echo the input)
        raise HTTPException(503, "Server speech failed; use the browser voice.")


@app.get("/api/config")
def config():
    provider = _provider()
    key_env = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    return {
        "server_has_key": bool(os.environ.get(key_env, "").strip()),
        "fake_llm": provider == "fake",
        "provider": provider,
        **(model_names(provider) if provider != "fake" else {}),
        "effort": os.environ.get("CLAIMS_EFFORT", "").strip() or "medium",
        "today": store.today.isoformat(),
        "tts": "kokoro" if tts.available() else "browser",
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
