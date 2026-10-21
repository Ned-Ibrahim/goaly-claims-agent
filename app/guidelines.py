"""Document guidance and follow-up answers from required_document_guideline.json.

Code picks which guidance applies and fills the templates; the responder only phrases
the result. That keeps "how soon", "how long" and "what if I don't have it" answers
identical to the company's wording instead of whatever the model guesses.
"""

from __future__ import annotations

from typing import Any


def _en(entry: dict | None) -> str | None:
    return entry.get("en") if entry else None


def _guideline_key(name: str, keys: list[str]) -> str | None:
    """Claims say "pathology report"; the guideline says "original pathology report"."""
    n = name.strip().lower()
    for k in keys:
        if k == n:
            return k
    for k in keys:
        if n in k or k in n:
            return k
    return None


def documents_phrase(claim: dict) -> str:
    docs = claim.get("documents_needed") or []
    named = [f"the {d}" for d in docs]
    if len(named) <= 1:
        return "".join(named)
    return ", ".join(named[:-1]) + " and " + named[-1]


def document_guidance(g: dict[str, Any], claim: dict) -> dict[str, Any]:
    """What each needed document must show, what to do if it's missing, and how to submit."""
    per_doc = g.get("document_guidance", {})
    alternatives = g.get("document_alternative_guidance", {})
    documents = []
    for d in claim.get("documents_needed") or []:
        key = _guideline_key(d, list(per_doc))
        documents.append({
            "document": d,
            "what_it_should_show": _en(per_doc.get(key)) if key else None,
            "if_you_dont_have_it": _en(alternatives.get(key)) if key else _en(alternatives.get("default")),
        })
    return {
        "documents": documents,
        "how_to_submit": _en(g.get("default_guidance")),
        "for_this_claim_type": _en(g.get("case_type_guidance", {}).get(claim.get("case_type"))),
    }


def followup_answer(g: dict[str, Any], claim: dict, text: str, lacks_document: bool) -> dict[str, str] | None:
    """The guideline answer for a follow-up question, or None if no template applies.

    Templates are tried in file order. One with match_any phrases applies when the
    caller's words contain one of them; the one without phrases (missing material
    alternatives) applies when the caller says they don't have a document.
    """
    docs = claim.get("documents_needed") or []
    low = text.lower()
    settings = g.get("claim_followup_settings", {})
    slots = {
        "case_id": claim["case_id"],
        "documents": documents_phrase(claim),
        "average_processing_time_after_submission": _en(
            settings.get("average_processing_time_after_submission")
        ) or "",
    }
    for t in g.get("claim_followup_guidance", []):
        if t.get("requires_documents") and not docs:
            continue
        phrases = t.get("match_any")
        if (phrases and any(p in low for p in phrases)) or (not phrases and lacks_document):
            return {"topic": t["topic"], "answer": t["en"].format(**slots)}
    return None


def followup_fallback(g: dict[str, Any]) -> str | None:
    return _en(g.get("claim_followup_fallback"))


def human_review_note(g: dict[str, Any]) -> str | None:
    return _en(g.get("claim_followup_settings", {}).get("human_review_after_document_alternatives_exhausted"))
