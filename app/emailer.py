"""Email summary for POST_PROCESS.

The body is assembled from the structured case record and the claim data, not
generated free-form, so every fact in it is grounded and no identity data (DOB,
SSN or national ID, phone) is included. Sending is mocked: the email goes to an
in-memory outbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from .deadlines import appeal_deadline_state
from .guidelines import documents_phrase

if TYPE_CHECKING:
    from .models import Session
    from .store import DataStore

PATH_TOPICS = {
    "denial_explanation": "why the claim was denied",
    "claim_status": "the current claim status",
    "appeal_steps": "how to get the decision reviewed again",
    "documents_needed": "the documents the claim needs",
}

STATUS_WORDS = {
    "open": "Open, in progress",
    "closed": "Closed",
    "denied": "Denied",
}


def compose_summary(store: DataStore, s: Session, to: str) -> dict:
    first = store.first_name(s.customer_id)
    claims = [store.get_claim(s.customer_id, cid) for cid in s.case.claims_discussed]
    claims = [c for c in claims if c]

    discussed = ["We verified your identity."]
    topics = ", ".join(PATH_TOPICS[p] for p in s.case.paths_taken if p in PATH_TOPICS)
    for c in claims:
        line = f"Claim {c['case_id']}: {c['summary']} (opened {c['created_at']})"
        discussed.append(line + (f". We covered {topics}." if topics else "."))

    status_lines = []
    next_steps = []
    for c in claims:
        line = f"{c['case_id']}: {STATUS_WORDS.get(c['status'], c['status'])}."
        if c["status"] == "denied" and c.get("denial_reason"):
            reason = c["denial_reason"]
            line += f" Reason: {reason[:1].upper()}{reason[1:]}."
        if c["status"] == "closed" and c.get("net_pay"):
            line += f" Amount paid: ${Decimal(c['net_pay']):,.2f}."
        status_lines.append(line)
        passed = appeal_deadline_state(c, store.today) == "passed"
        if c.get("documents_needed") and not passed:
            next_steps.append(
                f"Send {documents_phrase(c)} for {c['case_id']} through the member portal or the "
                "claim upload link."
            )
        if c.get("appeal_deadline"):
            if passed:
                next_steps.append(
                    f"The appeal deadline for {c['case_id']} ({c['appeal_deadline']}) has passed. "
                    "Please call us to review your options."
                )
            else:
                next_steps.append(f"Appeal deadline for {c['case_id']}: {c['appeal_deadline']}.")
    if not next_steps:
        next_steps.append("No further action is needed from you right now.")

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items)

    body = (
        f"Hi {first},\n\n"
        "Here is a summary of your call with claims support today.\n\n"
        f"What we discussed\n{bullets(discussed)}\n\n"
        f"Claim status\n{bullets(status_lines) if status_lines else '- No claim was reviewed.'}\n\n"
        f"Next steps\n{bullets(next_steps)}\n\n"
        "If anything here looks wrong, reply to this email or call us and we'll help.\n"
    )
    return {
        "to": to,
        "subject": "Summary of your claims support call",
        "body": body,
        "sent_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "session_id": s.session_id,
    }
