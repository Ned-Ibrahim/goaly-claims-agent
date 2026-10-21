"""One caller turn, end to end:

    extract (LLM, structured) -> remember (passive) -> SOP step (code) -> respond (LLM)
    -> guard (code) -> reply

Control decisions happen only in SOPController.step(). The LLM is used twice per
turn, both times with no authority: once to read the caller, once to phrase the reply.
"""

from __future__ import annotations

from typing import Any

from . import memory, pii
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

    def handle(
        self, s: Session, message: str, llm: LLM, channel: str | None = None, heard: str | None = None
    ) -> dict[str, Any]:
        s.turn += 1
        s.turn_events = []
        if channel:  # None keeps the session's channel (the server always passes one)
            s.channel = channel
        if heard:
            self._mark_interrupted(s, heard)

        # Identity details are read out by code first; the model sees only the masked text.
        parsed = pii.parse_identity(message, asked=self._last_agent_line(s))
        masked = parsed.masked
        summary_email = None
        if s.phase == Phase.POST_PROCESS and parsed.values.get("email"):
            summary_email = parsed.values.pop("email")  # an address for the summary, not identity
        vals = self._identity_values(s, {**parsed.values, **({"email": summary_email} if summary_email else {})})

        if s.phase in (Phase.HANDOFF, Phase.ENDED):
            plan = self.controller.step(s, Extraction())
            s.transcript.append({"role": "user", "content": masked})
            s.transcript.append({"role": "assistant", "content": plan.static_reply})
            return self.result(s, plan.static_reply, None, vals)

        claims = self._claims_for_extract(s)
        system = self._clean(s, extractor_system(claims), vals, "extract_system")
        user = self._clean(s, extractor_user(memory.recent_turns(s, 8), masked), vals, "extract_user")
        try:
            ex = llm.extract(system, user)
        except LLMError as e:
            self.controller.ev(s, "guard", name="extractor", action=f"fallback ({e})")
            ex = Extraction()
        s.transcript.append({"role": "user", "content": masked})

        # The model's values for identity fields are ignored: code's parse is the only source.
        for f in ("dob", "phone", "email", "id_last4"):
            setattr(ex, f, parsed.values.get(f))
        ex.email_address_for_summary = summary_email

        for slot in memory.write(s, ex, masked):
            self.controller.ev(s, "memory_write", slot=slot)
        if ex.in_scope:
            self.controller.ev(s, "scope", in_scope=True)

        plan = self.controller.step(s, ex)
        reply = self.respond(s, plan, llm, masked, vals)
        stored = plan.transcript_text or reply
        s.transcript.append({"role": "assistant", "content": stored})
        return self.result(s, reply, ex, vals, readback=bool(plan.transcript_text))

    def _claims_for_extract(self, s: Session) -> list[dict] | None:
        """Claims the extract model may see. A representative sees none until the policyholder
        approves a claim, and then only that claim (decision 8a)."""
        if not s.verified:
            return None
        claims = self.store.list_claims(s.customer_id)
        if s.caller_role != "representative":
            return claims
        if s.consent_status == "approved" and s.consent_claim_id:
            return [c for c in claims if c["case_id"] == s.consent_claim_id]
        return None

    # ---- identity values and the egress check ---------------------------------

    @staticmethod
    def _last_agent_line(s: Session) -> str | None:
        for t in reversed(s.transcript):
            if t["role"] == "assistant":
                return t["content"]
        return None

    @staticmethod
    def _identity_values(s: Session, parsed: dict[str, str]) -> list[str]:
        """Every raw identity value that must never leave: stored ones plus this turn's."""
        keep = ("dob", "phone", "email", "id_last4")
        stored = [s.identity[f] for f in keep if s.identity.get(f)]
        return list(dict.fromkeys([*stored, *[parsed[f] for f in keep if parsed.get(f)]]))

    def _egress(self, s: Session, where: str, hits: list[str]) -> None:
        """Log a hit without the value."""
        self.controller.ev(s, "guard", name="pii_egress", action="blocked", where=where, count=len(hits))

    def _clean(self, s: Session, text: str, vals: list[str], where: str) -> str:
        hits = pii.egress_hits(text, vals)
        if not hits:
            return text
        self._egress(s, where, hits)
        return pii.scrub(text, vals)

    def _mark_interrupted(self, s: Session, heard: str) -> None:
        """The caller cut the agent off: keep only what was heard (3b), masked."""
        for t in reversed(s.transcript):
            if t["role"] == "assistant":
                t["content"] = pii.sweep(pii.scrub(heard.strip(), self._identity_values(s, {}))) + " [interrupted]"
                return

    # ---- reply generation with guards ------------------------------------

    def respond(
        self, s: Session, plan: Plan, llm: LLM, message: str, vals: list[str] | None = None
    ) -> str:
        vals = vals if vals is not None else self._identity_values(s, {})
        if plan.static_reply:
            return plan.static_reply
        system = responder_system(plan.phase, plan.instructions, plan.data, known=memory.known_so_far(s))
        system = self._clean(s, system, vals, "respond_system")
        for attempt in range(2):
            try:
                msgs = [
                    {**t, "content": self._clean(s, t["content"], vals, "respond_messages")}
                    for t in memory.recent_turns(s)
                ]
                reply = llm.respond(system, msgs)
            except LLMError as e:
                self.controller.ev(s, "guard", name="responder", action=f"fallback ({e})")
                return plan.fallback
            problems = self.check(s, plan, reply, message)
            hits = pii.egress_hits(reply, vals)
            if hits:
                self._egress(s, "reply", hits)
                problems = [*problems, "identity details"]
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
    def _nothing_disclosable(s: Session) -> bool:
        """No claim may be disclosed yet: not verified, or a representative without approved consent."""
        if not s.verified:
            return True
        return s.caller_role == "representative" and s.consent_status != "approved"

    @classmethod
    def _guard_name(cls, s: Session) -> str:
        return "disclosure_filter" if cls._nothing_disclosable(s) else "grounding"

    def check(self, s: Session, plan: Plan, reply: str, message: str) -> list[str]:
        if self._nothing_disclosable(s):
            problems = disclosure_filter(reply, self.sensitive, s.identity.get("id_last4"))
        else:
            problems = grounding_check(reply, plan.data)
        self.controller.ev(
            s, "gate", name=self._guard_name(s), passed=not problems,
            detail=", ".join(problems) if problems else "clean",
        )
        return problems

    # ---- API shape -----------------------------------------------------------

    def result(
        self, s: Session, reply: str | None, ex: Extraction | None,
        vals: list[str] | None = None, readback: bool = False,
    ) -> dict[str, Any]:
        out = state(s)
        out["reply"] = reply
        out["events"] = list(s.turn_events)
        out["extraction"] = _masked_extraction(ex) if ex else None
        vals = vals if vals is not None else self._identity_values(s, {})
        return self._egress_result(s, out, vals, readback)

    def _egress_result(self, s: Session, out: dict[str, Any], vals: list[str], readback: bool) -> dict[str, Any]:
        """Last line: mask any identity value anywhere in the response. Only the code-written
        read-back may carry them, and only in "reply"."""
        count = 0

        def walk(v: Any) -> Any:
            nonlocal count
            if isinstance(v, str):
                hits = pii.egress_hits(v, vals)
                count += len(hits)
                return pii.scrub(v, vals) if hits else v
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v

        reply = out.get("reply")
        scrubbed = walk({k: x for k, x in out.items() if k != "reply" or not readback})
        if readback:
            scrubbed["reply"] = reply
        if count:
            self.controller.ev(s, "guard", name="pii_egress", action="masked", where="response", count=count)
            scrubbed["events"] = [*scrubbed["events"], s.turn_events[-1]]
        return scrubbed


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
        "caller_role": s.caller_role,
        "consent_status": s.consent_status,
        "path": s.path,
        "handoff_packet": s.handoff_packet,
        "transcript": [{"role": t["role"], "content": t["content"]} for t in s.transcript],
    }


def _masked_extraction(ex: Extraction) -> dict[str, Any]:
    """The extraction for the API: identity fields masked, free text swept of raw values."""
    d = ex.model_dump()
    for f in PII_FIELDS:
        if d.get(f):
            d[f] = memory.mask(f, d[f])
    if d.get("email_address_for_summary"):
        d["email_address_for_summary"] = memory.mask("email", d["email_address_for_summary"])
    for k, v in d.items():
        if isinstance(v, str) and k not in PII_FIELDS and k != "email_address_for_summary":
            d[k] = pii.sweep(v)
    return {k: v for k, v in d.items() if v not in (None, False, "none")}
