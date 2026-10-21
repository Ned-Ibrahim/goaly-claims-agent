"""Adapter tests with mocked SDK clients (no network)."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import anthropic
import httpx
import openai
import pytest
from fastapi import HTTPException

from app import llm as L
from app import server
from app.models import Extraction

ENV = ("CLAIMS_PROVIDER", "SOP_LLM", "CLAIMS_EXTRACT_MODEL", "CLAIMS_RESPOND_MODEL",
       "CLAIMS_EFFORT", "OPENAI_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def sdk_error(mod, cls, status):
    req = httpx.Request("POST", "https://example.test")
    return getattr(mod, cls)("x", response=httpx.Response(status, request=req), body=None)


def anth_text(text="hello", stop="end_turn"):
    return NS(stop_reason=stop, content=[NS(type="text", text=text)])


def test_anthropic_defaults_and_models():
    a = L.AnthropicLLM("k")
    assert (a.extract_model, a.respond_model, a.effort) == ("claude-haiku-5-5", "claude-haiku-5-5", "low")


def test_anthropic_env_overrides(monkeypatch):
    monkeypatch.setenv("CLAIMS_EXTRACT_MODEL", "e1")
    monkeypatch.setenv("CLAIMS_RESPOND_MODEL", "r1")
    monkeypatch.setenv("CLAIMS_EFFORT", "HIGH")
    a = L.AnthropicLLM("k")
    assert (a.extract_model, a.respond_model, a.effort) == ("e1", "r1", "high")


def test_anthropic_bad_effort(monkeypatch):
    monkeypatch.setenv("CLAIMS_EFFORT", "turbo")
    with pytest.raises(L.ConfigError):
        L.AnthropicLLM("k")


def test_anthropic_extract_uses_extract_model_without_effort():
    a = L.AnthropicLLM("k")
    a.client = MagicMock()
    ex = Extraction()
    a.client.beta.messages.parse.return_value = NS(stop_reason="end_turn", parsed_output=ex)
    assert a.extract("sys", "user") == ex
    kw = a.client.beta.messages.parse.call_args.kwargs
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["output_format"] is L.ExtractionWire
    assert "output_config" not in kw


def test_anthropic_respond_uses_respond_model_and_effort():
    a = L.AnthropicLLM("k", effort="low")
    a.client = MagicMock()
    a.client.beta.messages.create.return_value = anth_text("  hi  ")
    assert a.respond("sys", [{"role": "user", "content": "yo"}]) == "hi"
    kw = a.client.beta.messages.create.call_args.kwargs
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["output_config"] == {"effort": "low"}


def test_anthropic_refusal_and_empty_are_llm_errors():
    a = L.AnthropicLLM("k")
    a.client = MagicMock()
    a.client.beta.messages.parse.return_value = NS(stop_reason="refusal", parsed_output=None)
    with pytest.raises(L.LLMError):
        a.extract("s", "u")
    a.client.beta.messages.create.return_value = anth_text("")
    with pytest.raises(L.LLMError):
        a.respond("s", [])


@pytest.mark.parametrize("cls,status", [("AuthenticationError", 401), ("RateLimitError", 429),
                                        ("InternalServerError", 502)])
def test_anthropic_extract_errors_map(cls, status):
    a = L.AnthropicLLM("k")
    a.client = MagicMock()
    a.client.beta.messages.parse.side_effect = sdk_error(anthropic, cls, {401: 401, 429: 429, 502: 500}[status])
    with pytest.raises(L.ProviderError) as ei:
        a.extract("s", "u")
    assert ei.value.status == status


def test_anthropic_respond_auth_is_provider_error_but_outage_degrades():
    a = L.AnthropicLLM("k")
    a.client = MagicMock()
    a.client.beta.messages.create.side_effect = sdk_error(anthropic, "AuthenticationError", 401)
    with pytest.raises(L.ProviderError) as ei:
        a.respond("s", [])
    assert ei.value.status == 401
    a.client.beta.messages.create.side_effect = sdk_error(anthropic, "InternalServerError", 500)
    with pytest.raises(L.LLMError):
        a.respond("s", [])


def test_openai_requires_model_names(monkeypatch):
    with pytest.raises(L.ConfigError, match="CLAIMS_EXTRACT_MODEL and CLAIMS_RESPOND_MODEL"):
        L.OpenAILLM("k")
    monkeypatch.setenv("CLAIMS_EXTRACT_MODEL", "e")
    with pytest.raises(L.ConfigError, match="CLAIMS_RESPOND_MODEL"):
        L.OpenAILLM("k")


def test_openai_base_url(monkeypatch):
    monkeypatch.setenv("CLAIMS_EXTRACT_MODEL", "e")
    monkeypatch.setenv("CLAIMS_RESPOND_MODEL", "r")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://compat.example.test/v1")
    o = L.OpenAILLM("k")
    assert str(o.client.base_url).startswith("https://compat.example.test/v1")


def make_openai():
    o = L.OpenAILLM("k", extract_model="e", respond_model="r")
    o.client = MagicMock()
    return o


def test_openai_extract_uses_parse():
    o = make_openai()
    ex = Extraction()
    o.client.chat.completions.parse.return_value = NS(
        choices=[NS(message=NS(parsed=ex, refusal=None))])
    assert o.extract("sys", "user") == ex
    kw = o.client.chat.completions.parse.call_args.kwargs
    assert kw["model"] == "e" and kw["response_format"] is L.ExtractionWire
    assert kw["messages"][0] == {"role": "system", "content": "sys"}


def test_openai_respond_and_refusal():
    o = make_openai()
    o.client.chat.completions.create.return_value = NS(
        choices=[NS(message=NS(content=" ok ", refusal=None), finish_reason="stop")])
    assert o.respond("sys", [{"role": "user", "content": "hi"}]) == "ok"
    assert o.client.chat.completions.create.call_args.kwargs["model"] == "r"
    o.client.chat.completions.parse.return_value = NS(
        choices=[NS(message=NS(parsed=None, refusal="no"))])
    with pytest.raises(L.LLMError):
        o.extract("s", "u")


@pytest.mark.parametrize("cls,http,status", [("AuthenticationError", 401, 401),
                                             ("RateLimitError", 429, 429),
                                             ("InternalServerError", 500, 502)])
def test_openai_errors_map(cls, http, status):
    o = make_openai()
    o.client.chat.completions.parse.side_effect = sdk_error(openai, cls, http)
    with pytest.raises(L.ProviderError) as ei:
        o.extract("s", "u")
    assert ei.value.status == status


def test_selection_fake(monkeypatch):
    monkeypatch.setenv("SOP_LLM", "fake")
    assert isinstance(server._llm(None), L.FakeLLM)
    assert server.config()["provider"] == "fake"
    monkeypatch.delenv("SOP_LLM")
    monkeypatch.setenv("CLAIMS_PROVIDER", "fake")
    assert isinstance(server._llm(None), L.FakeLLM)


def test_selection_anthropic_default_and_header_key(monkeypatch):
    assert isinstance(server._llm("sk-x"), L.AnthropicLLM)
    with pytest.raises(HTTPException) as ei:
        server._llm(None)
    assert ei.value.status_code == 401
    monkeypatch.setenv("ANTHROPIC_API_KEY", "envkey")
    assert isinstance(server._llm(None), L.AnthropicLLM)


def test_selection_openai(monkeypatch):
    monkeypatch.setenv("CLAIMS_PROVIDER", "openai")
    with pytest.raises(HTTPException) as ei:
        server._llm(None)
    assert ei.value.status_code == 401
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with pytest.raises(HTTPException) as ei:  # model names missing: clear config error
        server._llm(None)
    assert ei.value.status_code == 500 and "CLAIMS_EXTRACT_MODEL" in ei.value.detail
    monkeypatch.setenv("CLAIMS_EXTRACT_MODEL", "e")
    monkeypatch.setenv("CLAIMS_RESPOND_MODEL", "r")
    assert isinstance(server._llm(None), L.OpenAILLM)


def test_selection_unknown_provider(monkeypatch):
    monkeypatch.setenv("CLAIMS_PROVIDER", "nope")
    with pytest.raises(HTTPException):
        server._llm("k")


def test_config_reports_models_never_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-key")
    c = server.config()
    assert c["provider"] == "anthropic" and c["extract_model"] == "claude-haiku-5-5"
    assert c["respond_model"] == "claude-haiku-5-5" and c["server_has_key"] is True
    assert "secret-key" not in str(c)


def test_wire_schema_has_no_optional_parameters():
    # The API rejects a structured-output schema with more than 24 optional parameters.
    schema = L.ExtractionWire.model_json_schema()

    def optional(s):
        n = 0
        for node in [s, *s.get("$defs", {}).values()]:
            props = node.get("properties", {})
            n += len(set(props) - set(node.get("required", [])))
        return n

    assert optional(schema) == 0
    assert set(L.ExtractionWire.model_fields) == set(Extraction.model_fields)


def test_wire_reply_converts_to_extraction():
    wire = L.ExtractionWire.model_validate(Extraction(intent="claim_status").model_dump())
    ex = L._to_extraction(wire)
    assert isinstance(ex, Extraction) and ex.intent == "claim_status"
