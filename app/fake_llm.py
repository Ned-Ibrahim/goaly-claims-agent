"""Deterministic stand-in for the LLM (regex extraction, templated replies).

Used by tests and by the eval suite in fake mode, so everything runs without an API key.
Each feature keeps its own heuristic in its own function.
"""

from __future__ import annotations

import re

from .models import EmotionReading, EmotionReadings, Extraction
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


# (emotion, level, pattern). First matching pattern per emotion wins; strong patterns come first.
_EMOTION_PATTERNS = [
    ("anger", "strong", r"useless|idiot|stupid|incompetent|damn|\bhell\b|\bshit\b|\bfuck\w*|\bcrap\b|furious|outrageous"),
    ("anger", "mild", r"unacceptable|angry"),
    ("frustration", "mild", r"already told you|ridiculous|just tell me|frustrat|annoy|unbelievable|come on"),
    ("anxiety", "mild", r"worried|scared|anxious|nervous|can't afford|cant afford|deadline"),
    ("confusion", "mild", r"what do you mean|i don't understand|i dont understand|confus"),
    ("refusal", "strong", r"not giving you|won't give|wont give|i'm not telling|im not telling|not going to give|i refuse|i said no|still not (?:telling|giving)|not telling you"),
    ("refusal", "mild", r"why do you need|why do i have to"),
]


def _fake_emotions(text: str) -> EmotionReadings:
    """Phrase heuristic for the five emotion readings (decision 11a)."""
    low = text.lower()
    out: dict[str, EmotionReading] = {}
    for name, level, pattern in _EMOTION_PATTERNS:
        if name in out:
            continue
        m = re.search(pattern, low)
        if m:
            out[name] = EmotionReading(level=level, words=m.group(0))
    return EmotionReadings(**out)


def _fake_question_answer(question: str, data: str) -> str:
    """A grounded one-line answer for a free question: the appeal deadline if shown, else an honest gap."""
    deadline = _find(r'"appeal_deadline": "([^"]+)"', data, 0)
    if deadline and re.search(r"deadline|by when", question):
        return f"The appeal deadline is {deadline}."
    return "I don't have that information here, but I can connect you with a human representative."


def _fake_switch_choice(low: str) -> str:
    """The answer to the offer to verify as another claim's owner (6c)."""
    if re.search(r"\b(human|person|representative)\b", low):
        return "human"
    if re.search(r"verify as|as the owner|as them|as him|as her|go back", low):
        return "verify"
    return "none"


_RELATIONS = (
    "son|daughter|mother|father|wife|husband|brother|sister|neighbor|friend|nephew|niece|"
    "grandson|granddaughter|caregiver|attorney|aunt|uncle|cousin"
)
# "calling for my mother" names the policyholder's role; the caller's own is the other side.
_INVERSE_RELATION = {"mother": "child", "father": "child", "wife": "spouse", "husband": "spouse",
                     "brother": "sibling", "sister": "sibling"}


def _fake_representative(msg: str, ex: Extraction) -> None:
    """Phrase heuristic for a caller acting for someone else (decision 8a).

    "I'm David Chen, calling for my mother Margaret Chen" gives rep_name David Chen,
    relationship child, and full_name Margaret Chen (the policyholder).
    """
    m = re.search(r"calling\s+(?:for|on behalf of)\s+(.*)", msg, re.IGNORECASE | re.DOTALL)
    if not m:
        # "I'm David Chen, her son": a later turn that only names the caller.
        rel = re.search(rf"\b(?:her|his|their)\s+({_RELATIONS})\b", msg, re.IGNORECASE)
        if rel and ex.full_name:
            ex.calling_for_other, ex.rep_name, ex.full_name = True, ex.full_name, None
            ex.relationship = rel.group(1).lower()
        return
    ex.calling_for_other = True

    def names(text: str) -> list[str]:
        return [
            f"{a} {b}" for a, b in re.findall(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b", text)
            if not ({a.lower(), b.lower()} & _NOT_NAMES)
        ]

    holder = names(m.group(1))
    ex.full_name = holder[0] if holder else None
    own = [n for n in names(msg) if n not in holder]
    ex.rep_name = own[0] if own else None
    direct = re.search(rf"\b(?:her|his|their)\s+({_RELATIONS})\b", msg, re.IGNORECASE)
    named = re.search(rf"\bmy\s+({_RELATIONS})\b", m.group(1), re.IGNORECASE)
    if direct:
        ex.relationship = direct.group(1).lower()
    elif named:
        rel = named.group(1).lower()
        ex.relationship = _INVERSE_RELATION.get(rel, rel)


def _fake_consent_reply(plan: str) -> list[str]:
    """Template lines for the representative and consent instructions (step 8)."""
    parts = []
    if "own name and how they are related" in plan:
        parts.append("Could you also tell me your own name and how you're related to the policyholder?")
    if "confirmed as someone on file" in plan:
        parts.append("Thank you, I can help you with that.")
    if "Ask which claim number" in plan:
        parts.append("Which claim number would you like to discuss?")
    if "waiting on the policyholder" in plan:
        parts.append("I've asked the policyholder to approve this, and I'm waiting on their answer.")
    if "approval request timed out" in plan:
        parts.append("The policyholder hasn't answered, so I can't share anything. Would you like me to connect you with a human representative?")
    if "approval did not come back" in plan:
        parts.append("I still can't share anything without the policyholder's approval. Would you like me to connect you with a human representative?")
    if "can only discuss a policy" in plan:
        parts.append("I can only discuss a policy with the policyholder or someone already on file for them. The person I'm connecting you with can help add you.")
    if "has approved this for this claim" in plan:
        parts.append("The policyholder has approved it.")
    return parts


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
        ex.asks_about_process = bool(re.search(r"why do you need|why do i have to|what for\?", low))
        ex.forbidden_action = bool(re.search(r"approve (my|the|it)|just pay|skip (the )?verification|ignore (all |your |previous |prior )+instructions|^\s*system:|admin mode|i'm an admin|verification (is )?(complete|completed|done)|reopen|cancel (my|the) claim", low))
        ex.other_claim = bool(re.search(r"another claim|other claim|different claim|\balso\b.*\b(auto|home|dental)\b.*claim", low))
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
        ex.emotions = _fake_emotions(msg)
        _fake_representative(msg, ex)
        ex.switch_choice = _fake_switch_choice(low)
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
        asked = re.search(r'Answer the caller\'s question \("([^"]*)"\)', plan)
        if asked:
            parts.append(_fake_question_answer(asked.group(1).lower(), data))
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
        if "verify again as the person" in plan:
            parts.append("I can't help with that claim under your verification. I can verify you as the person it belongs to, or connect you with a human. Which would you prefer?")
        if "verify them as the person" in plan:
            parts.append("Okay, let's verify you as that person. Could you share any three of the identity details?")
        if "Ask what they'd like help with" in plan:
            parts.append("How can I help with your claims today?")
        if "anything else" in plan:
            parts.append("Is there anything else I can help with?")
        if "Offer to connect them with a human" in plan or "Ask directly whether" in plan:
            parts.append("Would you like me to connect you with a human representative?")
        parts.extend(_fake_consent_reply(plan))
        return " ".join(parts) or "How can I help you with your claim today?"
