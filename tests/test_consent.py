"""Representatives and policyholder consent (step 8), driven through the Agent with FakeLLM."""

from __future__ import annotations

import pytest

from app import consent
from app.agent import Agent
from app.llm import FakeLLM
from app.models import Phase, Session
from app.store import DataStore

DETAILS = "Her date of birth is 1985-03-15 and her SSN last four is 4472."
DAVID = f"I'm David Chen, calling for my mother Margaret Chen. {DETAILS}"
NEIGHBOR = f"I'm Tom Baker, her neighbor, calling for Margaret Chen. {DETAILS}"

# Anything about her claims that must stay unsaid until she approves.
CLAIM_FACTS = ("CL-2048", "CL-2011", "CL-1899", "CL-2102", "CL-3001", "pathology", "denied", "$")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("CLAIMS_CONSENT_SCENARIO", raising=False)
    return Agent(DataStore(), []), FakeLLM()


def run(env, *messages, session=None):
    agent, llm = env
    s = session or Session("t")
    return s, [agent.handle(s, m, llm) for m in messages]


def no_claim_facts(reply: str, allow: tuple[str, ...] = ()) -> bool:
    return not any(f in reply for f in CLAIM_FACTS if f not in allow)


def test_scenarios_follow_the_starter_data(monkeypatch):
    assert [consent.consent_status("default", i) for i in range(3)] == ["pending", "approved", "approved"]
    assert {consent.consent_status("timeout", i) for i in range(5)} == {"pending"}
    assert consent.CONSENT_TIMEOUT_CHECKS == 5
    monkeypatch.setenv("CLAIMS_CONSENT_SCENARIO", "timeout")
    assert consent.scenario_name() == "timeout"


def test_listed_representative_reaches_process_case_only_after_approval(env):
    s, r = run(env, DAVID)
    # Both identity gates pass, but no claim list or fact is shared.
    assert r[0]["phase"] == "RESOLVE_INTENT"
    assert s.caller_role == "representative"
    assert (s.rep_name, s.rep_relationship) == ("David Chen", "child")
    assert no_claim_facts(r[0]["reply"])
    # The request names the claim; the agent waits.
    _, r = run(env, "I want to know why claim CL-2048 was denied.", session=s)
    assert r[0]["phase"] == "RESOLVE_INTENT"
    assert s.consent_status == "pending" and s.consent_claim_id == "CL-2048"
    assert s.selected_claim_id is None
    assert no_claim_facts(r[0]["reply"], allow=("CL-2048",))
    assert "waiting" in r[0]["reply"]
    # The second check comes back approved.
    _, r = run(env, "Any update?", session=s)
    assert r[0]["phase"] == "PROCESS_CASE"
    assert s.consent_status == "approved" and s.consent_checks == 2
    assert s.selected_claim_id == "CL-2048"
    assert "pathology report" in r[0]["reply"]


def test_hint_resolves_the_claim_without_naming_it(env):
    s, r = run(env, DAVID, "It's about the denied healthcare claim from January.")
    assert s.consent_claim_id == "CL-2048" and s.consent_status == "pending"
    assert no_claim_facts(r[1]["reply"])


def test_timeout_never_discloses_and_offers_a_human(env, monkeypatch):
    monkeypatch.setenv("CLAIMS_CONSENT_SCENARIO", "timeout")
    s, r = run(env, DAVID, "Why was claim CL-2048 denied?", *["Any news?"] * 4)
    for res in r[:-1]:
        assert res["phase"] == "RESOLVE_INTENT"
    assert s.consent_status == "timeout" and s.consent_checks == 5
    assert s.selected_claim_id is None and s.phase == Phase.RESOLVE_INTENT
    for res in r[2:]:
        assert no_claim_facts(res["reply"])
    assert "human representative" in r[-1]["reply"]
    assert s.pending == "handoff_offer"
    # Taking the offer hands off, still with nothing disclosed.
    s, r2 = run(env, "yes please", session=s)
    assert r2[0]["phase"] == "HANDOFF" and no_claim_facts(r2[0]["reply"])


def test_unlisted_caller_with_correct_details_gets_a_handoff(env):
    s, r = run(env, NEIGHBOR)
    assert r[0]["phase"] == "HANDOFF"
    assert s.handoff_packet["reason"] == "unlisted_representative"
    assert not s.verified and s.customer_id is None
    assert "someone already on file" in r[0]["reply"]
    assert no_claim_facts(r[0]["reply"])
    assert s.consent_status is None


def test_wrong_relationship_for_a_listed_name_is_unlisted(env):
    s, r = run(env, f"I'm David Chen, her neighbor, calling for Margaret Chen. {DETAILS}")
    assert s.handoff_packet["reason"] == "unlisted_representative"


def test_rep_who_gives_name_after_the_details_is_still_checked(env):
    s, r = run(env, f"I'm calling for my mother Margaret Chen. {DETAILS}")
    assert r[0]["phase"] == "VERIFY_ID" and s.pending == "rep_identify"
    assert no_claim_facts(r[0]["reply"])
    s, r = run(env, "I'm David Chen, her son.", session=s)
    assert r[0]["phase"] == "RESOLVE_INTENT" and s.verified


def test_second_claim_needs_a_new_consent_request(env):
    s, r = run(env, DAVID, "Why was claim CL-2048 denied?", "Any update?")
    assert r[2]["phase"] == "PROCESS_CASE" and s.consent_checks == 2
    # Asking about another claim sends the caller back to a fresh request.
    _, r2 = run(env, "What is the status of claim CL-2011?", session=s)
    assert r2[0]["phase"] == "RESOLVE_INTENT"
    assert s.consent_claim_id == "CL-2011" and s.consent_status == "pending"
    assert s.selected_claim_id is None and "waiting" in r2[0]["reply"]
    assert no_claim_facts(r2[0]["reply"], allow=("CL-2011",))
    _, r3 = run(env, "Any update?", session=s)
    assert r3[0]["phase"] == "PROCESS_CASE" and s.selected_claim_id == "CL-2011"
    assert s.consent_status == "approved"
    requests = [e for res in r + r2 + r3 for e in res["events"] if e.get("name") == "request_consent"]
    assert len(requests) == 2
