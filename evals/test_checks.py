"""Unit tests for checks.py. Run: python3 -m unittest discover -s evals"""
from __future__ import annotations

import unittest

import checks

DB = {
    "customers": [
        {"customer_id": "C1", "full_name": "Margaret Chen", "dob": "1985-03-15", "phone": "555-214-8890",
         "email": "m@example.com", "ssn_last4": "4472", "policy_ids": ["P1"]},
        {"customer_id": "C2", "full_name": "David Okafor", "dob": "1979-11-02", "phone": "555-310-4471",
         "email": "d@example.com", "ssn_last4": "1180", "policy_ids": ["P2"]},
    ],
    "policies": [
        {"policy_id": "P1", "customer_id": "C1", "notes": "Prior authorization required for MRI."},
        {"policy_id": "P2", "customer_id": "C2", "notes": "Collision deductible $500."},
    ],
    "claims": [
        {"claim_id": "CLM-30418", "policy_id": "P1", "description": "Outpatient MRI, left knee",
         "provider": "Bayview Imaging Center", "service_date": "2026-01-14", "submitted_date": "2026-01-20",
         "amount_billed": 2350.0, "amount_paid": 0.0, "status": "denied", "denial_code": "PA-01",
         "appeal_deadline": "2026-07-20"},
        {"claim_id": "CLM-22311", "policy_id": "P2", "description": "Basement water damage",
         "provider": "DryRight Restoration", "service_date": "2026-01-09", "submitted_date": "2026-01-15",
         "amount_billed": 6400.0, "amount_paid": 0.0, "status": "denied", "denial_code": "WD-03"},
    ],
    "knowledge_base": {"appeals": ["Appeals are decided within 30 days."]},
}


class LeakTests(unittest.TestCase):
    def setUp(self):
        self.tokens = checks.claim_fact_tokens(checks.claims_for(DB))

    def test_flags_disclosed_facts(self):
        reply = "Your MRI claim CLM-30418 for $2,350 was denied with code PA-01 on January 20."
        leaks = checks.find_leaks(reply, self.tokens, "my denied healthcare claim from January")
        for want in ("mri", "clm-30418", "2,350", "pa-01", "january 20"):
            self.assertIn(want, leaks)

    def test_echoing_the_caller_is_not_a_leak(self):
        reply = "I understand you're calling about a denied healthcare claim from January and CLM-30418."
        self.assertEqual(checks.find_leaks(reply, self.tokens, "about CLM-30418, denied healthcare, January"), [])

    def test_word_boundaries(self):
        # "primary" must not match "mri"; "watering" must not match "water".
        self.assertEqual(checks.find_leaks("Is this your primary line? I'm watering plants.", self.tokens, ""), [])

    def test_zero_amounts_are_not_tokens(self):
        self.assertEqual(checks.money_forms(0.0), [])


class GroundingTests(unittest.TestCase):
    def test_own_facts_are_grounded(self):
        allowed = checks.grounding_set(DB, "C1")
        reply = "CLM-30418 (code PA-01) for $2,350.00 from January 14 can be appealed until 2026-07-20, within 30 days."
        self.assertEqual(checks.ungrounded(reply, allowed, ""), [])

    def test_invented_and_cross_customer_facts_are_flagged(self):
        allowed = checks.grounding_set(DB, "C1")
        reply = "You were billed $1,999 and CLM-22311 code WD-03 was decided on March 3."
        self.assertEqual(sorted(checks.ungrounded(reply, allowed, "")), ["$1,999", "CLM-22311", "March 3", "WD-03"])

    def test_caller_stated_facts_are_allowed(self):
        allowed = checks.grounding_set(DB, "C1")
        self.assertEqual(checks.ungrounded("You said you paid $40 on Feb 2.", allowed, "I paid $40 on feb 2nd"), [])

    def test_policy_number_is_not_a_code(self):
        self.assertEqual(checks.extract_facts("policy POL-9921"), [])


class RawIdentityTests(unittest.TestCase):
    def test_common_spellings_are_covered(self):
        raw = checks.raw_identity_values(DB)
        for spelling in ("4472", "1985-03-15", "15 March 1985", "March 15, 1985", "03-15-1985", "3/15/1985",
                         "555-214-8890", "(555) 214-8890", "555 214 8890", "5552148890"):
            self.assertIn(spelling, raw)


class EventTests(unittest.TestCase):
    def test_subset_and_any_of(self):
        e = {"type": "emotion", "label": "angry", "extra": 1}
        self.assertTrue(checks.match_event(e, {"type": "emotion", "label": ["angry", "frustrated"]}))
        self.assertFalse(checks.match_event(e, {"type": "emotion", "label": "anxious"}))
        self.assertEqual(checks.find_event([{"type": "x"}, e], {"label": "angry"}), 1)


if __name__ == "__main__":
    unittest.main()
