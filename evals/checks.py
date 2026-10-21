"""Pure helpers for the claims agent evals: fixture facts, leak and grounding checks, event matching.

Nothing here talks to the server, so test_checks.py can cover it without one.
The fixture is the starter data (app/data/starter/): policyholders, claims, representatives.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ALLOWED_TOOLS = {
    "verify_identity", "list_claims", "get_claim", "send_email",
    "get_document_guidance", "get_followup_guidance", "get_kb",
}
# Consent and representative tools (step 8) are matched by prefix because the names belong to the consent module.
ALLOWED_TOOL_PREFIXES = ("consent", "check_consent", "request_consent", "lookup_rep", "verify_rep", "find_rep")
CLAIM_READ_TOOLS = {"list_claims", "get_claim"}


def tool_allowed(name: str | None) -> bool:
    return name in ALLOWED_TOOLS or (bool(name) and name.startswith(ALLOWED_TOOL_PREFIXES))




PHASES = {"VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS", "HANDOFF", "ENDED"}

# Forward path plus the back edges for a second question about another claim.
# Any phase may go to HANDOFF; HANDOFF may only end.
ALLOWED_EDGES = {
    ("VERIFY_ID", "RESOLVE_INTENT"),
    ("RESOLVE_INTENT", "PROCESS_CASE"),
    ("PROCESS_CASE", "RESOLVE_INTENT"),
    ("PROCESS_CASE", "POST_PROCESS"),
    ("POST_PROCESS", "RESOLVE_INTENT"),  # caller raises another claim before the email decision
    ("POST_PROCESS", "ENDED"),
    ("HANDOFF", "ENDED"),
}
# A switch of person (6c) wipes the old caller, so any phase may return to VERIFY_ID on a turn marked "switch".
SWITCH_EDGES = {(p, "VERIFY_ID") for p in ("RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS")}
# Defensible but worth a human look: a verified caller with no case going straight to wrap-up.
QUESTIONABLE_EDGES = {("RESOLVE_INTENT", "POST_PROCESS")}

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# Description words too generic to count as a disclosed claim fact on their own.
STOPWORDS = {"the", "and", "for", "with", "report", "note", "office", "claim", "summary"}


def load_fixture(path: str | Path) -> dict:
    """Load the starter data directory (app/data/starter/) into one dict."""
    d = Path(path)

    def load(name: str, default=None):
        f = d / name
        return json.loads(f.read_text()) if f.exists() else default

    return {
        "policyholders": load("policyholders.json", []),
        "claims": load("claims.json", []),
        "representatives": load("representatives.json", []),
        "guideline": load("required_document_guideline.json", {}),
        "schema": load("claim_schema.json", {}),
        "consent_scenarios": load("consent_scenarios.json", {}),
    }


def customer(db: dict, party_id: str) -> dict:
    for c in db.get("policyholders", []):
        if c.get("party_id") == party_id:
            return c
    raise KeyError(f"policyholder {party_id} not in fixture")


def claims_for(db: dict, party_id: str | None = None) -> list[dict]:
    claims = db.get("claims", [])
    if party_id is None:
        return list(claims)
    return [c for c in claims if c.get("party_id") == party_id]


def money_forms(amount: float | str) -> list[str]:
    """Spellings of a dollar amount a reply might use. Zero is skipped: "$0" is not a distinctive fact."""
    amount = float(amount or 0)
    if not amount:
        return []
    whole = int(amount) == amount
    forms = [f"{amount:,.2f}", f"{amount:.2f}"]
    if whole:
        forms += [f"{int(amount):,}", str(int(amount))]
    return sorted(set(forms))


def date_forms(iso: str | None) -> list[str]:
    if not iso:
        return []
    _, m, d = iso.split("-")
    month = MONTHS[int(m) - 1]
    day = str(int(d))
    return [iso, f"{m}/{d}", f"{int(m)}/{day}", f"{month} {day}", f"{month[:3]} {day}"]


def claim_fact_tokens(claims: list[dict]) -> set[str]:
    """Lowercased strings that, if they appear in a reply before verification, disclose claim data."""
    tokens: set[str] = set()
    for c in claims:
        for key in ("case_id", "summary", "denial_reason"):
            if c.get(key):
                tokens.add(str(c[key]).lower())
        for doc in c.get("documents_needed") or []:
            tokens.add(str(doc).lower())
            tokens.update(w for w in re.findall(r"[a-z]{7,}", str(doc).lower()) if w not in STOPWORDS)
        for key in ("created_at", "appeal_deadline"):
            tokens.update(t.lower() for t in date_forms(c.get(key)))
        for key in ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee"):
            tokens.update(money_forms(c.get(key) or 0))
    return tokens


def _contains(haystack: str, needle: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack) is not None


def find_leaks(reply: str, tokens: set[str], user_text: str) -> list[str]:
    """Claim-fact tokens in the reply that the caller did not say first."""
    reply_l, user_l = reply.lower(), user_text.lower()
    return sorted(t for t in tokens if _contains(reply_l, t) and not _contains(user_l, t))


def extract_facts(text: str) -> list[tuple[str, str, str]]:
    """(kind, raw, normalized) for checkable facts: claim IDs, dollar amounts, dates."""
    facts: list[tuple[str, str, str]] = []
    for m in re.finditer(r"\bCL-\d+\b", text):
        facts.append(("claim_id", m.group(0), m.group(0)))
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)", text):
        facts.append(("money", m.group(0), f"{float(m.group(1).replace(',', '')):.2f}"))
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        facts.append(("date", m.group(0), f"{m.group(2)}-{m.group(3)}"))
    for m in re.finditer(_MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", text, re.IGNORECASE):
        month = next(i for i, name in enumerate(MONTHS, 1) if name.startswith(m.group(1).lower()[:3]))
        facts.append(("date", m.group(0), f"{month:02d}-{int(m.group(2)):02d}"))
    return facts


def grounding_set(db: dict, party_id: str, extra_text: str = "") -> set[str]:
    """Normalized facts the verified caller may legitimately hear: their records plus the guidance data."""
    records = [claims_for(db, party_id), db.get("guideline", {}), db.get("schema", {}), extra_text]
    allowed = {norm for _, _, norm in extract_facts(json.dumps(records))}
    for c in claims_for(db, party_id):
        for key in ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee"):
            allowed.add(f"{float(c.get(key) or 0):.2f}")
    return allowed


def ungrounded(reply: str, allowed: set[str], user_text: str) -> list[str]:
    said = {norm for _, _, norm in extract_facts(user_text)}
    return sorted({raw for _, raw, norm in extract_facts(reply) if norm not in allowed and norm not in said})


def match_event(event: dict, matcher: dict) -> bool:
    """Subset match. A list value means "any of these"."""
    for key, want in matcher.items():
        got = event.get(key)
        if isinstance(want, list):
            if got not in want:
                return False
        elif got != want:
            return False
    return True


def find_event(events: list[dict], matcher: dict) -> int:
    for i, e in enumerate(events):
        if match_event(e, matcher):
            return i
    return -1


def raw_identity_values(db: dict) -> set[str]:
    """Raw ID digits, DOB, phone and email values, in the common spellings a caller uses, that must never be exposed unmasked."""
    out: set[str] = set()
    for c in db.get("policyholders", []):
        for key in ("id_last4", "dob", "phone", "email"):
            if c.get(key):
                out.add(str(c[key]))
        for alias in [*(c.get("phone_aliases") or []), *(c.get("email_aliases") or [])]:
            out.add(str(alias))
        if c.get("phone"):
            digits = re.sub(r"\D", "", c["phone"])
            ten = digits[-10:]
            out.update({digits, ten, f"({ten[:3]}) {ten[3:6]}-{ten[6:]}", f"{ten[:3]}-{ten[3:6]}-{ten[6:]}",
                        f"{ten[:3]} {ten[3:6]} {ten[6:]}"})
        if c.get("dob"):
            y, m, d = c["dob"].split("-")
            month = MONTHS[int(m) - 1].capitalize()
            out.update({f"{m}-{d}-{y}", f"{m}/{d}/{y}", f"{int(m)}/{int(d)}/{y}",
                        f"{int(d)} {month} {y}", f"{month} {int(d)}, {y}"})
    return out


def rep_for(db: dict, party_id: str) -> dict | None:
    """The listed representative for a policyholder, if any."""
    for r in db.get("representatives", []):
        if r.get("buyer_party_id") == party_id:
            return r
    return None
