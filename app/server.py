"""HTTP API and the test UI.

The API key comes from the X-API-Key header (the UI field) or, if absent, from the
ANTHROPIC_API_KEY environment variable. It is never logged or stored server side.
Set SOP_LLM=fake to run with the deterministic FakeLLM (no key needed).
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent, state
from .llm import DEFAULT_MODEL, AnthropicLLM, FakeLLM
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


def _llm(api_key: str | None):
    if os.environ.get("SOP_LLM") == "fake":
        return FakeLLM()
    key = (api_key or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise HTTPException(
            401,
            "No API key. Paste an Anthropic API key into the UI, send it as the X-API-Key "
            "header, or set ANTHROPIC_API_KEY before starting the server.",
        )
    return AnthropicLLM(key)


@app.post("/api/chat")
def chat(body: ChatIn, x_api_key: str | None = Header(default=None)):
    llm = _llm(x_api_key)
    s, lock = _session(body.session_id)
    with lock:
        before = copy.deepcopy(s)
        try:
            return agent.handle(s, body.message.strip(), llm)
        except anthropic.APIError as e:
            sessions[body.session_id] = before  # a failed turn leaves no trace
            if isinstance(e, anthropic.AuthenticationError):
                raise HTTPException(401, "The API key was rejected by the model provider.")
            if isinstance(e, anthropic.RateLimitError):
                raise HTTPException(429, "The model provider is rate limiting requests. Try again shortly.")
            raise HTTPException(502, f"Model provider error ({type(e).__name__}). Please resend.")
        except Exception:
            sessions[body.session_id] = before
            log.exception("turn failed; session restored")
            raise HTTPException(500, "Something went wrong on our side. The message was not applied; please resend.")


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


@app.get("/api/config")
def config():
    return {
        "server_has_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "fake_llm": os.environ.get("SOP_LLM") == "fake",
        "model": os.environ.get("SOP_MODEL") or DEFAULT_MODEL,
        "today": store.today.isoformat(),
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
