"""The SOP controller: a deterministic state machine that owns phase, gates and actions.

Each caller turn arrives here as an Extraction (what the caller said, as structured
fields). The controller updates state, runs the tools it allows in the current phase,
and returns a Plan: the instructions and the only data the responder LLM may use
for this turn. The LLM never changes the phase, never calls a tool, and never sees
data the current phase does not allow.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import consent, emotion, guidelines, memory, trace
from .deadlines import appeal_deadline_state, days_until_deadline
from .emailer import compose_summary
from .models import (
    EMOTIONS,
    MAX_VERIFY_FAILURES,
    OFF_TOPIC_HANDOFF,
    OFF_TOPIC_OFFER_HUMAN,
    PATHS,
    PII_FIELDS,
    RESISTANCE_HANDOFF,
    RESISTANCE_OFFER_HUMAN,
    Extraction,
    Phase,
    Session,
)
from .store import MONEY_FIELDS, DataStore, norm_dob

# Off by default: per-attempt match counts would tell a caller how many details were right.
DEBUG_TRACE = os.environ.get("SOP_DEBUG_TRACE") == "1"

FIELD_LABELS = {
    "full_name": "full name",
    "dob": "date of birth",
    "phone": "phone number on file",
    "email": "email on file",
    "id_last4": "last four digits of the SSN or national ID",
}

FALLBACKS = {
    Phase.VERIFY_ID: (
        "To protect your account, I need to verify your identity before we talk about any "
        "claim details. Could you share any three of these: your full name, date of birth, "
        "phone number, email, or the last four digits of your SSN or national ID?"
    ),
    Phase.RESOLVE_INTENT: "Thanks for that. Which claim are you calling about today?",
    Phase.PROCESS_CASE: (
        "I'm sorry, I couldn't put that answer together reliably from your claim record. "
        "Would you like me to connect you with a human representative?"
    ),
    Phase.POST_PROCESS: (
        "Would you like me to email you a summary of today's call? It's your choice: "
        "I can send it or skip it."
    ),
    Phase.HANDOFF: (
        "I'm connecting you with a human representative now, and I'll pass along what "
        "you've shared so you won't have to repeat it."
    ),
    Phase.ENDED: "Thanks for calling. This conversation is complete; start a new chat any time.",
}


@dataclass
class Plan:
    """What the responder must accomplish this turn, and the only data it may use."""

    phase: Phase
    instructions: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    static_reply: str | None = None  # set when no LLM call is needed
    closing: bool = False  # the call ends after this reply
    fallback_text: str | None = None  # safe reply specific to this turn
    # A code-written reply that holds identity values (the read-back, 7a) is stored in the
    # transcript in this masked form, so no later model call ever sees the values.
    transcript_text: str | None = None

    def say(self, line: str) -> None:
        self.instructions.append(line)

    @property
    def fallback(self) -> str:
        if self.fallback_text:
            return self.fallback_text
        return FALLBACKS[Phase.ENDED] if self.closing else FALLBACKS[self.phase]


PHASE_ORDER = {
    Phase.VERIFY_ID: 0,
    Phase.RESOLVE_INTENT: 1,
    Phase.PROCESS_CASE: 2,
    Phase.POST_PROCESS: 3,
}


def _labels(fields) -> str:
    return ", ".join(FIELD_LABELS[f] for f in fields)


PATH_INSTRUCTIONS = {
    "denial_explanation": (
        "Explain in plain words why this claim was denied (use denial_reason). If documents_needed "
        "is set, say which documents are missing and that sending them lets the claim be reviewed "
        "again. Offer to explain how to send them."
    ),
    "claim_status": (
        "Tell them the claim's current status in plain words (use status and summary). Give "
        "amounts only if ALLOWED DATA has them (amount_meanings says what each money field means)."
    ),
    "appeal_steps": (
        "Explain how to get this decision reviewed again, using only ALLOWED DATA: send the "
        "documents in document_guidance the way how_to_submit describes, before the appeal "
        "deadline. If ALLOWED DATA has no appeal deadline or documents, say you don't see an "
        "appeal option on this claim and offer a human representative."
    ),
    "documents_needed": (
        "Tell them which documents this claim needs and what each should show (document_guidance), "
        "how to submit them, and what to do if one isn't available. If the claim needs no "
        "documents, say so."
    ),
}


READBACK_FIELDS = ("id_last4", "dob", "phone", "email")
READBACK_PLACEHOLDERS = {
    "id_last4": "[ID last 4 given]",
    "dob": "[DOB given]",
    "phone": "[phone given]",
    "email": "[email given]",
}


def _digit_words(v: str) -> str:
    return ", ".join(re.sub(r"\D", "", v)[-4:])


def _spoken(field: str, value: str) -> str:
    """One identity value as the voice should say it: digits one by one, dates in words."""
    if field == "full_name":
        return f"the name {value}"
    if field == "id_last4":
        return f"ID ending {_digit_words(value)}"
    if field == "phone":
        return f"phone ending {_digit_words(value)}"
    if field == "dob":
        try:
            d = datetime.strptime(norm_dob(value), "%Y-%m-%d")
            return f"birth date {d.strftime('%B')} {d.day}, {d.year}"
        except ValueError:
            return "the birth date you gave"
    local, _, domain = value.partition("@")
    return f"email {local} at {domain.replace('.', ' dot ')}"


# Decision 9c: the reply model gets only the claim fields the answer needs. Money, the policy
# number and deadlines are added only when the caller's words ask for them.
CLAIM_BASE_FIELDS = ("case_id", "case_type", "status", "summary", "created_at")
PATH_CLAIM_FIELDS = {
    "claim_status": (),
    "denial_explanation": ("denial_reason", "documents_needed", "appeal_deadline"),
    "appeal_steps": ("documents_needed", "appeal_deadline"),
    "documents_needed": ("documents_needed", "appeal_deadline"),
}
ASK_FIELDS = (
    (re.compile(r"amount|\$|pay|paid|cost|fee|reimburs|money|owe|how much|cover", re.I), MONEY_FIELDS),
    (re.compile(r"deadline|appeal|due|by when|how long", re.I), ("appeal_deadline", "documents_needed")),
    (re.compile(r"document|report|note|send|submit|proof", re.I), ("documents_needed",)),
    (re.compile(r"deni|reason|why", re.I), ("denial_reason",)),
)
POLICY_ASK_RE = re.compile(r"policy", re.I)


def _claim_fields(path: str | None, asked: str) -> tuple[str, ...]:
    out = list(CLAIM_BASE_FIELDS) + list(PATH_CLAIM_FIELDS.get(path or "", ()))
    for pattern, extra in ASK_FIELDS:
        if pattern.search(asked):
            out += [f for f in extra if f not in out]
    return tuple(out)


def _last_caller_text(s: Session) -> str:
    for t in reversed(s.transcript):
        if t["role"] == "user":
            return t["content"]
    return ""


class SOPController:
    def __init__(self, store: DataStore, outbox: list[dict]):
        self.store = store
        self.outbox = outbox

    # ---- event helpers ---------------------------------------------------

    @staticmethod
    def ev(s: Session, type_: str, **kw: Any) -> None:
        s.turn_events.append({"type": type_, **kw})
        trace.write(s, s.turn_events[-1])

    def transition(self, s: Session, to: Phase) -> None:
        self.ev(s, "transition", **{"from": s.phase.value, "to": to.value})
        s.phase = to

    # ---- entry point -----------------------------------------------------

    def step(self, s: Session, ex: Extraction) -> Plan:
        emotion.record(s, ex)
        if s.phase in (Phase.HANDOFF, Phase.ENDED):
            return Plan(s.phase, static_reply=FALLBACKS[s.phase])

        p = Plan(s.phase)
        self._log_emotion(s, ex)
        for line in emotion.tone_lines(ex):
            p.say(line)

        offered_before = s.pending == "handoff_offer"
        if ex.wants_human or (s.pending == "handoff_offer" and ex.yes_no == "yes"):
            return self.handoff(s, "caller_request", p)
        if s.pending == "handoff_offer" and ex.yes_no == "no":
            s.pending = None
            p.say("The caller declined the transfer. Acknowledge that and continue helping.")

        if not ex.in_scope:
            return self._off_topic(s, p)

        if ex.forbidden_action:
            self.ev(
                s, "guard", name="allowed_actions",
                action="refused: caller asked to skip a step, override the rules, or change a claim",
            )
            p.say(
                "The caller asked for something you are not allowed to do (approve, pay, change "
                "or cancel a claim, or skip a required step). Say plainly that you can't do that "
                "and why in one sentence, then offer what you can do."
            )

        start = s.phase
        resist = self._resist(s, ex, p, offered_before)
        if isinstance(resist, Plan):
            return resist

        handler = {
            Phase.VERIFY_ID: self.verify,
            Phase.RESOLVE_INTENT: self.resolve,
            Phase.PROCESS_CASE: self.process,
            Phase.POST_PROCESS: self.post,
        }[s.phase]
        plan = handler(s, ex, p)
        if PHASE_ORDER.get(plan.phase, -1) > PHASE_ORDER.get(start, -1):
            s.counters["resistance"] = 0  # real progress resets the streak
        elif resist and plan.phase == start and not plan.closing:
            plan.instructions[:0] = resist  # the step is still pending: persuade first
        return plan

    # ---- cross-cutting ---------------------------------------------------

    def _log_emotion(self, s: Session, ex: Extraction) -> None:
        """Trace event: the strongest reading by name, plus every non-none reading with words."""
        seen = {
            name: {"level": r.level, "words": r.words}
            for name in EMOTIONS
            if (r := getattr(ex.emotions, name)).level != "none"
        }
        top = max(seen, key=lambda n: seen[n]["level"] == "strong", default=None)
        self.ev(s, "emotion", label=top or "neutral", readings=seen)

    @staticmethod
    def _required_step(s: Session) -> str | None:
        """The SOP step the caller may resist: identity, or a representative's consent."""
        if s.phase == Phase.VERIFY_ID:
            return "identity"
        if s.caller_role == "representative" and s.consent_status == "pending":
            return "consent"
        return None

    def _resist(self, s: Session, ex: Extraction, p: Plan, offered_before: bool) -> Plan | list[str] | None:
        """Decision 11c ladder: explain and offer, then offer a person, then hand off.

        Returns the handoff Plan, or the persuasion lines to put ahead of the step's own
        lines, or None. The gate itself is never loosened here.
        """
        step_name = self._required_step(s)
        pushing = (
            emotion.resisting(ex)
            or ex.emotions.anger.level != "none"
            or (step_name == "identity" and ex.forbidden_action)
        )
        if step_name is None or not pushing:
            return None
        n = s.counters["resistance"] = s.counters.get("resistance", 0) + 1
        self.ev(s, "resistance", step=step_name, count=n)
        if n >= RESISTANCE_HANDOFF or (offered_before and ex.emotions.anger.level == "strong"):
            return self.handoff(s, "repeated_resistance", p)

        if step_name == "identity":
            why = (
                "Explain in one sentence that claim details are protected and verification keeps "
                "their information safe. Offer the allowed options: any three of the five details "
                "work, so they can skip one they don't want to share; they can type the details "
                "instead of saying them; or they can speak with a human representative."
            )
        else:
            why = (
                "Explain in one sentence that the policyholder must approve before anyone else "
                "hears about their claim, which protects the policyholder. Offer the options: wait "
                "for the approval, or speak with a human representative."
            )
        lines = [
            "The caller is pushing back on a required step. After one sentence acknowledging how "
            "they feel, " + why[0].lower() + why[1:] + " Do not disclose any claim detail and "
            "do not skip the step."
        ]
        if n >= RESISTANCE_OFFER_HUMAN or emotion.frustration_shown(s):
            s.pending = "handoff_offer"
            self.ev(s, "handoff_offered", reason="resistance")
            lines.append(
                "Stop persuading. Offer to connect them with a human representative, and ask "
                "directly whether they'd like that."
            )
        return lines

    def _off_topic(self, s: Session, p: Plan) -> Plan:
        s.counters["off_topic"] += 1
        n = s.counters["off_topic"]
        self.ev(s, "scope", in_scope=False, count=n)
        if n >= OFF_TOPIC_HANDOFF:
            return self.handoff(s, "repeated_off_topic", p)
        p.say(
            "The caller asked about something outside insurance customer service. Politely "
            "decline in one sentence. Do NOT answer it or hint at the answer. Then steer back: "
            + self._current_ask(s)
        )
        if n >= OFF_TOPIC_OFFER_HUMAN:
            s.pending = "handoff_offer"
            self.ev(s, "handoff_offered", reason="repeated_off_topic")
            p.say(
                "This has happened several times. Offer to connect them with a human "
                "representative, and ask whether they'd like that."
            )
        return p

    def _current_ask(self, s: Session) -> str:
        if s.phase == Phase.VERIFY_ID:
            missing = [f for f in PII_FIELDS if not s.identity.get(f)]
            return f"you still need to verify their identity (any three of: {_labels(PII_FIELDS)}); still open: {_labels(missing)}."
        if s.phase == Phase.RESOLVE_INTENT:
            return "ask which of their claims they'd like help with."
        if s.phase == Phase.PROCESS_CASE:
            return "ask what else they'd like to know about their claim."
        return "ask whether they'd like the email summary sent or skipped."

    @staticmethod
    def _packet(s: Session, reason: str) -> dict[str, Any]:
        """Decision 11d. Field names and the caller's words only, never identity values."""
        known = Extraction(**{f: s.identity[f] for f in PII_FIELDS if s.identity.get(f)})
        readings = []
        for entry in s.emotion_log:
            seen = {
                name: {"level": r["level"], "words": memory.redact(r.get("words") or "", known, "VERIFY_ID")}
                for name, r in entry.get("readings", {}).items()
                if r.get("level", "none") != "none"
            }
            if seen:
                readings.append({"turn": entry["turn"], "readings": seen})
        return {
            "reason": reason,
            "verified": s.verified,
            "customer_id": s.customer_id if s.verified else None,
            "matched_fields": list(s.matched_fields),
            "phase_at_handoff": s.phase.value,
            "memory": {k: v for k, v in memory.snapshot(s).items() if not k.startswith("id.") and k != "email_address"},
            "claims_discussed": list(s.case.claims_discussed),
            "counters": dict(s.counters),
            "emotions": readings,
            "caller_role": s.caller_role,
            "rep_name": s.rep_name,
            "rep_relationship": s.rep_relationship,
            "consent_status": s.consent_status,
            "consent_claim_id": s.consent_claim_id,
        }

    def handoff(self, s: Session, reason: str, p: Plan) -> Plan:
        s.handoff_packet = self._packet(s, reason)
        s.pending = None
        self.ev(s, "handoff", reason=reason)
        self.transition(s, Phase.HANDOFF)
        p.phase = Phase.HANDOFF
        p.data = {"reason": reason, "caller_verified": s.verified}
        p.say(
            "Tell the caller you are connecting them with a human representative now, and that "
            "you'll pass along what they've shared so they won't have to repeat it."
        )
        if not s.verified:
            p.say("Mention the representative will also need to confirm their identity.")
        return p

    # ---- VERIFY_ID (strict) -----------------------------------------------

    def verify(self, s: Session, ex: Extraction, p: Plan) -> Plan:
        readback = self._readback(s, ex, p)
        if readback is not None:
            return readback
        if ex.calling_for_other or s.caller_role == "representative":
            early = self._rep_intake(s, ex, p)  # step 8: caller acts for the policyholder
            if early is not None:
                return early
        given = [f for f in PII_FIELDS if s.identity.get(f)]
        p.data = {
            "identity_fields_received": [FIELD_LABELS[f] for f in given],
            "policy_number_received": bool(s.identity.get("policy_id")),
        }

        key = tuple(sorted((k, v) for k, v in s.identity.items()))
        if len(given) >= 3 and key != s.identity_attempted:
            s.identity_attempted = key
            self.ev(s, "tool_call", name="verify_identity")
            r = self.store.verify_identity(s.identity)
            detail = "3 or more details match one record, none conflict" if r.verified else "not verified"
            if DEBUG_TRACE:
                detail += f" [debug: {len(r.matched)} match, {r.mismatched} conflict]"
            self.ev(s, "gate", name="identity_verified", passed=r.verified, detail=detail)
            if r.verified:
                if s.caller_role == "representative":
                    return self._rep_gate(s, ex, p, r)
                s.verified = True
                s.customer_id = r.customer_id
                s.matched_fields = r.matched
                memory.forget_id_digits(s)
                if s.pending == "handoff_offer":
                    s.pending = None
                self.transition(s, Phase.RESOLVE_INTENT)
                p.phase = Phase.RESOLVE_INTENT
                # VERIFY_ID-only lines (refusal coaching, "can't discuss claims yet") would
                # contradict what RESOLVE_INTENT now says, so none were added on this path.
                return self.resolve(s, ex, p, just_verified=True)

            s.counters["verify_failures"] += 1
            if s.counters["verify_failures"] >= MAX_VERIFY_FAILURES:
                return self.handoff(s, "verification_failed", p)
            # Start the identity check over: stale wrong values must not block forever, and
            # clearing everything (not just the bad field) avoids telling anyone which was wrong.
            memory.clear_identity(s)
            p.data["identity_fields_received"] = []
            p.say(
                "The details given don't match our records. Do NOT say which detail was wrong or "
                "reveal anything we have on file. Say gently that something didn't match (one may "
                "be mistyped), and ask them to share any three of the details again. You may say "
                "attempts are limited, but not the exact count."
            )
            return p

        self._verify_coaching(s, ex, p)
        if given:
            missing = [f for f in PII_FIELDS if f not in given]
            p.say(
                f"The caller has given: {_labels(given)}. Do not ask for those again. Ask for "
                f"{3 - len(given)} more, from: {_labels(missing)}."
            )
        else:
            p.say(
                "Ask the caller to verify their identity with any three of: "
                f"{_labels(PII_FIELDS)}."
            )
        if ex.policy_id:
            p.say(
                "They gave a policy number: thank them, it helps locate the account, but it isn't "
                "one of the identity details."
            )
        return p

    # ---- read-back of voice-captured details (7a) -----------------------------

    def _readback(self, s: Session, ex: Extraction, p: Plan) -> Plan | None:
        """Read voice-captured details back before the check; None means carry on to the check."""
        fresh = [f for f in READBACK_FIELDS if getattr(ex, f)] if s.channel == "voice" else []
        if s.pending == "readback":
            if ex.yes_no == "yes" and not fresh:
                s.pending, s.readback_fields = None, []
                self.ev(s, "gate", name="readback", passed=True, detail="caller confirmed")
                return None
            if ex.yes_no == "no":
                # A no is a mishearing, not a failed check: drop exactly those details.
                for f in s.readback_fields:
                    if f not in fresh:
                        s.identity.pop(f, None)
                        s.memory.pop(f"id.{f}", None)
                s.identity_attempted = None
                s.readback_fields = []
                s.pending = None
                self.ev(s, "gate", name="readback", passed=False, detail="caller said no")
                if not fresh:
                    p.say(
                        "The caller said a detail was heard wrong. Apologise briefly, say it "
                        "doesn't count against them, and ask for the details again."
                    )
                    return None
            else:
                fresh = [f for f in READBACK_FIELDS if f in s.readback_fields or f in fresh]
        if not fresh:
            return None
        if s.identity.get("full_name") and "full_name" not in fresh:
            # A misheard name fails the check like any other detail, so it is read back with the rest.
            fresh = ["full_name", *fresh]
        s.readback_fields, s.pending = fresh, "readback"
        self.ev(s, "gate", name="readback", passed=None, detail="read back before the check")
        p.phase = Phase.VERIFY_ID
        p.static_reply = self._readback_line(s, fresh, masked=False)
        p.transcript_text = self._readback_line(s, fresh, masked=True)
        return p

    @staticmethod
    def _readback_line(s: Session, fields: list[str], masked: bool) -> str:
        parts = []
        for f in fields:
            if masked and f != "full_name":  # the model sees names (ADR 0003), so the name is not masked
                parts.append(READBACK_PLACEHOLDERS[f])
            else:
                parts.append(_spoken(f, s.identity.get(f, "")))
        if len(parts) > 1:
            heard = ", ".join(parts[:-1]) + " and " + parts[-1]
        else:
            heard = parts[0]
        return f"I heard {heard}. Is that right?"

    def _verify_coaching(self, s: Session, ex: Extraction, p: Plan) -> None:
        """Lines for a turn that stays in VERIFY_ID: questions and noted hints (resistance is step())."""
        if ex.asks_about_process:
            p.say(
                "First answer their question about the process: we confirm identity before "
                "discussing any account or claim to protect their information, and any three "
                "of the five details are enough."
            )

        noted = any(
            k in s.memory and s.memory[k].source_turn == s.turn and s.memory[k].phase_captured == "VERIFY_ID"
            for k in ("intent", "claim_type", "claim_month", "claim_status_hint", "claim_id_hint")
        )
        if noted:
            p.say(
                "The caller said what they're calling about. Acknowledge that you've noted it and "
                "will help with it right after verification. Do not confirm or deny that any claim "
                "exists, and give no claim details."
            )

    # ---- RESOLVE_INTENT (interpretation, bounded by ownership) ---------------

    def _hints(self, s: Session, ex: Extraction, use_memory: bool) -> dict[str, Any]:
        turn = {
            "claim_type": ex.claim_type,
            "claim_month": ex.claim_month,
            "claim_year": ex.claim_year,
            "claim_status": ex.claim_status_hint,
        }
        if any(turn.values()):
            return turn
        if use_memory and not s.memory_hints_used:
            remembered = {
                "claim_type": memory.hint(s, "claim_type"),
                "claim_month": memory.month_number(memory.hint(s, "claim_month")),
                "claim_year": memory.hint(s, "claim_year"),
                "claim_status": memory.hint(s, "claim_status_hint"),
            }
            if any(remembered.values()):
                return remembered
        return {}

    @staticmethod
    def _match(claims: list[dict], hints: dict[str, Any]) -> list[dict]:
        out = claims
        if hints.get("claim_type"):
            out = [c for c in out if c["case_type"] == hints["claim_type"]]
        if hints.get("claim_month"):
            out = [c for c in out if int(c["created_at"][5:7]) == hints["claim_month"]]
        if hints.get("claim_year"):
            out = [c for c in out if int(c["created_at"][:4]) == hints["claim_year"]]
        if hints.get("claim_status"):
            out = [c for c in out if c["status"] == hints["claim_status"]]
        return out

    def resolve(self, s: Session, ex: Extraction, p: Plan, just_verified: bool = False,
                after_back_edge: bool = False) -> Plan:
        p.phase = Phase.RESOLVE_INTENT
        if s.caller_role == "representative":
            return self._rep_resolve(s, ex, p, just_verified)
        first = self.store.first_name(s.customer_id)
        self.ev(s, "tool_call", name="list_claims")
        claims = self.store.list_claims(s.customer_id)
        p.data = {"caller_first_name": first, "claims_on_file": claims}
        if just_verified:
            p.say(f"Identity is now verified. Briefly thank {first} and say they're verified.")

        # 0. Answer to the offer to verify as another claim's owner (6c).
        if s.pending == "switch_offer":
            answered = self._switch_answer(s, ex, p)
            if answered is not None:
                return answered

        # 1. Answer to a proposed claim.
        if s.pending == "claim_confirm" and s.proposed_claim_id:
            if ex.yes_no == "yes" and not (ex.claim_id and ex.claim_id.upper() != s.proposed_claim_id):
                return self._select(s, ex, p, s.proposed_claim_id)
            if ex.yes_no == "no":
                s.pending, s.proposed_claim_id = None, None
                p.say("The caller says that isn't the claim they meant.")

        # 2. A claim the caller named or clearly pointed at.
        hint_slot = s.memory.get("claim_id_hint")
        claim_id = ex.claim_id or (
            hint_slot.value if just_verified and hint_slot is not None else None
        )
        if claim_id:
            owned = self.store.get_claim(s.customer_id, claim_id) is not None
            self.ev(s, "gate", name="claim_ownership", passed=owned, detail=claim_id.upper())
            if owned:
                if just_verified:  # named before verification: confirm before using it
                    return self._propose(s, p, claim_id.upper(), from_memory=True)
                return self._select(s, ex, p, claim_id.upper())
            return self._other_claim(s, p)

        # 3. Match on hints (this turn's, else the ones remembered from earlier phases).
        hints = self._hints(s, ex, use_memory=not after_back_edge)
        if hints:
            from_memory = not any(
                (ex.claim_type, ex.claim_month, ex.claim_year, ex.claim_status_hint)
            )
            candidates = self._match(claims, hints)
            s.memory_hints_used = True  # remembered hints are proposed once, then the caller steers
            if len(candidates) == 1:
                return self._propose(s, p, candidates[0]["case_id"], from_memory=from_memory)
            if len(candidates) > 1:
                p.data["matching_claims"] = candidates
                p.say(
                    "More than one claim fits what the caller described. Ask which one they mean, "
                    "naming each briefly (what it was for, its status, and the month and year)."
                )
                return p
            p.say(
                "No claim on file fits what the caller described. Say so gently and briefly list "
                "the claims on file so they can pick."
            )
            return p

        intent = ex.intent if ex.intent in PATHS else memory.hint(s, "intent")
        if intent and len(claims) == 1:
            return self._propose(s, p, claims[0]["case_id"], from_memory=False)
        p.say(
            "Ask what they'd like help with today. You may briefly mention the claims on file."
        )
        return p

    # ---- a claim that is not the verified caller's (6c) ---------------------------

    def _other_claim(self, s: Session, p: Plan) -> Plan:
        """Never confirm or deny the claim exists or whose it is; offer once, or hand off."""
        if emotion.frustration_shown(s) or s.counters.get("switch_offers", 0) >= 1:
            return self.handoff(s, "other_identity_claim", p)
        s.counters["switch_offers"] = 1
        s.pending = "switch_offer"
        p.say(
            "The caller asked about a claim you can't discuss under their verified identity. Do "
            "NOT say whether that claim exists or whose it is, and say nothing about it. Offer "
            "the caller two choices: verify again as the person that claim belongs to, or be "
            "connected with a human representative. Ask which they prefer."
        )
        return p

    def _switch_answer(self, s: Session, ex: Extraction, p: Plan) -> Plan | None:
        """The caller's choice after the offer; None when they moved on (the offer is spent)."""
        claim = s.memory.get("claim_id_hint")
        claim_id = claim.value if claim is not None else None
        if ex.switch_choice == "human":
            return self.handoff(s, "other_identity_claim", p)
        if ex.switch_choice == "verify" and claim_id:
            self.ev(s, "gate", name="person_switch", passed=True, detail="verify as another person")
            memory.clear_person(s, keep_claim_id=claim_id)  # failure count stays (7d)
            self.transition(s, Phase.VERIFY_ID)
            p.phase = Phase.VERIFY_ID
            p.data = {"identity_fields_received": [], "policy_number_received": False}
            p.say(
                "Tell the caller you'll verify them as the person they named. Ask for any three "
                f"of: {_labels(PII_FIELDS)}. Do not say anything about the claim."
            )
            return p
        s.pending = None
        return None

    def _propose(self, s: Session, p: Plan, claim_id: str, from_memory: bool) -> Plan:
        s.pending, s.proposed_claim_id = "claim_confirm", claim_id
        p.data["proposed_claim"] = next(
            c for c in p.data["claims_on_file"] if c["case_id"] == claim_id
        )
        if from_memory:
            p.say(
                "Use what the caller told you earlier in the call (do not ask from scratch): say "
                "you found the claim that matches it, summarize it in one line (what it was for, "
                "the date it was opened, and its status), and ask them to confirm it's the one."
            )
        else:
            p.say(
                "Say you found the claim that matches, summarize it in one line (what it was for, "
                "the date it was opened, and its status), and ask them to confirm it's the one."
            )
        return p

    def _select(self, s: Session, ex: Extraction, p: Plan, claim_id: str) -> Plan:
        claim = self.store.get_claim(s.customer_id, claim_id)
        s.pending, s.proposed_claim_id, s.selected_claim_id = None, None, claim_id
        intent = ex.intent if ex.intent in PATHS else memory.hint(s, "intent")
        if intent not in PATHS:
            intent = "denial_explanation" if claim["status"] == "denied" else "claim_status"
        s.path = intent
        self.ev(s, "gate", name="intent_resolved", passed=True, detail=f"{claim_id} / {intent}")
        self.ev(s, "path", name=intent)
        self.transition(s, Phase.PROCESS_CASE)
        return self.process(s, ex, p, entering=True)

    # ---- PROCESS_CASE (free language, grounded data, bounded actions) ----------

    def process(self, s: Session, ex: Extraction, p: Plan, entering: bool = False) -> Plan:
        p.phase = Phase.PROCESS_CASE
        path_changed = False
        if not entering:
            switching = ex.other_claim or (
                ex.claim_id and ex.claim_id.upper() != s.selected_claim_id
            )
            if switching:
                s.selected_claim_id, s.path, s.pending = None, None, None
                self.transition(s, Phase.RESOLVE_INTENT)
                return self.resolve(s, ex, p, after_back_edge=True)
            if ex.done or (s.pending == "anything_else" and ex.yes_no == "no" and not ex.question):
                self.transition(s, Phase.POST_PROCESS)
                return self.post(s, ex, p, entering=True)
            if ex.intent in PATHS and ex.intent != s.path:
                s.path = ex.intent
                path_changed = True
                self.ev(s, "path", name=s.path)

        self.ev(s, "tool_call", name="get_claim")
        claim = self.store.get_claim(s.customer_id, s.selected_claim_id)
        self.ev(s, "gate", name="claim_ownership", passed=claim is not None, detail=s.selected_claim_id)
        today = self.store.today
        asked = " ".join(
            [_last_caller_text(s), ex.question or "", getattr(s.memory.get("deferred_question"), "value", "")]
        )
        fields = _claim_fields(s.path, asked)
        p.data = {"claim": {k: claim[k] for k in fields if claim.get(k) not in (None, "", [])},
                  "as_of_date": today.isoformat()}
        if "net_pay" in fields:
            p.data["amount_meanings"] = self.store.amount_meanings()
        if POLICY_ASK_RE.search(asked):
            p.data["policy_number"] = self.store.policy_number(s.customer_id)
        g = self.store.guideline
        if claim.get("documents_needed") and "documents_needed" in fields:
            self.ev(s, "tool_call", name="get_document_guidance")
            p.data["document_guidance"] = guidelines.document_guidance(g, claim)

        deadline = appeal_deadline_state(claim, today) if "appeal_deadline" in fields else None
        if deadline:
            p.data["appeal_deadline_state"] = deadline
            if deadline == "open":
                p.data["days_until_appeal_deadline"] = days_until_deadline(claim, today)
            else:
                p.say(
                    "The appeal deadline on this claim has already passed. Say so plainly, do not "
                    "tell them to file before it, and offer a human representative to review "
                    "their options. Do not promise a late appeal is possible."
                )

        if claim["case_id"] not in s.case.claims_discussed:
            s.case.claims_discussed.append(claim["case_id"])
        if s.path not in s.case.paths_taken:
            s.case.paths_taken.append(s.path)

        if entering or path_changed:
            p.say(PATH_INSTRUCTIONS[s.path])
            if s.path == "claim_status" and "net_pay" not in fields:
                p.say("Do not mention any amounts unless the caller asks; none are in ALLOWED DATA.")
            if deadline == "open" and s.path != "claim_status":
                p.say(
                    "Mention the appeal deadline (appeal_deadline) and how many days are left "
                    "(days_until_appeal_deadline)."
                )
        else:
            p.say(
                "Keep helping with this claim. Respond to what the caller just said; don't repeat "
                "what you already explained unless they ask."
            )

        deferred = s.memory.get("deferred_question")
        if entering and deferred is not None:
            p.say(f"Earlier, before verification, the caller asked: \"{deferred.value}\". Make sure this reply answers it.")
        followup_text = f"{_last_caller_text(s)} {ex.question or ''}"
        lacks_document = ex.lacks_document
        if entering:  # words and flags from before verification count too
            followup_text += f" {deferred.value}" if deferred is not None else ""
            lacks_document = lacks_document or s.memory.get("deferred_lacks_document") is not None
        followup = guidelines.followup_answer(g, claim, followup_text, lacks_document)
        if followup:
            self.ev(s, "tool_call", name="get_followup_guidance", topic=followup["topic"])
            p.data["guideline_answer"] = followup
            p.say(
                "The company has a set answer for this follow-up (guideline_answer). Give it in your "
                "own words, keeping every fact and timeframe in it unchanged."
            )
            if lacks_document:
                p.data["if_alternatives_fail"] = guidelines.human_review_note(g)
        elif ex.question and (deferred is None or ex.question != deferred.value):
            if claim.get("documents_needed"):
                p.data["followup_fallback"] = guidelines.followup_fallback(g)
            p.say(
                f"Answer the caller's question (\"{ex.question}\") using only ALLOWED DATA. If the "
                "data doesn't contain the answer, say you don't have that information here"
                + (" (followup_fallback gives the general expectation)" if claim.get("documents_needed") else "")
                + " and offer a human representative. Never guess."
            )
        p.say("End by asking if there's anything else they'd like to know about this claim or another one.")
        s.pending = "anything_else"
        return p

    # ---- POST_PROCESS (consent-gated follow-up) --------------------------------

    def post(self, s: Session, ex: Extraction, p: Plan, entering: bool = False) -> Plan:
        p.phase = Phase.POST_PROCESS
        on_file = self.store.customer(s.customer_id)["email"]
        p.data = {
            "claims_discussed": s.case.claims_discussed,
            "email_on_file_masked": memory.mask("email", on_file),
        }
        rep = s.caller_role == "representative"
        where = (
            "the email on the policyholder's file (masked form), not the caller's own address; "
            "say it goes to the policyholder's file"
            if rep else "their email on file (use the masked form)"
        )
        if entering:
            s.pending = "email_consent"
            p.say(
                "Wrap up the case in one short sentence. Then offer to email a summary (what was "
                f"discussed, the claim status, and next steps) to {where}. "
                "Make clear it's their choice: send it or skip it."
            )
            return p

        if ex.other_claim or ex.claim_id:
            s.pending = None
            self.transition(s, Phase.RESOLVE_INTENT)
            return self.resolve(s, ex, p, after_back_edge=True)

        # Decision 8b: the summary goes only to the email on the policyholder's file.
        if ex.email_address_for_summary:
            p.say(
                "They gave another address. Say plainly that the summary can only go to the "
                "email on the policyholder's file (masked form), never to a different address."
            )

        if s.pending == "email_consent":
            if ex.yes_no == "yes":
                return self._send_and_close(s, p, on_file)
            if ex.yes_no == "no":
                self.ev(s, "gate", name="email_consent", passed=False, detail="caller declined; nothing sent")
                s.pending = None
                self.transition(s, Phase.ENDED)
                p.closing = True
                p.say("Confirm no email will be sent, thank them, and close the call warmly.")
                return p

        p.say(
            "The caller hasn't decided yet. Briefly explain what the email would include (what was "
            "discussed, the claim status, next steps) and ask clearly: send it or skip it?"
        )
        s.pending = "email_consent"
        return p

    def _send_and_close(self, s: Session, p: Plan, to: str) -> Plan:
        self.ev(s, "gate", name="email_consent", passed=True, detail="caller chose to send")
        self.ev(s, "tool_call", name="send_email")
        email = compose_summary(self.store, s)
        self.outbox.append(email)
        masked = memory.mask("email", to)
        self.ev(s, "email_sent", to=masked)
        s.pending = None
        self.transition(s, Phase.ENDED)
        p.closing = True
        owner = "the policyholder's email on file" if s.caller_role == "representative" else "your email on file"
        p.fallback_text = f"Done: I've sent the summary to {owner}, {masked}. Thanks for calling, and take care."
        p.data["email_sent_to"] = masked
        p.say(
            "Confirm the summary was sent to the email on the policyholder's file (masked form)"
            + (", and tell the representative it went there" if s.caller_role == "representative" else "")
            + ", thank them, and close the call warmly."
        )
        return p

    # ---- representative and consent (step 8) -----------------------------------

    def _rep_intake(self, s: Session, ex: Extraction, p: Plan) -> Plan | None:
        """Record who is calling for the policyholder; the 3-of-5 check runs on the policyholder."""
        if ex.calling_for_other and s.caller_role != "representative":
            s.caller_role = "representative"
            self.ev(s, "gate", name="caller_role", passed=True, detail="representative")
        if ex.rep_name:
            s.rep_name = ex.rep_name.strip()
        if ex.relationship:
            s.rep_relationship = ex.relationship.strip().lower()
        if s.rep_name and ex.full_name and consent.same_name(ex.full_name, s.rep_name):
            # The caller's own name is not an identity detail of the policyholder.
            s.identity.pop("full_name", None)
            s.memory.pop("id.full_name", None)
        p.say(
            "The caller is acting for someone else. The identity details to check are the "
            "policyholder's, not the caller's own. Do not say anything about the policyholder's "
            "account or claims."
        )
        if s.pending == "rep_identify":
            r = self.store.verify_identity(s.identity)
            if r.verified and s.rep_name and s.rep_relationship:
                return self._rep_gate(s, ex, p, r)
        if not (s.rep_name and s.rep_relationship):
            p.say(
                "Also ask for the caller's own name and how they are related to the policyholder."
            )
        return None

    def _rep_gate(self, s: Session, ex: Extraction, p: Plan, r) -> Plan:
        """Gate 2 (8a, 8d): the caller must be on the representative list for this policyholder."""
        p.data = {"caller_role": "representative"}
        if not (s.rep_name and s.rep_relationship):
            s.pending = "rep_identify"
            self.ev(s, "gate", name="representative_listed", passed=False, detail="name or relationship missing")
            p.say(
                "Ask for the caller's own name and how they are related to the policyholder. "
                "Do not say whether the details matched or anything about the account."
            )
            return p
        listed = consent.is_listed(
            self.store.representatives_for(r.customer_id), s.rep_name, s.rep_relationship
        )
        self.ev(s, "gate", name="representative_listed", passed=listed, detail="checked against the list on file")
        if not listed:
            self.handoff(s, "unlisted_representative", p)
            p.say(
                "Say you can only discuss a policy with the policyholder or someone already on "
                "file for them, and that the person you are connecting them with can help add "
                "them. Disclose nothing about the policyholder or any claims, and do not say "
                "whether a policy or claims exist."
            )
            return p
        s.verified, s.customer_id, s.matched_fields = True, r.customer_id, r.matched
        s.pending = None
        memory.forget_id_digits(s)
        self.transition(s, Phase.RESOLVE_INTENT)
        p.phase = Phase.RESOLVE_INTENT
        return self.resolve(s, ex, p, just_verified=True)

    def _rep_claim(self, s: Session, ex: Extraction, just_verified: bool) -> tuple[str | None, bool]:
        """The one claim the caller points at, found without telling them anything: (id, named)."""
        slot = s.memory.get("claim_id_hint")
        claim_id = ex.claim_id or (slot.value if just_verified and slot is not None else None)
        if claim_id:
            owned = self.store.get_claim(s.customer_id, claim_id) is not None
            self.ev(s, "gate", name="claim_ownership", passed=owned, detail=claim_id.upper())
            return (claim_id.upper() if owned else None), True
        hints = self._hints(s, ex, use_memory=True)
        if hints:
            s.memory_hints_used = True
            found = self._match(self.store.list_claims(s.customer_id), hints)
            if len(found) == 1:
                return found[0]["case_id"], False
        return None, False

    def _rep_resolve(self, s: Session, ex: Extraction, p: Plan, just_verified: bool) -> Plan:
        """A representative sees no claim list and no claim fact until the policyholder approves it."""
        p.data = {"caller_role": "representative", "representative_name": s.rep_name}
        if just_verified:
            p.say(
                "The caller is confirmed as someone on file for the policyholder. Thank them. "
                "Do not mention any claim, how many exist, or any status or amount."
            )
        if s.consent_status == "pending":
            return self._rep_wait(s, ex, p)
        claim_id, named = self._rep_claim(s, ex, just_verified)
        if claim_id is None:
            if s.consent_status == "timeout" and not (ex.claim_id or ex.other_claim):
                p.say(
                    "The policyholder's approval did not come back. Disclose nothing and offer a "
                    "human representative."
                )
                s.pending = "handoff_offer"
            else:
                p.say(
                    "Ask which claim number the caller wants to discuss. Do not list claims or say "
                    "whether any exist, and give no claim details."
                )
            return p
        if s.consent_status == "approved" and s.consent_claim_id == claim_id:
            return self._select(s, ex, p, claim_id)
        if s.consent_status == "timeout" and s.consent_claim_id == claim_id:
            # 8c: a timed-out claim is not re-requested; only a different claim starts a new request.
            p.say(
                "The policyholder's approval did not come back. Disclose nothing and offer a "
                "human representative."
            )
            s.pending = "handoff_offer"
            return p
        return self._rep_request(s, ex, p, claim_id, named)

    def _rep_request(self, s: Session, ex: Extraction, p: Plan, claim_id: str, named: bool) -> Plan:
        """8e: one request per claim, naming it. Consent lasts for this call only."""
        s.consent_claim_id, s.consent_status, s.consent_checks = claim_id, "pending", 0
        s.selected_claim_id, s.path = None, None
        self.ev(
            s, "tool_call", name="request_consent",
            detail=f"{s.rep_relationship} {s.rep_name} asks to discuss {claim_id}",
        )
        return self._rep_poll(s, ex, p, named)

    def _rep_wait(self, s: Session, ex: Extraction, p: Plan) -> Plan:
        named = bool(ex.claim_id and ex.claim_id.upper() == s.consent_claim_id)
        if ex.claim_id and not named:
            p.say("A different claim was named. Say the first approval is still pending and it can follow.")
        return self._rep_poll(s, ex, p, named)

    def _rep_poll(self, s: Session, ex: Extraction, p: Plan, named: bool) -> Plan:
        """One consent check per caller turn (8c): approved, still pending, or timed out."""
        status = consent.consent_status(consent.scenario_name(), s.consent_checks)
        s.consent_checks += 1
        self.ev(s, "tool_call", name="check_consent", detail=f"check {s.consent_checks}")
        if status == "approved":
            s.consent_status = "approved"
            self.ev(s, "gate", name="consent", passed=True, detail=s.consent_claim_id)
            p.say("The policyholder has approved this for this claim. Say so in one short sentence.")
            return self._select(s, ex, p, s.consent_claim_id)
        if s.consent_checks >= consent.CONSENT_TIMEOUT_CHECKS:
            s.consent_status = "timeout"
            s.pending = "handoff_offer"
            self.ev(s, "gate", name="consent", passed=False, detail="timeout")
            self.ev(s, "handoff_offered", reason="consent_timeout")
            p.data = {"caller_role": "representative"}
            p.say(
                "The approval request timed out: the policyholder has not answered. Say you "
                "cannot share anything without it, disclose nothing about any claim, and ask "
                "whether they would like a human representative."
            )
            return p
        p.data = {"caller_role": "representative", "representative_name": s.rep_name}
        if named:
            p.data["claim_id_requested"] = s.consent_claim_id
        p.say(
            "You have asked the policyholder to approve this caller discussing the claim"
            + (f" {s.consent_claim_id}" if named else " they described")
            + ", and you are waiting on the policyholder's answer. Say only that. Give no claim "
            "status, amount, date or other claim detail."
        )
        return p
