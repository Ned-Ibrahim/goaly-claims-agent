"""Unit tests for the eval helpers. No server and no network.

Run from the repo root: uv run pytest -q evals/test_checks.py (or python3 -m unittest discover -s evals).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import checks
import run_evals
import scenarios
import simulate

DB = {
    "policyholders": [
        {"party_id": "P9", "name": "Margaret Chen", "policy_number": "POL-9921", "dob": "1985-03-15",
         "id_type": "ssn_last4", "id_last4": "4472", "phone": "+16505212836", "email": "margaret@email.com"},
        {"party_id": "P12", "name": "Ma Tian", "policy_number": "POL-8836", "dob": "1964-09-10",
         "id_type": "national_id_last4", "id_last4": "6688", "phone": "+16502088799", "email": "matian@example.com",
         "email_aliases": ["tian.ma@example.com"]},
    ],
    "claims": [
        {"case_id": "CL-2048", "party_id": "P9", "case_type": "healthcare", "created_at": "2026-01-12",
         "status": "denied", "summary": "Healthcare claim denied due to missing pathology report and office note",
         "denial_reason": "the review file did not include the pathology report and the treating provider office note",
         "documents_needed": ["pathology report", "office note"], "appeal_deadline": "2026-03-18",
         "expected_reimbursement_amount": "0.00", "allowed_max_amount": "1450.00", "net_pay": "0.00",
         "net_fee": "1450.00"},
        {"case_id": "CL-3001", "party_id": "P12", "case_type": "healthcare", "created_at": "2026-03-01",
         "status": "denied", "summary": "Healthcare claim denied because the diagnosis report is missing",
         "documents_needed": ["diagnosis report"], "appeal_deadline": "2026-04-15",
         "expected_reimbursement_amount": "0.00", "allowed_max_amount": "1200.00", "net_pay": "0.00",
         "net_fee": "1200.00"},
    ],
    "representatives": [{"rep_name": "David Chen", "relationship": "son", "buyer_name": "Margaret Chen",
                         "buyer_party_id": "P9"}],
    "guideline": {"default_guidance": {"en": "Use the member portal first."}},
    "schema": {},
}


class LeakTests(unittest.TestCase):
    def setUp(self):
        self.tokens = checks.claim_fact_tokens(checks.claims_for(DB))

    def test_flags_disclosed_facts(self):
        reply = "Claim CL-2048 was denied: the pathology report is missing. Net fee is $1,450.00, appeal by March 18."
        leaks = checks.find_leaks(reply, self.tokens, "my denied healthcare claim from January")
        for want in ("cl-2048", "pathology report", "1,450.00", "march 18"):
            self.assertIn(want, leaks)

    def test_echoing_the_caller_is_not_a_leak(self):
        reply = "I understand you're calling about CL-2048."
        self.assertEqual(checks.find_leaks(reply, self.tokens, "about CL-2048"), [])

    def test_other_people_claims_are_tokens_too(self):
        self.assertIn("diagnosis report", self.tokens)
        self.assertIn("cl-3001", self.tokens)

    def test_zero_amounts_are_not_tokens(self):
        self.assertEqual(checks.money_forms("0.00"), [])
        self.assertIn("1,450.00", checks.money_forms("1450.00"))


class GroundingTests(unittest.TestCase):
    def test_own_facts_are_grounded(self):
        allowed = checks.grounding_set(DB, "P9")
        reply = "CL-2048 allows up to $1,450.00 and you can appeal until 2026-03-18."
        self.assertEqual(checks.ungrounded(reply, allowed, ""), [])

    def test_invented_and_cross_person_facts_are_flagged(self):
        allowed = checks.grounding_set(DB, "P9")
        reply = "You were billed $1,999 and CL-3001 was decided on March 3."
        self.assertEqual(sorted(checks.ungrounded(reply, allowed, "")), ["$1,999", "CL-3001", "March 3"])

    def test_caller_stated_facts_are_allowed(self):
        allowed = checks.grounding_set(DB, "P9")
        self.assertEqual(checks.ungrounded("You said you paid $40 on Feb 2.", allowed, "I paid $40 on feb 2nd"), [])

    def test_policy_number_is_not_a_claim(self):
        self.assertEqual(checks.extract_facts("policy POL-9921"), [])


class RawIdentityTests(unittest.TestCase):
    def test_common_spellings_are_covered(self):
        raw = checks.raw_identity_values(DB)
        for spelling in ("4472", "1985-03-15", "15 March 1985", "March 15, 1985", "03-15-1985", "3/15/1985",
                         "+16505212836", "650-521-2836", "(650) 521-2836", "650 521 2836", "6505212836",
                         "margaret@email.com", "tian.ma@example.com"):
            self.assertIn(spelling, raw)


class EventTests(unittest.TestCase):
    def test_subset_and_any_of(self):
        e = {"type": "emotion", "label": "angry", "extra": 1}
        self.assertTrue(checks.match_event(e, {"type": "emotion", "label": ["angry", "frustrated"]}))
        self.assertFalse(checks.match_event(e, {"type": "emotion", "label": "anxious"}))
        self.assertEqual(checks.find_event([{"type": "x"}, e], {"label": "angry"}), 1)

    def test_tool_allowlist(self):
        self.assertTrue(checks.tool_allowed("get_claim"))
        self.assertTrue(checks.tool_allowed("check_consent"))
        self.assertFalse(checks.tool_allowed("delete_claim"))


class FixtureTests(unittest.TestCase):
    def test_starter_data_matches_the_scenario_constants(self):
        starter = Path(__file__).resolve().parent.parent / "app" / "data" / "starter"
        if not starter.exists():
            self.skipTest("starter data not present")
        db = checks.load_fixture(starter)
        self.assertEqual(checks.customer(db, scenarios.MARGARET)["name"], "Margaret Chen")
        self.assertEqual(checks.customer(db, scenarios.MA_TIAN)["name"], "Ma Tian")
        ids = {c["case_id"] for c in checks.claims_for(db, scenarios.MARGARET)}
        self.assertTrue({"CL-2048", "CL-2011"} <= ids)
        self.assertEqual({c["case_id"] for c in checks.claims_for(db, scenarios.MA_TIAN)}, {"CL-3001"})
        self.assertEqual(checks.rep_for(db, scenarios.MARGARET)["rep_name"], "David Chen")

    def test_load_fixture_reads_a_directory(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "claims.json").write_text("[]")
            self.assertEqual(checks.load_fixture(d)["claims"], [])


class ScenarioShapeTests(unittest.TestCase):
    def test_names_are_unique_and_complete(self):
        names = [s["name"] for s in scenarios.SCENARIOS + scenarios.SIM_SCENARIOS]
        self.assertEqual(len(names), len(set(names)))
        for sc in scenarios.SCENARIOS:
            self.assertTrue(sc["turns"] and sc["covers"] and sc["customer"], sc["name"])
        for sc in scenarios.SIM_SCENARIOS:
            for key in ("persona", "facts", "goal", "behavior", "grade", "covers", "customer"):
                self.assertIn(key, sc, sc["name"])

    def test_the_nine_memory_cases_are_present(self):
        covered = {c for s in scenarios.SCENARIOS for c in s["covers"]}
        for n in range(1, 9):
            self.assertIn(f"MEM{n}", covered)
        self.assertTrue({"MEM1", "MEM3", "MEM4", "MEM5", "MEM6"} <= {c for s in scenarios.SIM_SCENARIOS for c in s["covers"]})

    def test_consent_scenarios_are_selected_by_server_setting(self):
        default = {s["name"] for s in run_evals.select(None, "fake", "default")}
        timeout = {s["name"] for s in run_evals.select(None, "fake", "timeout")}
        self.assertNotIn("rep_consent_timeout", default)
        self.assertEqual(timeout, {"rep_consent_timeout"})


class StubClient:
    """Serves canned /api/chat responses so the runner logic is tested without a server."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def reset(self, session_id): pass
    def outbox(self, session_id): return []
    def state(self, session_id): return {"phase": self.last["phase"], "verified": self.last["verified"]}

    def chat(self, session_id, message, channel="text"):
        self.sent.append((message, channel))
        self.last = self.responses.pop(0)
        return self.last


def resp(phase="VERIFY_ID", verified=False, reply="", events=None, **extra):
    base = {"phase": phase, "verified": verified, "reply": reply, "events": events or [], "memory": {},
            "counters": {}, "transcript": [], "ended": False}
    base.update(extra)
    return base


VERIFIED_EVENTS = [
    {"type": "tool_call", "name": "verify_identity"},
    {"type": "gate", "name": "identity_verified", "passed": True},
    {"type": "transition", "from": "VERIFY_ID", "to": "RESOLVE_INTENT"},
]


class RunnerTests(unittest.TestCase):
    def run_sc(self, sc, responses):
        with tempfile.TemporaryDirectory() as d:
            client = StubClient(responses)
            return run_evals.run_scenario(sc, client, DB, Path(d)), client

    def sc(self, turns, **extra):
        return {"name": "t", "covers": ["X1"], "customer": "P9", "turns": turns, **extra}

    def test_passing_script(self):
        res, client = self.run_sc(
            self.sc([{"say": "hi", "expect": {"phase": "VERIFY_ID", "verified": False}}]), [resp()])
        self.assertEqual(res.status, "PASS")

    def test_voice_channel_is_sent(self):
        _, client = self.run_sc(self.sc([{"say": "hi", "channel": "voice", "expect": {}}]), [resp()])
        self.assertEqual(client.sent, [("hi", "voice")])

    def test_claim_fact_before_verification_fails(self):
        res, _ = self.run_sc(self.sc([{"say": "hi", "expect": {}}]), [resp(reply="Your claim CL-2048 was denied.")])
        self.assertEqual(res.status, "FAIL")

    def test_claim_read_before_gate_fails(self):
        events = [{"type": "tool_call", "name": "list_claims"}]
        res, _ = self.run_sc(self.sc([{"say": "hi", "expect": {}}]), [resp(events=events)])
        self.assertEqual(res.status, "FAIL")

    def test_raw_identity_in_transcript_fails(self):
        transcript = [{"role": "user", "content": "my dob is 1985-03-15"}]
        res, _ = self.run_sc(self.sc([{"say": "hi", "expect": {}}]), [resp(transcript=transcript)])
        self.assertEqual(res.status, "FAIL")

    def test_until_repeats_and_fails_when_never_reached(self):
        turn = {"say": "any news?", "until": {"consent": "approved"}, "max": 3, "expect": {}}
        res, client = self.run_sc(self.sc([turn], rep=True), [resp(), resp(), resp()])
        self.assertEqual(len(client.sent), 3)
        self.assertEqual(res.status, "FAIL")
        ok, client = self.run_sc(self.sc([turn], rep=True), [resp(), resp(consent_status="approved")])
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(ok.status, "PASS")

    def test_rep_claim_read_needs_consent(self):
        read = [{"type": "tool_call", "name": "get_claim"}]
        turns = [{"say": "a", "expect": {}}]
        res, _ = self.run_sc(self.sc(turns, rep=True), [resp("RESOLVE_INTENT", True, events=VERIFIED_EVENTS + read)])
        self.assertEqual(res.status, "FAIL")
        res, _ = self.run_sc(self.sc(turns, rep=True),
                             [resp("RESOLVE_INTENT", True, events=VERIFIED_EVENTS + read, consent_status="approved")])
        self.assertEqual(res.status, "PASS")

    def test_switch_turn_may_return_to_verify(self):
        turns = [{"say": "a", "expect": {}}, {"say": "b", "switch": True, "expect": {"verified": False}}]
        back = [{"type": "transition", "from": "RESOLVE_INTENT", "to": "VERIFY_ID"}]
        res, _ = self.run_sc(self.sc(turns), [resp("RESOLVE_INTENT", True, events=VERIFIED_EVENTS), resp(events=back)])
        self.assertEqual(res.status, "PASS")
        turns[1].pop("switch")
        res, _ = self.run_sc(self.sc(turns), [resp("RESOLVE_INTENT", True, events=VERIFIED_EVENTS), resp(events=back)])
        self.assertEqual(res.status, "FAIL")

    def test_final_checks_use_events_from_the_whole_call(self):
        sc = self.sc([{"say": "a", "expect": {}}, {"say": "b", "expect": {}}],
                     final={"events": [{"type": "gate", "name": "identity_verified"}]})
        res, _ = self.run_sc(sc, [resp("RESOLVE_INTENT", True, events=VERIFIED_EVENTS), resp("RESOLVE_INTENT", True)])
        self.assertEqual(res.status, "PASS")

    def test_either_counter_name_counts(self):
        exp = {"counters_min": {"refusals|resistance": 2}}
        res = run_evals.Result()
        run_evals.check_expect(exp, resp(counters={"resistance": 2}), [], ["X"], res, "FAIL", "t")
        self.assertEqual(res.items, [])


class SimulatorTests(unittest.TestCase):
    def test_caller_loop_ends_on_marker(self):
        said = iter(["hello there", "[END]"])
        caller = simulate.Caller(lambda system, messages: next(said), scenarios.SIM_SCENARIOS[0], 0)
        self.assertEqual(caller.next_message(None), "hello there")
        self.assertIsNone(caller.next_message("How can I help?"))

    def test_system_prompt_holds_persona_facts_and_a_run_specific_style(self):
        spec = scenarios.SIM_SCENARIOS[0]
        one, two = simulate.build_system(spec, simulate.style_for(0)), simulate.build_system(spec, simulate.style_for(1))
        self.assertIn("Margaret Chen", one)
        self.assertNotEqual(one, two)

    def test_simulated_run_is_graded_on_state(self):
        sc = {"name": "s", "covers": ["MEM1"], "customer": "P9", "persona": "p", "facts": "f", "goal": "g",
              "behavior": "b", "grade": {"verified": True, "events": [{"type": "gate", "name": "identity_verified"}]}}
        said = iter(["I need help", "My name is Margaret Chen", "[END]"])
        with tempfile.TemporaryDirectory() as d:
            client = StubClient([resp(), resp("RESOLVE_INTENT", True, events=VERIFIED_EVENTS)])
            res = run_evals.run_simulated(sc, client, DB, Path(d), lambda system, messages: next(said), 0)
        self.assertEqual(res.status, "PASS")
        with tempfile.TemporaryDirectory() as d:
            said = iter(["I need help", "[END]"])
            res = run_evals.run_simulated(sc, StubClient([resp()]), DB, Path(d), lambda system, messages: next(said), 1)
        self.assertEqual(res.status, "FAIL")

    def test_no_key_skips_with_exit_zero(self):
        import io
        import os
        import sys
        from contextlib import redirect_stdout
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        saved_argv = sys.argv
        try:
            sys.argv = ["run_evals.py", "--simulate"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_evals.main()
        finally:
            sys.argv = saved_argv
            if old is not None:
                os.environ["ANTHROPIC_API_KEY"] = old
        self.assertEqual(code, 0)
        self.assertIn("SKIP", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
