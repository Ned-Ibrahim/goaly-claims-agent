"""The SOP controller: a deterministic state machine that owns phase, gates and actions.

Each caller turn arrives here as an Extraction (what the caller said, as structured
fields). The controller updates state, runs the tools it allows in the current phase,
and returns a Plan: the instructions and the only data the responder LLM may use
for this turn. The LLM never changes the phase, never calls a tool, and never sees
data the current phase does not allow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import guidelines, memory
from .deadlines import appeal_deadline_state, days_until_deadline
from .emailer import compose_summary
from .models import (
    MAX_VERIFY_FAILURES,
    OFF_TOPIC_HANDOFF,
    OFF_TOPIC_OFFER_HUMAN,
    PATHS,
    PII_FIELDS,
    REFUSALS_HANDOFF,
    REFUSALS_OFFER_HUMAN,
    Extraction,
    Phase,
    Session,
)
from .store import DataStore

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
        "Tell them the claim's current status in plain words (use status and summary) and the "
        "amounts that matter to them (amount_meanings says what each money field means)."
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

    def transition(self, s: Session, to: Phase) -> None:
        self.ev(s, "transition", **{"from": s.phase.value, "to": to.value})
        if PHASE_ORDER.get(to, -1) > PHASE_ORDER.get(s.phase, -1):
            s.counters["off_topic"] = 0  # real progress resets the off-topic streak
        s.phase = to

    # ---- entry point -----------------------------------------------------

    def step(self, s: Session, ex: Extraction) -> Plan:
        if s.phase in (Phase.HANDOFF, Phase.ENDED):
            return Plan(s.phase, static_reply=FALLBACKS[s.phase])

        p = Plan(s.phase)
        self._emotion(s, ex, p)

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

        if (
            s.phase == Phase.VERIFY_ID
            and (ex.refuses_identity or ex.forbidden_action)
            and s.counters["refusals"] + 1 >= REFUSALS_HANDOFF
        ):
            s.counters["refusals"] += 1
            return self.handoff(s, "repeated_refusal", p)

        handler = {
            Phase.VERIFY_ID: self.verify,
            Phase.RESOLVE_INTENT: self.resolve,
            Phase.PROCESS_CASE: self.process,
            Phase.POST_PROCESS: self.post,
        }[s.phase]
        return handler(s, ex, p)

    # ---- cross-cutting ---------------------------------------------------

    def _emotion(self, s: Session, ex: Extraction, p: Plan) -> None:
        label = "refusing" if ex.refuses_identity and s.phase == Phase.VERIFY_ID else ex.emotion
        self.ev(s, "emotion", label=label)
        if ex.emotion in ("frustrated", "angry"):
            p.say(
                "The caller is upset. Open with one sincere sentence acknowledging how they feel "
                "(in your own words, no stock phrases), then continue calmly. Do not argue."
            )
        elif ex.emotion == "anxious":
            p.say("The caller sounds worried. Open with brief reassurance, then continue.")
        elif ex.emotion == "confused":
            p.say("The caller sounds confused. Explain simply, one step at a time.")

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

    def handoff(self, s: Session, reason: str, p: Plan) -> Plan:
        s.handoff_packet = {
            "reason": reason,
            "verified": s.verified,
            "customer_id": s.customer_id if s.verified else None,
            "phase_at_handoff": s.phase.value,
            "memory": memory.snapshot(s),
            "claims_discussed": list(s.case.claims_discussed),
            "counters": dict(s.counters),
        }
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

    def _verify_coaching(self, s: Session, ex: Extraction, p: Plan) -> None:
        """Lines for a turn that stays in VERIFY_ID: refusals, pressure, questions, noted hints."""
        if ex.refuses_identity or ex.forbidden_action:
            # Pushback and pressure share one persuasion budget.
            s.counters["refusals"] += 1
            n = s.counters["refusals"]
            if n >= REFUSALS_HANDOFF:
                p.say("The caller has refused repeatedly; stop persuading.")
                return
            p.say(
                "The caller is pushing back on verification. Explain in one sentence that claim "
                "details are protected and verification keeps their information safe. Offer "
                "alternatives: any three of the five details work, so they can skip one they don't "
                "want to share, or they can speak with a human representative."
            )
            if n >= REFUSALS_OFFER_HUMAN:
                s.pending = "handoff_offer"
                self.ev(s, "handoff_offered", reason="repeated_refusal")
                p.say(
                    "Stop persuading. Ask directly whether they'd like to be connected with a "
                    "human representative instead."
                )

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
        first = self.store.first_name(s.customer_id)
        self.ev(s, "tool_call", name="list_claims")
        claims = self.store.list_claims(s.customer_id)
        p.data = {"caller_first_name": first, "claims_on_file": claims}
        if just_verified:
            p.say(f"Identity is now verified. Briefly thank {first} and say they're verified.")

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
            p.data["caller_mentioned_claim_id"] = claim_id.upper()
            p.say(
                f"There is no claim {claim_id.upper()} on this caller's account. Say you couldn't "
                "find it on their account (never say whether it exists elsewhere) and ask them to "
                "check the number or describe the claim."
            )
            return p

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
        p.data = {
            "claim": claim,
            "policy_number": self.store.policy_number(s.customer_id),
            "amount_meanings": self.store.amount_meanings(),
            "as_of_date": today.isoformat(),
        }
        g = self.store.guideline
        if claim.get("documents_needed"):
            self.ev(s, "tool_call", name="get_document_guidance")
            p.data["document_guidance"] = guidelines.document_guidance(g, claim)

        deadline = appeal_deadline_state(claim, today)
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
        followup = guidelines.followup_answer(
            g, claim, f"{_last_caller_text(s)} {ex.question or ''}", ex.lacks_document
        )
        if followup:
            self.ev(s, "tool_call", name="get_followup_guidance", topic=followup["topic"])
            p.data["guideline_answer"] = followup
            p.say(
                "The company has a set answer for this follow-up (guideline_answer). Give it in your "
                "own words, keeping every fact and timeframe in it unchanged."
            )
            if ex.lacks_document:
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
        if entering:
            s.pending = "email_consent"
            p.say(
                "Wrap up the case in one short sentence. Then offer to email a summary (what was "
                "discussed, the claim status, and next steps) to their email on file (use the "
                "masked form). Make clear it's their choice: send it or skip it."
            )
            return p

        if ex.other_claim or ex.claim_id:
            s.pending, s.email_to = None, None
            self.transition(s, Phase.RESOLVE_INTENT)
            return self.resolve(s, ex, p, after_back_edge=True)

        if s.pending == "email_address_confirm":
            new_addr = (ex.email_address_for_summary or "").strip().lower()
            if new_addr and new_addr != s.email_to:
                s.email_to = new_addr
                p.data["requested_address"] = new_addr
                p.say("They gave a different address. Read back the new address and ask them to confirm it before you send.")
                return p
            if ex.yes_no == "yes":
                return self._send_and_close(s, p, s.email_to)
            if ex.yes_no == "no":
                s.pending, s.email_to = "email_consent", None
                p.say("Ask whether to send it to the email on file instead, or skip it.")
                return p

        if s.pending == "email_consent":
            if ex.yes_no == "yes":
                other = ex.email_address_for_summary
                if other and other.strip().lower() != on_file.lower():
                    s.pending, s.email_to = "email_address_confirm", other.strip().lower()
                    p.data["requested_address"] = s.email_to
                    p.say("Read back the address they gave and ask them to confirm it before you send.")
                    return p
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
        email = compose_summary(self.store, s, to)
        self.outbox.append(email)
        masked = memory.mask("email", to)
        self.ev(s, "email_sent", to=masked)
        s.pending = None
        self.transition(s, Phase.ENDED)
        p.closing = True
        p.fallback_text = f"Done: I've sent the summary to {masked}. Thanks for calling, and take care."
        p.data["email_sent_to"] = masked
        p.say("Confirm the summary was sent to that address (masked form), thank them, and close the call warmly.")
        return p
