"""Passive cross-phase memory.

Writes happen on every caller turn, in every phase. The writer has no access to the
phase or the gates, so remembering something can never move the workflow; the SOP
controller decides when a remembered slot may be used.
"""

from __future__ import annotations

import re
from typing import Any

from .models import CaseRecord, Extraction, MemorySlot, Session


def mask(field: str, value: str) -> str:
    if field == "id_last4":
        return "***" + value[-1:]
    if field == "dob":
        return "****-**-" + value[-2:]
    if field == "phone":
        digits = "".join(ch for ch in value if ch.isdigit())
        return "***-***-" + digits[-4:]
    if field in ("email", "email_address"):
        local, _, domain = value.partition("@")
        return (local[:1] + "***@" + domain) if domain else "***"
    if field == "full_name":
        parts = value.split()
        return " ".join([parts[0]] + [p[:1] + "." for p in parts[1:]]) if parts else value
    return value


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_REDACTIONS = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b"), "[date]"),
    (re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.IGNORECASE), "[date]"),
    (re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH},?\s+\d{{4}}\b", re.IGNORECASE), "[date]"),
    (re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"), "[phone]"),
    (re.compile(r"((?:ssn|social|national\s+id|id\s+number|last\s+(?:four|4))\D{0,25})\d{4}\b", re.IGNORECASE), r"\1[id]"),
)


def redact(text: str, ex: Extraction, phase: str = "") -> str:
    """Strip identity values from a caller quote before it is stored anywhere."""
    for f, tag in (("full_name", "[name]"), ("email", "[email]"), ("phone", "[phone]"),
                   ("dob", "[date]"), ("id_last4", "[id]")):
        v = (getattr(ex, f) or "").strip()
        if len(v) >= 4:
            text = re.sub(re.escape(v), tag, text, flags=re.IGNORECASE)
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    if phase == "VERIFY_ID":
        # Safety net while identity details are flowing: no 4+ digit run survives, except
        # inside policy or claim numbers.
        text = re.sub(r"(?<![A-Z]-)(?<![\d])\d{4,}\b", "[number]", text)
    return text


IDENTITY_SLOTS = ("full_name", "dob", "phone", "email", "id_last4", "policy_id")
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _put(session: Session, slot: str, value: Any, quote: str) -> str | None:
    current = session.memory.get(slot)
    if current is not None and current.value == value:
        return None
    session.memory[slot] = MemorySlot(value, session.turn, session.phase.value, quote)
    return slot


def write(session: Session, ex: Extraction, utterance: str) -> list[str]:
    """Store every high-signal fact the caller just gave. Returns the slots written."""
    quote = redact(utterance.strip(), ex, session.phase.value)[:200]
    written: list[str | None] = []

    for f in IDENTITY_SLOTS:
        v = getattr(ex, f)
        if v:
            session.identity[f] = v.strip()
            shown = v.strip().upper() if f == "policy_id" else mask(f, v.strip())
            written.append(_put(session, f"id.{f}", shown, "[identity detail, not stored]"))

    if ex.intent not in ("none", "other_in_scope"):
        written.append(_put(session, "intent", ex.intent, quote))
    if ex.claim_type:
        written.append(_put(session, "claim_type", ex.claim_type, quote))
    if ex.claim_month and 1 <= ex.claim_month <= 12:
        written.append(_put(session, "claim_month", MONTHS[ex.claim_month - 1], quote))
    if ex.claim_year:
        written.append(_put(session, "claim_year", ex.claim_year, quote))
    if ex.claim_status_hint:
        written.append(_put(session, "claim_status_hint", ex.claim_status_hint, quote))
    if ex.claim_id:
        written.append(_put(session, "claim_id_hint", ex.claim_id.strip().upper(), quote))
    if ex.email_address_for_summary:
        # Masked form only: the raw address is never stored (V2 sends to the address on file).
        written.append(
            _put(session, "email_address", mask("email", ex.email_address_for_summary.strip().lower()), quote)
        )
    if ex.lacks_document and session.phase.value == "VERIFY_ID":
        written.append(_put(session, "deferred_lacks_document", True, quote))
    if ex.question and session.phase.value == "VERIFY_ID" and (
        ex.intent != "none" or ex.claim_id or ex.claim_type or ex.claim_status_hint
    ):
        # A claim question asked before verification: kept so it is answered later, not now.
        written.append(_put(session, "deferred_question", redact(ex.question, ex, "VERIFY_ID"), quote))
    if ex.question:
        session.case.caller_questions.append(redact(ex.question, ex, session.phase.value))

    return [s for s in written if s]


def clear_identity(session: Session) -> None:
    """After a failed check, start over so a stale wrong value can't block verification."""
    for f in IDENTITY_SLOTS:
        if f != "policy_id":
            session.identity.pop(f, None)
            session.memory.pop(f"id.{f}", None)
    session.identity_attempted = None


def forget_id_digits(session: Session) -> None:
    """After verification the SSN or national ID digits have done their job; drop them."""
    session.identity.pop("id_last4", None)
    slot = session.memory.get("id.id_last4")
    if slot is not None:
        slot.value = "verified (discarded)"


def hint(session: Session, slot: str) -> Any:
    s = session.memory.get(slot)
    return s.value if s else None


def month_number(name: str | None) -> int | None:
    return MONTHS.index(name) + 1 if name in MONTHS else None


def snapshot(session: Session) -> dict[str, dict]:
    return {k: v.as_dict() for k, v in session.memory.items()}


# ---- V2 contracts --------------------------------------------------------------

# Slots the reply model may see, and what must hold first. Identity slots ("id.*") and
# email_address are never rendered. Slots hold no verified status, phase or permissions.
_USABLE_WHEN_VERIFIED = (
    "intent", "claim_type", "claim_month", "claim_year",
    "claim_status_hint", "claim_id_hint", "deferred_question",
)


def _usable(s: Session, slot: str) -> bool:
    return slot in _USABLE_WHEN_VERIFIED and s.verified


def known_so_far(s: Session) -> dict[str, Any]:
    """Only the slots usable now, as {slot: {"value", "words"}}."""
    return {
        k: {"value": v.value, "words": v.quote}
        for k, v in s.memory.items()
        if _usable(s, k)
    }


def recent_turns(s: Session, n: int = 4) -> list[dict[str, str]]:
    """Last n masked entries of the current person's conversation, starting with a user turn."""
    turns = list(s.transcript[s.window_start:])[-n:]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def clear_person(s: Session, keep_claim_id: str | None = None) -> None:
    """Switch of person: forget the old caller, keep the failure count."""
    s.identity.clear()
    s.identity_attempted = None
    s.verified = False
    s.customer_id = None
    s.matched_fields = []
    s.memory.clear()
    s.proposed_claim_id = None
    s.selected_claim_id = None
    s.path = None
    s.pending = None
    s.readback_fields = []
    s.memory_hints_used = False
    s.case = CaseRecord()
    s.caller_role = "policyholder"
    s.rep_name = None
    s.rep_relationship = None
    s.consent_claim_id = None
    s.consent_status = None
    s.consent_checks = 0
    s.window_start = len(s.transcript)
    if keep_claim_id:
        _put(s, "claim_id_hint", keep_claim_id.strip().upper(), "[claim the caller asked for]")
