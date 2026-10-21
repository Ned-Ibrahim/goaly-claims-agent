"""Realtime voice: the ephemeral key route, with the OpenAI call mocked (no network)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import server

client = TestClient(server.app)


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


@pytest.fixture
def seen(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return Resp(200, {"value": "ek_test"})

    monkeypatch.setattr(server.httpx, "post", fake_post)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLAIMS_REALTIME_MODEL", raising=False)
    return calls


def test_session_mints_key_with_the_tool(seen):
    r = client.post("/api/realtime/session", headers={"x-api-key": "sk-test"})
    assert r.status_code == 200
    assert r.json() == {"client_secret": "ek_test", "model": "gpt-realtime"}
    url, kw = seen[0]
    assert url == "https://api.openai.com/v1/realtime/client_secrets"
    assert kw["headers"]["Authorization"] == "Bearer sk-test"
    session = kw["json"]["session"]
    assert session["type"] == "realtime"
    assert [t["name"] for t in session["tools"]] == ["agent_turn"]
    assert session["audio"]["input"]["turn_detection"] == {"type": "semantic_vad"}


def test_model_override_and_env_key(seen, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("CLAIMS_REALTIME_MODEL", "gpt-realtime-mini")
    r = client.post("/api/realtime/session")
    assert r.json()["model"] == "gpt-realtime-mini"
    assert seen[0][1]["headers"]["Authorization"] == "Bearer sk-env"


def test_no_key_is_401(seen):
    assert client.post("/api/realtime/session").status_code == 401
    assert seen == []


@pytest.mark.parametrize("upstream,expected", [(401, 401), (403, 401), (429, 429), (500, 502)])
def test_error_mapping(monkeypatch, upstream, expected):
    monkeypatch.setattr(server.httpx, "post", lambda url, **kw: Resp(upstream))
    r = client.post("/api/realtime/session", headers={"x-api-key": "sk-test"})
    assert r.status_code == expected
    assert "sk-test" not in r.text


def test_network_failure_is_502(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(server.httpx, "post", boom)
    assert client.post("/api/realtime/session", headers={"x-api-key": "k"}).status_code == 502
