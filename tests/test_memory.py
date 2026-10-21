"""Memory contracts: usable-from gate, recent turns, person switch, masked trace."""

from __future__ import annotations

import json

import pytest

from app import memory
from app.agent import Agent
from app.llm import FakeLLM
from app.models import Extraction, Session
from app.store import DataStore

HINT = "my healthcare claim from January was denied, why?"
ID_MSG = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. "
    "DOB is 1985-03-15, SSN last four is 4472."
)


@pytest.fixture
def agent():
    return Agent(DataStore(), [])


def test_hint_stored_but_gated_until_verified(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_TRACE_DIR", str(tmp_path))
    s, llm = Session("m1"), FakeLLM()
    agent.handle(s, "Hi, " + HINT, llm)
    assert not s.verified
    assert s.memory["claim_month"].value == "January"
    assert memory.known_so_far(s) == {}
    agent.handle(s, ID_MSG, llm)
    assert s.verified
    known = memory.known_so_far(s)
    assert known["claim_month"]["value"] == "January"
    assert "words" in known["claim_month"]
    assert not any(k.startswith("id.") for k in known)


def test_recent_turns_length_and_first_role():
    s = Session("m2")
    for i in range(5):
        s.transcript += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
    t = memory.recent_turns(s, 3)
    assert len(t) == 2 and t[0]["role"] == "user"
    t = memory.recent_turns(s, 4)
    assert len(t) == 4 and t[0]["content"] == "u3"
    s.window_start = 8
    assert [x["content"] for x in memory.recent_turns(s)] == ["u4", "a4"]


def test_clear_person_wipes_and_keeps(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_TRACE_DIR", str(tmp_path))
    s, llm = Session("m3"), FakeLLM()
    agent.handle(s, "Hi, " + HINT, llm)
    agent.handle(s, ID_MSG, llm)
    s.counters["verify_failures"] = 2
    s.selected_claim_id, s.path = "CL-2048", "denial_explanation"
    memory.clear_person(s, keep_claim_id="cl-3001")
    assert s.identity == {} and not s.verified and s.customer_id is None
    assert s.matched_fields == [] and s.proposed_claim_id is None and s.selected_claim_id is None
    assert s.path is None and s.window_start == len(s.transcript)
    assert set(s.memory) == {"claim_id_hint"}
    assert s.memory["claim_id_hint"].value == "CL-3001"
    assert s.counters["verify_failures"] == 2
    assert memory.recent_turns(s) == []


def test_clear_identity_keeps_claim_hints():
    s = Session("m4")
    memory.write(s, Extraction(dob="1985-03-15", claim_month=1, intent="claim_status"), "x")
    memory.clear_identity(s)
    assert "id.dob" not in s.memory and s.memory["claim_month"].value == "January"


def test_month_correction_overwrites():
    s = Session("m5")
    memory.write(s, Extraction(claim_month=1), "January")
    memory.write(s, Extraction(claim_month=2), "not January, February")
    assert s.memory["claim_month"].value == "February"
    assert s.memory["claim_month"].quote == "not January, February"


def test_trace_has_events_and_no_identity(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMS_TRACE_DIR", str(tmp_path))
    s, llm = Session("m6"), FakeLLM()
    agent.handle(s, "Hi, " + HINT, llm)
    agent.handle(s, ID_MSG, llm)
    text = (tmp_path / "m6.jsonl").read_text()
    lines = [json.loads(x) for x in text.splitlines()]
    assert lines and all({"turn", "phase", "type"} <= set(x) for x in lines)
    p9 = next(p for p in json.loads(
        (DataStore.__module__ and __import__("pathlib").Path("app/data/starter/policyholders.json")).read_text()
    ) if p["party_id"] == "P9")
    for key in ("dob", "id_last4", "email"):
        assert p9[key] not in text
    assert p9["phone"][-10:] not in text
