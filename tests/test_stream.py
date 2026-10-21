"""Streaming reply: per-sentence gates, fallbacks, transcript, and the NDJSON endpoint."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import server
from app.agent import Agent
from app.llm import FakeLLM
from app.models import Session
from app.sentences import split_sentences
from app.store import DataStore

MARGARET = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my "
    "denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)


class Scripted(FakeLLM):
    """Replies from a script, one per respond call (stream or not); the last one repeats."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0
        self.closed = False

    def respond(self, system, transcript):
        r = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return r

    def respond_stream(self, system, transcript):
        r = self.respond(system, transcript)
        try:
            for i in range(0, len(r), 7):
                yield r[i : i + 7]
        finally:
            self.closed = True


def stream(agent, s, msg, llm):
    sentences, done = [], None
    for kind, payload in agent.handle_stream(s, msg, llm):
        if kind == "sentence":
            sentences.append(payload)
        else:
            done = payload
    return sentences, done


@pytest.fixture
def agent():
    return Agent(DataStore(), [])


def test_split_keeps_numbers_whole():
    assert split_sentences("It costs $1,450.00 today. Is 3.5 fine? Yes.") == ["It costs $1,450.00 today.", "Is 3.5 fine?", "Yes."]


def test_sentences_match_chat_reply(agent):
    s1, s2 = Session("a"), Session("b")
    for msg in (MARGARET, "yes"):
        full = agent.handle(s1, msg, FakeLLM())
        sentences, done = stream(agent, s2, msg, FakeLLM())
        assert len(sentences) >= 1
        assert " ".join(sentences) == full["reply"] == done["reply"]
        assert done["phase"] == full["phase"] and done["transcript"] == full["transcript"]


def test_static_reply_streams(agent):
    s = Session("h")
    assert agent.handle(s, "I want to talk to a real person", FakeLLM())["phase"] == "HANDOFF"
    llm = Scripted("never used")
    sentences, done = stream(agent, s, "hello?", llm)
    assert sentences and " ".join(sentences) == done["reply"]
    assert llm.calls == 0
    assert s.transcript[-1]["content"] == done["reply"]


def test_identity_value_in_later_sentence_never_emitted(agent):
    s = Session("p")
    stream(agent, s, MARGARET, FakeLLM())  # verified; values stored
    llm = Scripted("Thanks for waiting. Your birthday is 1985-03-15 on file. Anything else?")
    sentences, done = stream(agent, s, "yes", llm)
    assert sentences[0] == "Thanks for waiting."
    assert sentences[0] == "Thanks for waiting."
    assert "connect you with a human representative" in " ".join(sentences[1:])  # plan.fallback
    assert not any("1985" in x or "03-15" in x for x in sentences)
    assert "1985" not in done["reply"]
    assert llm.closed  # the model stream was stopped
    assert "1985" not in json.dumps(s.transcript)
    assert s.transcript[-1]["content"] == " ".join(sentences)


def test_gate_failure_before_first_sentence_uses_retry_path(agent):
    s = Session("r")
    stream(agent, s, MARGARET, FakeLLM())
    llm = Scripted("Your birthday is 1985-03-15. That is all.", "Good news, nothing on file leaked. Anything else?")
    sentences, done = stream(agent, s, "yes", llm)
    assert llm.calls == 2  # stream, then the non-streaming retry
    assert sentences == ["Good news, nothing on file leaked.", "Anything else?"]
    assert any(e.get("action") == "regenerate" for e in done["events"] if e["type"] == "guard")
    assert s.transcript[-1]["content"] == " ".join(sentences)


def test_unverified_caller_cannot_hear_claim_data(agent):
    s = Session("u")
    llm = Scripted("Hello there. Claim CL-2048 was denied.")
    sentences, _ = stream(agent, s, "why was my claim denied?", llm)
    assert not any("CL-2048" in x for x in sentences)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SOP_LLM", "fake")
    server.sessions.clear()
    return TestClient(server.app)


def lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x]


def test_endpoint_ndjson_matches_chat(client):
    a = client.post("/api/chat", json={"session_id": "c1", "message": MARGARET}).json()
    r = client.post("/api/chat/stream", json={"session_id": "c2", "message": MARGARET})
    assert r.headers["content-type"].startswith("application/x-ndjson")
    ev = lines(r)
    assert [e["type"] for e in ev[:-1]] and all(e["type"] == "sentence" for e in ev[:-1])
    done = ev[-1]
    assert done["type"] == "done"
    assert " ".join(e["text"] for e in ev[:-1]) == a["reply"] == done["reply"]
    assert {k: done[k] for k in a if k not in ("session_id", "events")} == {k: a[k] for k in a if k not in ("session_id", "events")}


def test_endpoint_releases_lock_and_orders_turns(client):
    client.post("/api/chat/stream", json={"session_id": "c3", "message": MARGARET})
    r = client.post("/api/chat/stream", json={"session_id": "c3", "message": "yes"})
    assert lines(r)[-1]["turn"] == 2


def test_endpoint_missing_key_is_http_error(monkeypatch):
    monkeypatch.delenv("SOP_LLM", raising=False)
    monkeypatch.setenv("CLAIMS_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = TestClient(server.app).post("/api/chat/stream", json={"session_id": "c4", "message": "hi"})
    assert r.status_code == 401
