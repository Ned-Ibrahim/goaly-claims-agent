"""LLM adapters. The harness talks to an LLM through two calls only:

- extract(): caller message -> Extraction (structured output, no free text)
- respond(): turn plan + allowed data + transcript -> reply text

AnthropicLLM is the real one. FakeLLM is a deterministic stand-in (regex extraction,
templated replies) for tests and for running the eval suite without an API key.
"""

from __future__ import annotations

import os
import re
from typing import Protocol

import anthropic

from .models import Extraction
from .store import norm_dob

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """The model call failed or declined; the caller should use the safe fallback reply."""


class LLM(Protocol):
    def extract(self, system: str, user: str) -> Extraction: ...
    def respond(self, system: str, transcript: list[dict[str, str]]) -> str: ...


class AnthropicLLM:
    def __init__(self, api_key: str, model: str | None = None):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=60.0)
        self.model = model or os.environ.get("SOP_MODEL") or DEFAULT_MODEL

    def extract(self, system: str, user: str) -> Extraction:
        try:
            resp = self._parse(system, user)
        except anthropic.APIError:
            raise  # auth / rate limit / outage: the server reports it and the turn is not applied
        except Exception as e:  # malformed or schema-invalid output
            raise LLMError(f"unreadable extraction ({type(e).__name__})") from e
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise LLMError(f"extraction stopped: {resp.stop_reason}")
        return resp.parsed_output

    def _parse(self, system: str, user: str):
        return self.client.beta.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=Extraction,
            output_config={"effort": "low"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    def respond(self, system: str, transcript: list[dict[str, str]]) -> str:
        # By this point the controller has already changed state, so a failure here
        # degrades to the safe fallback reply instead of failing the whole turn.
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=system,
                messages=[{"role": t["role"], "content": t["content"]} for t in transcript],
                output_config={"effort": "low"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMError(type(e).__name__) from e
        if resp.stop_reason == "refusal":
            raise LLMError("reply declined by the model")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise LLMError(f"empty reply (stop_reason={resp.stop_reason})")
        return text


# The deterministic stand-in lives in fake_llm.py; re-exported for existing imports.
from .fake_llm import FakeLLM  # noqa: E402,F401
