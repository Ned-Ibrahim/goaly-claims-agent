"""Identity gate tests: read-back (7a), name order (7b), failure count (7d), claim switches (6b, 6c)."""

from __future__ import annotations

import pytest

from app.agent import Agent
from app.llm import FakeLLM
from app.models import Phase, Session
from app.store import DataStore

MARGARET = "My name is Margaret Chen, DOB 1985-03-15, SSN last four 4472"
MARGARET_PHONE = "My phone is 650-521-2836"
WRONG = "My name is Margaret Chen, DOB 1985-03-16, SSN last four 1111"
MA_TIAN = "My name is Ma Tian, DOB 1964-09-10, SSN last four 6688"


@pytest.fixture
def env():
    return Agent(DataStore(), []), FakeLLM()


def run(env, *messages, session=None, channel="text"):
    agent, llm = env
    s = session or Session("t")
    out = []
    for m in messages:
        s.channel = channel
        out.append(agent.handle(s, m, llm))
    return s, out


def verified_margaret(env):
    s, _ = run(env, MARGARET)
    assert s.verified and s.customer_id == "P9"
    return s


# ---- 7a: read-back -----------------------------------------------------------


def test_voice_details_are_read_back_before_the_check(env):
    s, r = run(env, MARGARET, channel="voice")
    assert not s.verified and s.pending == "readback" and s.phase == Phase.VERIFY_ID
    reply = r[0]["reply"]
    assert "ID ending 4, 4, 7, 2" in reply and "birth date March 15, 1985" in reply
    assert reply.endswith("Is that right?")
    assert not [e for e in r[0]["events"] if e.get("name") == "verify_identity"]
    assert sorted(s.readback_fields) == ["dob", "full_name", "id_last4"]  # a misheard name fails the check too
    assert reply.startswith("I heard the name ")


def test_readback_yes_runs_the_check(env):
    s, r = run(env, MARGARET, "yes", channel="voice")
    assert s.verified and s.customer_id == "P9" and s.pending is None
    assert r[1]["phase"] == "RESOLVE_INTENT" and s.readback_fields == []


def test_readback_no_drops_only_those_details_and_is_not_a_failure(env):
    s, r = run(env, MARGARET, "no", channel="voice")
    assert s.counters["verify_failures"] == 0 and s.pending is None
    assert set(s.identity) == set()  # the name was read back with the rest, so a no drops it too
    assert "id.dob" not in s.memory and "id.id_last4" not in s.memory and "id.full_name" not in s.memory
    assert not s.verified and r[1]["phase"] == "VERIFY_ID"
    # Giving them again (by voice) reads them back again, then yes verifies.
    s, r = run(env, MARGARET, "yes", session=s, channel="voice")
    assert s.verified


def test_readback_unclear_asks_again(env):
    s, r = run(env, MARGARET, "maybe, not sure", channel="voice")
    assert s.pending == "readback" and "Is that right?" in r[1]["reply"]
    assert "4, 4, 7, 2" in r[1]["reply"]
    assert s.counters["verify_failures"] == 0 and not s.verified


def test_typed_details_skip_the_readback(env):
    s, r = run(env, MARGARET, channel="text")
    assert s.verified and s.pending != "readback" and s.readback_fields == []


def test_voice_phone_is_read_digit_by_digit(env):
    s, r = run(env, MARGARET_PHONE, channel="voice")
    assert "phone ending 2, 8, 3, 6" in r[0]["reply"]


def test_voice_name_only_has_no_readback(env):
    s, r = run(env, "My name is Margaret Chen", channel="voice")
    assert s.pending is None and s.phase == Phase.VERIFY_ID


def test_readback_line_masks_in_transcript_form(env):
    agent, _ = env
    s = Session("t")
    s.identity.update({"id_last4": "4472", "dob": "1985-03-15", "phone": "6505212836"})
    fields = ["id_last4", "dob", "phone"]
    line = agent.controller._readback_line(s, fields, masked=True)
    assert line == "I heard [ID last 4 given], [DOB given] and [phone given]. Is that right?"
    spoken = agent.controller._readback_line(s, fields, masked=False)
    assert spoken == (
        "I heard ID ending 4, 4, 7, 2, birth date March 15, 1985 and phone ending 2, 8, 3, 6. "
        "Is that right?"
    )


# ---- 7b: name order ----------------------------------------------------------


@pytest.mark.parametrize("name,ok", [
    ("Ma Tian", True), ("Tian Ma", True), ("ma-tian", True), ("TIAN, MA", True),
    ("Matt Tian", False), ("Ma", False), ("Tian Ma Li", False),
])
def test_name_word_order(name, ok):
    r = DataStore().verify_identity({"full_name": name, "dob": "1964-09-10", "id_last4": "6688"})
    assert r.verified is ok and (r.customer_id == "P12" or not ok)


def test_name_order_with_agent(env):
    s, _ = run(env, "My name is Tian Ma, DOB 1964-09-10, SSN last four 6688")
    assert s.verified and s.customer_id == "P12"


def test_alias_still_counts():
    r = DataStore().verify_identity({"full_name": "Yaven Li", "dob": "1989-12-03", "id_last4": "5317"})
    assert r.verified and r.customer_id == "P13"


def test_conflicting_detail_fails_the_check():
    r = DataStore().verify_identity(
        {"full_name": "Ma Tian", "dob": "1964-09-10", "id_last4": "6688", "phone": "6505550000"}
    )
    assert not r.verified and r.mismatched == 1


# ---- 6b: a second claim of the same caller ---------------------------------------


def test_second_claim_of_the_same_caller_skips_reverification(env):
    s, r = run(
        env, MARGARET + ". It is my denied healthcare claim from January.", "yes",
        "I have another claim, CL-1899",
    )
    assert s.verified and s.customer_id == "P9" and s.selected_claim_id == "CL-1899"
    last = r[-1]
    moves = [(e["from"], e["to"]) for e in last["events"] if e["type"] == "transition"]
    assert ("PROCESS_CASE", "RESOLVE_INTENT") in moves
    assert all(to != "VERIFY_ID" for _, to in moves)
    assert not [e for e in last["events"] if e.get("name") == "verify_identity"]


# ---- 6c: a claim that is not the caller's --------------------------------------------


def test_other_claim_gets_one_neutral_offer(env):
    s, r = run(env, MARGARET, "Actually I want to ask about CL-3001")
    assert s.pending == "switch_offer" and s.phase == Phase.RESOLVE_INTENT and s.verified
    reply = r[1]["reply"]
    assert "Which would you prefer" in reply
    for leak in ("Ma", "Tian", "P12", "diagnosis", "exists", "doesn't exist"):
        assert leak not in reply.replace("Margaret", "")
    gate = [e for e in r[1]["events"] if e.get("name") == "claim_ownership"]
    assert gate and gate[0]["passed"] is False


def test_unknown_claim_gets_the_same_offer(env):
    s, r = run(env, MARGARET, "Actually I want to ask about CL-9999")
    assert s.pending == "switch_offer"


def test_frustration_shown_hands_off_without_an_offer(env):
    s = verified_margaret(env)
    s.emotion_log.append({"turn": 1, "readings": {"frustration": {"level": "strong", "words": "ridiculous"}}})
    s, r = run(env, "Actually I want to ask about CL-3001", session=s)
    assert s.phase == Phase.HANDOFF and s.handoff_packet["reason"] == "other_identity_claim"
    assert "Which would you prefer" not in r[0]["reply"]


def test_switch_to_the_claim_owner_clears_the_caller(env):
    s, r = run(env, MARGARET, "Actually I want to ask about CL-3001", "verify as the owner")
    assert s.phase == Phase.VERIFY_ID and not s.verified and s.customer_id is None
    assert s.identity == {} and s.pending is None
    assert s.memory["claim_id_hint"].value == "CL-3001"
    assert "id.dob" not in s.memory
    # The owner passes the check; the remembered claim is then proposed for confirmation.
    s, r = run(env, MA_TIAN, session=s)
    assert s.verified and s.customer_id == "P12"
    assert s.proposed_claim_id == "CL-3001"


def test_human_choice_hands_off(env):
    s, r = run(env, MARGARET, "Actually I want to ask about CL-3001", "a person please")
    assert s.phase == Phase.HANDOFF and s.handoff_packet["reason"] == "other_identity_claim"


def test_second_request_after_the_offer_hands_off(env):
    s, r = run(env, MARGARET, "Actually I want to ask about CL-3001", "no, never mind",
               "What about CL-3001 again?")
    assert s.phase == Phase.HANDOFF and s.handoff_packet["reason"] == "other_identity_claim"


# ---- 7d: the failure count is per call ---------------------------------------------------


def test_failure_count_survives_a_switch(env):
    s, r = run(env, WRONG)
    assert s.counters["verify_failures"] == 1
    s, r = run(env, WRONG, session=s)
    assert s.counters["verify_failures"] == 2
    s, r = run(env, MARGARET, "Actually I want to ask about CL-3001", "verify as the owner", session=s)
    assert s.phase == Phase.VERIFY_ID and s.counters["verify_failures"] == 2
    s, r = run(env, "My name is Ma Tian, DOB 1964-09-11, SSN last four 6688, phone 650-208-8799",
               session=s)
    assert s.counters["verify_failures"] == 3 and s.phase == Phase.HANDOFF
