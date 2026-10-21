"""Read-only data access over the assessment's starter fixtures.

The fixture files are used as shipped (app/data/starter/ is a verbatim copy of
starter/apps/insurance_claims/fixtures/). Every claim read is scoped to the verified
policyholder.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import PII_FIELDS

DEFAULT_DATA_DIR = Path(__file__).parent / "data" / "starter"

# The four money fields in claims.json (decimal strings, see claim_schema.json).
MONEY_FIELDS = ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee")


def _norm_name(v: str) -> str:
    # Letters only, so "Ya Wen Li", "Yawen Li" and "Ya-Wen Li" compare equal.
    return re.sub(r"[^a-z]", "", v.lower())


def _digits(v: str) -> str:
    return re.sub(r"\D", "", v)


_DOB_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%B %d %Y")


def norm_dob(v: str) -> str:
    v = v.strip()
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return v


NORMALIZERS = {
    "full_name": _norm_name,
    "dob": norm_dob,
    "phone": lambda v: _digits(v)[-10:],
    "email": lambda v: v.strip().strip("<>\"'").rstrip(".,;:!?").lower(),
    "id_last4": lambda v: _digits(v)[-4:],
}

# Every value on file that proves a field, aliases included. An alias is the same
# person (e.g. a speech-to-text spelling of the name), so it counts as a match.
RECORD_VALUES = {
    "full_name": lambda p: [p["name"], *p.get("name_aliases", [])],
    "dob": lambda p: [p["dob"]],
    "phone": lambda p: [p["phone"], *p.get("phone_aliases", [])],
    "email": lambda p: [p["email"], *p.get("email_aliases", [])],
    "id_last4": lambda p: [p["id_last4"]],
}

ID_TYPE_LABELS = {"ssn_last4": "SSN", "national_id_last4": "national ID"}


@dataclass
class VerificationResult:
    verified: bool
    customer_id: str | None
    matched: list[str]
    mismatched: int


class DataStore:
    def __init__(self, data_dir: str | Path | None = None):
        d = Path(data_dir or os.environ.get("SOP_DATA_DIR") or DEFAULT_DATA_DIR)

        def load(name: str) -> Any:
            return json.loads((d / name).read_text())

        self.customers: dict[str, dict] = {p["party_id"]: p for p in load("policyholders.json")}
        self.claims: dict[str, dict] = {c["case_id"]: c for c in load("claims.json")}
        self.representatives: list[dict] = load("representatives.json")
        self.consent_scenarios: dict[str, dict] = load("consent_scenarios.json")
        self.guideline: dict[str, Any] = load("required_document_guideline.json")
        self.claim_schema: dict[str, Any] = load("claim_schema.json")
        self.today = self._today()

    def _today(self) -> date:
        """The date deadlines are judged against.

        SOP_TODAY=YYYY-MM-DD pins it and SOP_TODAY=real uses the system clock. By default
        it is the day after the newest record in the fixtures, so the sample data reads
        as current (on the real date every appeal deadline in it has passed).
        """
        raw = os.environ.get("SOP_TODAY", "").strip().lower()
        if raw == "real":
            return date.today()
        if raw:
            return date.fromisoformat(raw)
        newest = max(date.fromisoformat(c["created_at"]) for c in self.claims.values())
        return newest + timedelta(days=1)

    # ---- identity -------------------------------------------------------

    def verify_identity(self, provided: dict[str, str]) -> VerificationResult:
        """Verified iff at least 3 of the 5 PII fields match one policyholder and none conflict.

        Matches never combine across people. A policy number only breaks ties between
        equally good candidates: it never counts toward the 3 and never vetoes.
        """
        pii = {k: NORMALIZERS[k](v) for k, v in provided.items() if k in PII_FIELDS and v}
        policy = (provided.get("policy_id") or "").strip().upper() or None
        if not pii:
            return VerificationResult(False, None, [], 0)

        def matched_fields(p: dict) -> list[str]:
            return [
                k for k, v in pii.items()
                if v in {NORMALIZERS[k](x) for x in RECORD_VALUES[k](p)}
            ]

        def score(p: dict) -> tuple[int, int]:
            return len(matched_fields(p)), int(policy == p["policy_number"])

        best = max(self.customers.values(), key=score)
        matched = matched_fields(best)
        if not matched:
            return VerificationResult(False, None, [], len(pii))
        mismatched = len(pii) - len(matched)
        verified = len(matched) >= 3 and mismatched == 0
        return VerificationResult(verified, best["party_id"], matched, mismatched)

    def customer(self, customer_id: str) -> dict:
        return self.customers[customer_id]

    def first_name(self, customer_id: str) -> str:
        return self.customers[customer_id]["name"].split()[0]

    def id_label(self, customer_id: str) -> str:
        return ID_TYPE_LABELS.get(self.customers[customer_id]["id_type"], "ID")

    # ---- claims (always ownership-checked) -------------------------------

    def list_claims(self, customer_id: str) -> list[dict]:
        return [
            {k: c[k] for k in ("case_id", "case_type", "summary", "created_at", "status")}
            for c in self.claims.values()
            if c["party_id"] == customer_id
        ]

    def get_claim(self, customer_id: str, case_id: str) -> dict | None:
        claim = self.claims.get((case_id or "").strip().upper())
        if claim is None or claim["party_id"] != customer_id:
            return None
        return claim

    def policy_number(self, customer_id: str) -> str:
        return self.customers[customer_id]["policy_number"]

    def amount_meanings(self) -> dict[str, str]:
        fields = self.claim_schema.get("field_descriptions", {})
        return {k: fields[k]["description"] for k in MONEY_FIELDS if k in fields}

    # ---- for the pre-verification disclosure filter ----------------------

    def sensitive_claim_strings(self) -> set[str]:
        """Claim-specific values in the dataset, for leak detection before verification."""
        out: set[str] = set()
        for c in self.claims.values():
            out.add(c["case_id"].lower())
            out.add(c["summary"].lower())
            if c.get("denial_reason"):
                out.add(c["denial_reason"].lower())
            for f in MONEY_FIELDS:
                amt = Decimal(c.get(f) or "0")
                if amt:
                    out.update({f"{amt:,.2f}", f"{amt:.2f}"})
        return out
