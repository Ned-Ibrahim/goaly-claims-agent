"""Deterministic stand-in for the LLM (regex extraction, templated replies).

Used by tests and by the eval suite in fake mode, so everything runs without an API key.
Each feature keeps its own heuristic in its own function.
"""

from __future__ import annotations

import re

from .models import Extraction
from .store import norm_dob

# ---------------------------------------------------------------------------
# Deterministic stand-in
# ---------------------------------------------------------------------------

_NOT_NAMES = {
    "okay", "ok", "sorry", "hi", "hello", "hey", "yes", "no", "thanks", "thank", "just", "this",
    "the", "my", "i'm", "and", "but", "so", "well", "please", "silver", "ppo", "dear", "good",
    "morning", "afternoon", "evening", "social", "security", "national", "pathology", "office",
}
_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def _find(pattern: str, text: str, flags: int = re.IGNORECASE) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1) if m else None


class FakeLLM:
    """Regex extraction plus template replies. Good enough to exercise the controller."""

    def extract(self, system: str, user: str) -> Extraction:
        msg = user.split("CURRENT CALLER MESSAGE:\n", 1)[-1]
        last_agent = ""
        for line in user.split("CURRENT CALLER MESSAGE:")[0].splitlines():
            if line.startswith("ASSISTANT:"):
                last_agent = line
        low = msg.lower()
        ex = Extraction()

        name = _find(r"(?i:my name is|name's|this is|i am|i'm)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", msg, 0)
        if not name:
            for m in re.finditer(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b", msg):
                if not ({m.group(1).lower(), m.group(2).lower()} & _NOT_NAMES):
                    name = m.group(0)
                    break
        ex.full_name = name
        months = "|".join(_MONTHS)
        long_dob = re.search(
            rf"(?:born|birth|dob)\D{{0,15}}((?:{months})\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}\s+(?:{months}),?\s+\d{{4}})",
            low,
        )
        dob = (
            _find(r"(\d{4}-\d{2}-\d{2})", msg)
            or _find(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", msg)
            or _find(r"\b(\d{1,2}-\d{1,2}-\d{4})\b", msg)
        )
        if not dob and long_dob:
            dob = norm_dob(long_dob.group(1).title().replace(",", ""))
        ex.dob = dob
        ex.id_last4 = _find(r"(?:ssn|social|national id|id number|last (?:four|4))[^\d]{0,30}(\d{4})\b", msg)
        ex.phone = _find(r"(?<![\d-])((?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})(?!\d)", msg)
        ex.email = _find(r"([\w.+-]+@[\w-]+\.[\w.]+)", msg)
        ex.policy_id = (_find(r"\b(POL-\d+)\b", msg) or "").upper() or None
        ex.claim_id = (_find(r"\b(CL-\d+)\b", msg) or "").upper() or None

        if re.search(r"\b(denied|denial|why .*(not|n't) (pay|paid|cover))", low):
            ex.intent, ex.claim_status_hint = "denial_explanation", "denied"
        elif "appeal" in low:
            ex.intent = "appeal_steps"
        elif re.search(r"document|paperwork|what do i need to (send|submit)|upload", low):
            ex.intent = "documents_needed"
        elif re.search(r"status|where is my claim", low):
            ex.intent = "claim_status"
        for kw, t in (("health", "healthcare"), ("medical", "healthcare"), ("mri", "healthcare"),
                      ("auto", "auto"), ("car", "auto"), ("home", "home"), ("dental", "dental")):
            if re.search(rf"\b{kw}", low):
                ex.claim_type = t
                break
        if re.search(r"\b(closed|settled)\b", low):
            ex.claim_status_hint = "closed"
        month_text = low.replace(long_dob.group(1), "") if long_dob else low
        for i, m in enumerate(_MONTHS):
            if re.search(rf"\b({m}|{m[:3]})\b", month_text):
                ex.claim_month = i + 1
                year = re.search(rf"\b(?:{m}|{m[:3]})\s+(?:of\s+)?(20\d\d)\b", month_text)
                ex.claim_year = int(year.group(1)) if year else None
                break
        ex.lacks_document = bool(re.search(r"(don't|do not|can't|cannot|couldn't) (have|get|find)|lost (the|my)", low))

        ex.in_scope = not re.search(
            r"what is rl|reinforcement learning|weather|capital of|write (me )?a (poem|python|script|program)|python (code|script)|stock price|recipe|joke|who won",
            low,
        )
        ex.wants_human = bool(re.search(r"\b(human|real person|representative|supervisor|manager)\b", low)) and not re.search(r"\bno\b", low[:4])
        ex.refuses_identity = bool(re.search(r"already told you|not giving|won't give|wont give|not going to give|don't want to (give|share)|refuse|i said no|not telling", low))
        ex.asks_about_process = bool(re.search(r"why do you need|why do i have to|what for\?", low))
        ex.forbidden_action = bool(re.search(r"approve (my|the|it)|just pay|skip (the )?verification|ignore (all |your |previous |prior )+instructions|^\s*system:|admin mode|i'm an admin|verification (is )?(complete|completed|done)|reopen|cancel (my|the) claim", low))
        ex.other_claim = bool(re.search(r"another claim|other claim|different claim", low))
        ex.done = bool(re.search(r"that's all|thats all|nothing else|i'm good|im good|that is all|no more questions", low))
        if re.search(r"^\s*(yes|yeah|yep|sure|please do|go ahead|ok|okay|correct|that's (it|the one|right))\b", low):
            ex.yes_no = "yes"
        elif re.search(r"^\s*(no|nope|nah|skip|don't|do not|not now)\b", low):
            ex.yes_no = "no"
        elif last_agent.rstrip().endswith("?") and re.search(r"\b(maybe|not sure|what's in it|what would)\b", low):
            ex.yes_no = "unclear"
        ex.email_address_for_summary = ex.email if "send" in low or "email it" in low else None
        if ex.email_address_for_summary:
            ex.email = None
        if re.search(r"ridiculous|frustrat|annoy|unbelievable|come on", low):
            ex.emotion = "frustrated"
        elif re.search(r"angry|furious|outrageous|unacceptable", low):
            ex.emotion = "angry"
        elif re.search(r"worried|anxious|scared|nervous", low):
            ex.emotion = "anxious"
        elif re.search(r"confus|don't understand|what do you mean", low):
            ex.emotion = "confused"
        if "?" in msg:
            ex.question = msg.strip()[:160]
        return ex

    def respond(self, system: str, transcript: list[dict[str, str]]) -> str:
        plan = system.split("TURN PLAN (do all of these, in order):\n", 1)[-1].split("\n\nALLOWED DATA:")[0]
        data = system.split("ALLOWED DATA:\n", 1)[-1]
        parts = []
        if "upset" in plan:
            parts.append("I understand this is frustrating, and I'm sorry.")
        if "outside insurance" in plan:
            parts.append("I'm sorry, I can only help with questions about your insurance and claims.")
        if "don't match our records" in plan:
            parts.append("Something didn't match our records, and one detail may be mistyped. Could you share any three of: full name, date of birth, phone, email, or the last four of your SSN or national ID again?")
        elif "verify their identity" in plan or "Ask for" in plan:
            parts.append("To protect your account, I need to verify your identity first. Could you share any three of: full name, date of birth, phone, email, or the last four of your SSN or national ID?")
        if "Identity is now verified" in plan:
            parts.append("Thank you, you're verified.")
        if "summarize it in one line" in plan:
            cid = _find(r'"proposed_claim": \{\s*"case_id": "([A-Z]+-\d+)"', data, 0)
            parts.append(f"I found claim {cid}. Is that the one you're calling about?")
        if "More than one claim" in plan:
            parts.append("I see more than one claim that fits. Which one do you mean?")
        if "why this claim was denied" in plan:
            reason = _find(r'"denial_reason": "([^"]+)"', data, 0)
            parts.append(f"Your claim was denied because {reason}.")
        if "current status" in plan:
            status = _find(r'"status": "([a-z_]+)"', data, 0)
            parts.append(f"Your claim's current status is {status}.")
        if "reviewed again, using only" in plan:
            parts.append("To have it reviewed again, send the missing documents through the member portal before the deadline.")
        if "which documents this claim needs" in plan:
            docs = re.findall(r'"document": "([^"]+)"', data)
            parts.append(f"This claim needs: {', '.join(docs)}." if docs else "This claim doesn't need any documents.")
        if "set answer for this follow-up" in plan:
            parts.append(_find(r'"answer": "([^"]+)"', data, 0) or "")
        if "offer to email a summary" in plan:
            parts.append("Would you like me to email you a summary of today's call, or skip it?")
        if "summary was sent" in plan:
            parts.append("Done, the summary is on its way. Thanks for calling!")
        if "no email will be sent" in plan:
            parts.append("No problem, I won't send an email. Thanks for calling!")
        if "human representative now" in plan:
            parts.append("I'm connecting you with a human representative now.")
        if "Ask what they'd like help with" in plan:
            parts.append("How can I help with your claims today?")
        if "anything else" in plan:
            parts.append("Is there anything else I can help with?")
        if "Offer to connect them with a human" in plan or "Ask directly whether" in plan:
            parts.append("Would you like me to connect you with a human representative?")
        return " ".join(parts) or "How can I help you with your claim today?"
