"""LLM adapters. The harness talks to an LLM through two calls only:

- extract(): caller message -> Extraction (structured output, no free text)
- respond(): turn plan + allowed data + transcript -> reply text

AnthropicLLM and OpenAILLM are the real ones. FakeLLM is a deterministic stand-in (regex extraction,
templated replies) for tests and for running the eval suite without an API key.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Protocol

import anthropic
import openai
from pydantic import BaseModel, Field, create_model

from .models import Extraction

FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_EXTRACT_MODEL = "claude-haiku-5-5"
# Haiku at low effort for the spoken reply: Opus at medium took most of a ~10 s voice turn (5b revised).
DEFAULT_RESPOND_MODEL = "claude-haiku-5-5"
DEFAULT_EFFORT = "low"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class LLMError(Exception):
    """The model call failed or declined; the caller should use the safe fallback reply."""


class ConfigError(Exception):
    """A required setting is missing (for example an OpenAI model name)."""


class ProviderError(Exception):
    """The provider rejected the call. status: 401 bad key, 429 rate limit, 502 anything else."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(detail or str(status))
        self.status = status


class LLM(Protocol):
    def extract(self, system: str, user: str) -> Extraction: ...
    def respond(self, system: str, transcript: list[dict[str, str]]) -> str: ...
    def respond_stream(self, system: str, transcript: list[dict[str, str]]) -> Iterator[str]: ...


log = logging.getLogger("claims.llm")


def _all_required(model: type[BaseModel]) -> type[BaseModel]:
    """The same fields with no defaults, nested models included.

    Structured outputs cap a schema at 24 optional parameters; Extraction has more, all
    with defaults so code can build an empty one. The wire copy makes every field required
    (nullable ones stay nullable), and replies are converted back to the real model.
    """
    fields = {}
    for name, f in model.model_fields.items():
        ann = f.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            ann = _all_required(ann)
        fields[name] = (ann, Field(..., description=f.description))
    return create_model(f"{model.__name__}Wire", __doc__=model.__doc__, **fields)


ExtractionWire = _all_required(Extraction)


def _to_extraction(wire: BaseModel) -> Extraction:
    return Extraction.model_validate(wire.model_dump())


def provider_error(e: Exception) -> ProviderError:
    """Map an Anthropic or OpenAI SDK error onto ProviderError."""
    # The provider's own status and message (never the key) make a 502 diagnosable.
    log.warning("model provider error: %s status=%s %s", type(e).__name__,
                getattr(e, "status_code", None), getattr(e, "message", "") or str(e)[:300])
    if isinstance(e, (anthropic.AuthenticationError, openai.AuthenticationError)):
        return ProviderError(401, type(e).__name__)
    if isinstance(e, (anthropic.RateLimitError, openai.RateLimitError)):
        return ProviderError(429, type(e).__name__)
    return ProviderError(502, type(e).__name__)


def model_names(provider: str) -> dict[str, str | None]:
    """Configured model names for a provider (env only, never a key)."""
    extract = os.environ.get("CLAIMS_EXTRACT_MODEL", "").strip() or None
    respond = os.environ.get("CLAIMS_RESPOND_MODEL", "").strip() or None
    if provider == "anthropic":
        extract, respond = extract or DEFAULT_EXTRACT_MODEL, respond or DEFAULT_RESPOND_MODEL
    return {"extract_model": extract, "respond_model": respond}


class AnthropicLLM:
    def __init__(self, api_key: str, extract_model: str | None = None,
                 respond_model: str | None = None, effort: str | None = None):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=60.0)
        names = model_names("anthropic")
        self.extract_model = extract_model or names["extract_model"]
        self.respond_model = respond_model or names["respond_model"]
        self.effort = (effort or os.environ.get("CLAIMS_EFFORT", "").strip() or DEFAULT_EFFORT).lower()
        if self.effort not in EFFORTS:
            raise ConfigError(f"CLAIMS_EFFORT must be one of {', '.join(EFFORTS)}")

    def extract(self, system: str, user: str) -> Extraction:
        try:
            resp = self._parse(system, user)
        except anthropic.APIError as e:  # auth / rate limit / outage: the turn is not applied
            raise provider_error(e) from e
        except Exception as e:  # malformed or schema-invalid output
            raise LLMError(f"unreadable extraction ({type(e).__name__})") from e
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise LLMError(f"extraction stopped: {resp.stop_reason}")
        return _to_extraction(resp.parsed_output)

    def _parse(self, system: str, user: str):
        # No effort here: Haiku 5.5 defaults to medium, and omitting it is identical.
        return self.client.beta.messages.parse(
            model=self.extract_model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=ExtractionWire,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    def respond(self, system: str, transcript: list[dict[str, str]]) -> str:
        # By this point the controller has already changed state, so a failure here
        # degrades to the safe fallback reply, except a bad key or rate limit.
        try:
            resp = self.client.beta.messages.create(
                model=self.respond_model,
                max_tokens=4000,
                system=system,
                messages=[{"role": t["role"], "content": t["content"]} for t in transcript],
                output_config={"effort": self.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            pe = provider_error(e)
            if pe.status != 502:
                raise pe from e
            raise LLMError(type(e).__name__) from e
        if resp.stop_reason == "refusal":
            raise LLMError("reply declined by the model")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise LLMError(f"empty reply (stop_reason={resp.stop_reason})")
        return text

    def respond_stream(self, system: str, transcript: list[dict[str, str]]) -> Iterator[str]:
        """The same call as respond(), yielding text deltas. Errors map the same way."""
        got = False
        try:
            with self.client.beta.messages.stream(
                model=self.respond_model,
                max_tokens=4000,
                system=system,
                messages=[{"role": t["role"], "content": t["content"]} for t in transcript],
                output_config={"effort": self.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                for delta in stream.text_stream:
                    got = got or bool(delta.strip())
                    yield delta
                final = stream.get_final_message()
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            pe = provider_error(e)
            if pe.status != 502:
                raise pe from e
            raise LLMError(type(e).__name__) from e
        if final.stop_reason == "refusal":
            raise LLMError("reply declined by the model")
        if not got:
            raise LLMError(f"empty reply (stop_reason={final.stop_reason})")


class OpenAILLM:
    """Native OpenAI SDK. Model names are required settings; no defaults are guessed."""

    def __init__(self, api_key: str, extract_model: str | None = None,
                 respond_model: str | None = None, base_url: str | None = None):
        names = model_names("openai")
        self.extract_model = extract_model or names["extract_model"]
        self.respond_model = respond_model or names["respond_model"]
        missing = [n for n, v in (("CLAIMS_EXTRACT_MODEL", self.extract_model),
                                  ("CLAIMS_RESPOND_MODEL", self.respond_model)) if not v]
        if missing:
            raise ConfigError(f"OpenAI provider needs {' and '.join(missing)} set to a model name.")
        base_url = base_url or os.environ.get("OPENAI_BASE_URL", "").strip() or None
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=2, timeout=60.0)

    def extract(self, system: str, user: str) -> Extraction:
        try:
            resp = self.client.chat.completions.parse(
                model=self.extract_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=ExtractionWire,
            )
        except openai.APIError as e:
            raise provider_error(e) from e
        except Exception as e:
            raise LLMError(f"unreadable extraction ({type(e).__name__})") from e
        msg = resp.choices[0].message
        if msg.refusal or msg.parsed is None:
            raise LLMError("extraction stopped: refusal" if msg.refusal else "extraction empty")
        return _to_extraction(msg.parsed)

    def respond(self, system: str, transcript: list[dict[str, str]]) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.respond_model,
                messages=[{"role": "system", "content": system}]
                + [{"role": t["role"], "content": t["content"]} for t in transcript],
            )
        except (openai.APIStatusError, openai.APIConnectionError) as e:
            pe = provider_error(e)
            if pe.status != 502:
                raise pe from e
            raise LLMError(type(e).__name__) from e
        msg = resp.choices[0].message
        if msg.refusal:
            raise LLMError("reply declined by the model")
        text = (msg.content or "").strip()
        if not text:
            raise LLMError(f"empty reply (finish_reason={resp.choices[0].finish_reason})")
        return text

    def respond_stream(self, system: str, transcript: list[dict[str, str]]) -> Iterator[str]:
        got = False
        refused = False
        try:
            stream = self.client.chat.completions.create(
                model=self.respond_model,
                messages=[{"role": "system", "content": system}]
                + [{"role": t["role"], "content": t["content"]} for t in transcript],
                stream=True,
            )
            with stream:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    d = chunk.choices[0].delta
                    if getattr(d, "refusal", None):
                        refused = True
                    if d.content:
                        got = got or bool(d.content.strip())
                        yield d.content
        except (openai.APIStatusError, openai.APIConnectionError) as e:
            pe = provider_error(e)
            if pe.status != 502:
                raise pe from e
            raise LLMError(type(e).__name__) from e
        if refused:
            raise LLMError("reply declined by the model")
        if not got:
            raise LLMError("empty reply")


# The deterministic stand-in lives in fake_llm.py; re-exported for existing imports.
from .fake_llm import FakeLLM  # noqa: E402,F401
