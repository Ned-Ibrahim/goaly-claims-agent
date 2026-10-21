"""Date logic the model must not do on its own."""

from __future__ import annotations

from datetime import date


def appeal_deadline_state(claim: dict, today: date) -> str | None:
    """Code, not the model, decides whether a deadline is still open."""
    raw = claim.get("appeal_deadline")
    if not raw:
        return None
    return "passed" if date.fromisoformat(raw) < today else "open"


def days_until_deadline(claim: dict, today: date) -> int | None:
    raw = claim.get("appeal_deadline")
    if not raw:
        return None
    return (date.fromisoformat(raw) - today).days
