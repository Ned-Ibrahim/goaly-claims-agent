"""One caller turn, end to end:

    extract (LLM, structured) -> remember (passive) -> SOP step (code) -> respond (LLM)
    -> guard (code) -> reply

Control decisions happen only in SOPController.step(). The LLM is used twice per
turn, both times with no authority: once to read the caller, once to phrase the reply.
"""

from __future__ import annotations

from typing import Any

from . import memory
from .guards import disclosure_filter, grounding_check
from .llm import LLM, LLMError
from .models import PII_FIELDS, Extraction, Phase, Session
from .prompts import extractor_system, extractor_user, responder_system
from .sop import Plan, SOPController
from .store import DataStore


class Agent:
    def __init__(self, store: DataStore, outbox: list[dict]):
        self.store = store
        self.controller = SOPController(store, outbox)
        self.sensitive = store.sensitive_claim_strings()

    def handle(self, s: Session, message: str, llm: LLM) -> dict[str, Any]:
        s.turn += 1
        s.turn_events = []

        if s.phase in (Phase.HANDOFF, Phase.ENDED):
            plan = self.controller.step(s, Extraction())
            s.transcript.append({"role": "user", "content": message})
            s.transcript.append({"role": "assistant", "content": plan.static_reply})
            return self.result(s, plan.static_reply, None)

        claims = self.store.list_claims(s.customer_id) if s.verified else None
        try:
            ex = llm.extract(extractor_system(claims), extractor_user(s.transcript, message))
        except LLMError as e:
            self.controller.ev(s, "guard", name="extractor", action=f"fallback ({e})")
            ex = Extraction()
        s.transcript.append({"role": "user", "content": message})

        for slot in memory.write(s, ex, message):
            self.controller.ev(s, "memory_write", slot=slot)
        if ex.in_scope:
            self.controller.ev(s, "scope", in_scope=True)

        plan = self.controller.step(s, ex)
        reply = self.respond(s, plan, llm, message)
        s.transcript.append({"role": "assistant", "content": reply})
        return self.result(s, reply, ex)

    # ---- reply generation with guards ------------------------------------

    def respond(self, s: Session, plan: Plan, llm: LLM, message: str) -> str:
        if plan.static_reply:
            return plan.static_reply
        system = responder_system(plan.phase, plan.instructions, plan.data)
        for attempt in range(2):
            try:
                reply = llm.respond(system, s.transcript)
            except LLMError as e:
                self.controller.ev(s, "guard", name="responder", action=f"fallback ({e})")
                return plan.fallback
            problems = self.check(s, plan, reply, message)
            if not problems:
                return reply
            action = "regenerate" if attempt == 0 else "fallback"
            self.controller.ev(s, "guard", name=self._guard_name(s), action=action, found=problems)
            system += (
                "\n\nYOUR PREVIOUS DRAFT WAS BLOCKED by the SOP guard because it contained: "
                + ", ".join(problems)
                + ". Write the reply again without any of that."
            )
        return plan.fallback

    @staticmethod
    def _guard_name(s: Session) -> str:
        return "grounding" if s.verified else "disclosure_filter"

    def check(self, s: Session, plan: Plan, reply: str, message: str) -> list[str]:
        if not s.verified:
            problems = disclosure_filter(reply, self.sensitive, s.identity.get("id_last4"))
        else:
            problems = grounding_check(reply, plan.data)
        self.controller.ev(
            s, "gate", name=self._guard_name(s), passed=not problems,
            detail=", ".join(problems) if problems else "clean",
        )
        return problems

    # ---- API shape -----------------------------------------------------------

    def result(self, s: Session, reply: str | None, ex: Extraction | None) -> dict[str, Any]:
        out = state(s)
        out["reply"] = reply
        out["events"] = list(s.turn_events)
        out["extraction"] = _masked_extraction(ex) if ex else None
        return out


def state(s: Session) -> dict[str, Any]:
    return {
        "session_id": s.session_id,
        "phase": s.phase.value,
        "ended": s.phase in (Phase.HANDOFF, Phase.ENDED),
        "verified": s.verified,
        "turn": s.turn,
        "memory": memory.snapshot(s),
        "counters": dict(s.counters),
        "pending": s.pending,
        "proposed_claim_id": s.proposed_claim_id,
        "selected_claim_id": s.selected_claim_id,
        "path": s.path,
        "handoff_packet": s.handoff_packet,
        "transcript": list(s.transcript),
    }


def _masked_extraction(ex: Extraction) -> dict[str, Any]:
    d = ex.model_dump()
    for f in PII_FIELDS:
        if d.get(f):
            d[f] = memory.mask(f, d[f])
    if d.get("email_address_for_summary"):
        d["email_address_for_summary"] = memory.mask("email", d["email_address_for_summary"])
    return {k: v for k, v in d.items() if v not in (None, False, "none")}
