"""Shared types: phases, the per-turn extraction schema, and session state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Phase(StrEnum):
    VERIFY_ID = "VERIFY_ID"
    RESOLVE_INTENT = "RESOLVE_INTENT"
    PROCESS_CASE = "PROCESS_CASE"
    POST_PROCESS = "POST_PROCESS"
    HANDOFF = "HANDOFF"
    ENDED = "ENDED"


# The five fields the SOP accepts as identity proof. id_last4 is the last four digits
# of the SSN or national ID, whichever the policyholder has on file (id_type). A policy
# number locates a record but never counts toward the three required matches.
PII_FIELDS = ("full_name", "dob", "phone", "email", "id_last4")

# Bounded workflow paths for PROCESS_CASE. The LLM may pick one; it cannot invent one.
PATHS = ("claim_status", "denial_explanation", "appeal_steps", "documents_needed")

# Thresholds (documented in the README).
MAX_VERIFY_FAILURES = 3
OFF_TOPIC_OFFER_HUMAN = 2  # decision 10b
OFF_TOPIC_HANDOFF = 3
RESISTANCE_OFFER_HUMAN = 2  # decision 11c: same numbers as off-topic
RESISTANCE_HANDOFF = 3


Intent = Literal[
    "claim_status",
    "denial_explanation",
    "appeal_steps",
    "documents_needed",
    "other_in_scope",
    "none",
]
ClaimType = Literal["healthcare", "auto", "home", "life", "dental", "other"]
ClaimStatus = Literal["open", "closed", "denied"]
YesNo = Literal["yes", "no", "unclear", "none"]
Channel = Literal["text", "voice"]

# Emotion readings (decisions 11a, 11b). Each caller message gets a strength for each of
# the five caller emotions, with the caller's own words as evidence. See CONTEXT.md.
EmotionLevel = Literal["none", "mild", "strong"]
EMOTIONS = ("frustration", "anger", "anxiety", "confusion", "refusal")


class EmotionReading(BaseModel):
    level: EmotionLevel = "none"
    words: str | None = Field(None, description="The caller's own words that show it, quoted.")


class EmotionReadings(BaseModel):
    frustration: EmotionReading = Field(default_factory=EmotionReading)
    anger: EmotionReading = Field(default_factory=EmotionReading)
    anxiety: EmotionReading = Field(default_factory=EmotionReading)
    confusion: EmotionReading = Field(default_factory=EmotionReading)
    refusal: EmotionReading = Field(default_factory=EmotionReading)


class Extraction(BaseModel):
    """What the extractor LLM reads out of one caller message. It proposes; code decides."""

    full_name: str | None = Field(None, description="Caller's full name if stated in this message.")
    dob: str | None = Field(None, description="Date of birth normalized to YYYY-MM-DD if stated.")
    phone: str | None = Field(None, description="Phone number if stated, digits only.")
    email: str | None = Field(None, description="Email address if stated as an identity detail.")
    id_last4: str | None = Field(
        None, description="Last four digits of the SSN or national ID, if stated."
    )
    policy_id: str | None = Field(None, description="Policy number if stated, e.g. POL-9921.")
    claim_id: str | None = Field(None, description="Claim or case number if stated, e.g. CL-2048.")
    intent: Intent = Field(
        "none",
        description="What the caller wants done about a claim, if expressed in this message.",
    )
    claim_type: ClaimType | None = Field(None, description="Kind of claim mentioned, if any.")
    claim_month: int | None = Field(
        None, description="Month (1-12) the caller associates with the claim, if mentioned."
    )
    claim_year: int | None = Field(
        None, description="Year the caller associates with the claim, if stated (e.g. 2025)."
    )
    claim_status_hint: ClaimStatus | None = Field(
        None,
        description="Claim status the caller mentions: 'denied' -> denied, 'settled' or 'closed' -> closed, 'in progress' -> open.",
    )
    in_scope: bool = Field(
        True,
        description=(
            "False only when the message asks about something unrelated to insurance customer "
            "service (general knowledge, coding, trivia, other companies). Greetings, thanks, "
            "small talk about the call, identity details and insurance questions are in scope."
        ),
    )
    wants_human: bool = Field(False, description="Caller explicitly asks for a human representative.")
    asks_about_process: bool = Field(
        False,
        description="Caller asks why a step is needed or how the process works (e.g. 'why do you need my DOB?').",
    )
    forbidden_action: bool = Field(
        False,
        description="Caller asks the agent to approve, pay, change, reopen or cancel a claim, or to skip a required step.",
    )
    other_claim: bool = Field(
        False, description="Caller wants to talk about a different claim than the current one."
    )
    done: bool = Field(
        False, description="Caller indicates they have no more questions or want to wrap up."
    )
    yes_no: YesNo = Field(
        "none",
        description="If the agent's last message asked a yes/no question, the caller's answer; otherwise none.",
    )
    lacks_document: bool = Field(
        False,
        description="Caller says they don't have, can't get, or lost a document the claim needs.",
    )
    email_address_for_summary: str | None = Field(
        None, description="An email address the caller wants the summary sent to, if given."
    )
    emotions: EmotionReadings = Field(
        default_factory=EmotionReadings,
        description="Strength of each caller emotion in this message, with the caller's words.",
    )
    question: str | None = Field(
        None, description="The caller's actual question in this message, restated briefly, if any."
    )
    # Representative callers (decision 8a). full_name stays the policyholder's name.
    calling_for_other: bool = Field(
        False, description="Caller says they are calling for someone else (a family member, etc.)."
    )
    rep_name: str | None = Field(
        None, description="The caller's OWN full name when they are acting for someone else."
    )
    relationship: str | None = Field(
        None,
        description="The caller's relationship to the policyholder from the caller's side "
        "(e.g. 'son' when calling for their mother, 'neighbor'), if stated.",
    )
    switch_choice: Literal["verify", "human", "none"] = Field(
        "none",
        description=(
            "Only when the agent offered to verify as the owner of another claim or connect a "
            "person: 'verify' if the caller chooses to verify as that person, 'human' if they "
            "choose a person, otherwise none."
        ),
    )


@dataclass
class MemorySlot:
    value: Any
    source_turn: int
    phase_captured: str
    quote: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source_turn": self.source_turn,
            "phase_captured": self.phase_captured,
            "quote": self.quote,
        }


@dataclass
class CaseRecord:
    """Structured record of what happened, used for the email summary and handoff packet."""

    claims_discussed: list[str] = field(default_factory=list)
    paths_taken: list[str] = field(default_factory=list)
    caller_questions: list[str] = field(default_factory=list)


@dataclass
class Session:
    session_id: str
    phase: Phase = Phase.VERIFY_ID
    channel: Channel = "text"  # how the latest caller message arrived
    turn: int = 0
    transcript: list[dict[str, str]] = field(default_factory=list)
    # Raw identity values live only server side; memory shows masked values.
    identity: dict[str, str] = field(default_factory=dict)
    identity_attempted: tuple | None = None
    verified: bool = False
    customer_id: str | None = None
    matched_fields: list[str] = field(default_factory=list)
    memory: dict[str, MemorySlot] = field(default_factory=dict)
    counters: dict[str, int] = field(
        default_factory=lambda: {"verify_failures": 0, "off_topic": 0, "resistance": 0, "switch_offers": 0}
    )
    pending: str | None = None  # which yes/no question the agent is waiting on
    proposed_claim_id: str | None = None
    memory_hints_used: bool = False
    selected_claim_id: str | None = None
    path: str | None = None
    email_to: str | None = None
    case: CaseRecord = field(default_factory=CaseRecord)
    handoff_packet: dict[str, Any] | None = None
    turn_events: list[dict[str, Any]] = field(default_factory=list)
    # Representative and consent (step 8).
    caller_role: Literal["policyholder", "representative"] = "policyholder"
    rep_name: str | None = None
    rep_relationship: str | None = None
    consent_claim_id: str | None = None
    consent_status: str | None = None  # None, "pending", "approved", "timeout"
    consent_checks: int = 0
    # Read-back of voice-captured identity details (7a): fields waiting for the caller's yes.
    readback_fields: list[str] = field(default_factory=list)
    # Emotion readings per caller message (11a); see emotion.py.
    emotion_log: list[dict[str, Any]] = field(default_factory=list)
    # Index into transcript where the current person's conversation starts (9b switch).
    window_start: int = 0
