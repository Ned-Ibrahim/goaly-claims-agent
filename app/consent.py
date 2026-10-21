"""Policyholder consent for a representative (decisions 8a to 8e).

The starter data simulates asking the policyholder: a named scenario holds the answer the
agent sees on each successive check. Consent covers one named claim and one call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .store import DEFAULT_DATA_DIR, _norm_name

# Pending checks in a row that count as a timeout (8c); matches the starter's `timeout` scenario.
CONSENT_TIMEOUT_CHECKS = 5

# Relationship words that name the same bond from the caller's side.
_RELATION_GROUPS = (
    {"son", "daughter", "child"},
    {"mother", "father", "parent"},
    {"husband", "wife", "spouse", "partner"},
    {"brother", "sister", "sibling"},
)


def scenario_name() -> str:
    return os.environ.get("CLAIMS_CONSENT_SCENARIO", "").strip() or "default"


def _scenarios() -> dict[str, Any]:
    d = Path(os.environ.get("SOP_DATA_DIR") or DEFAULT_DATA_DIR)
    return json.loads((d / "consent_scenarios.json").read_text())


def consent_status(scenario: str, checks_so_far: int) -> str:
    """The answer on the next check, after `checks_so_far` checks: "pending" or "approved"."""
    seq = _scenarios()[scenario]["status_sequence"]
    return seq[min(max(checks_so_far, 0), len(seq) - 1)]


def _relation_key(rel: str) -> str:
    rel = rel.strip().lower()
    for group in _RELATION_GROUPS:
        if rel in group:
            return "|".join(sorted(group))
    return rel


def is_listed(reps: list[dict], rep_name: str | None, relationship: str | None) -> bool:
    """True when the caller's name and relationship match a representative on file."""
    if not rep_name or not relationship:
        return False
    return any(
        _norm_name(r["rep_name"]) == _norm_name(rep_name)
        and _relation_key(r["relationship"]) == _relation_key(relationship)
        for r in reps
    )


def same_name(a: str, b: str) -> bool:
    return _norm_name(a) == _norm_name(b)
