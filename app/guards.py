"""Output guards that run on every reply before the caller sees it.

- disclosure_filter: before verification, block any reply that contains a claim value
  from the dataset or claim-shaped content (case IDs like CL-2048, dollar amounts).
- grounding_check: after verification, every claim ID, code, amount and date in the
  reply must appear in the data the controller allowed for this turn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from itertools import combinations
from typing import Any

ID_RE = re.compile(r"\b[A-Z]{2,4}-\d{2,6}\b")
MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
LONG_DATE_RE = re.compile(
    r"\b(" + "|".join(m[:3] for m in MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)


def disclosure_filter(reply: str, sensitive: set[str], caller_id_digits: str | None) -> list[str]:
    low = reply.lower()
    hits = [s for s in sensitive if s and s in low]
    hits += [m for m in ID_RE.findall(reply.upper()) if m.startswith("CL-")]
    if MONEY_RE.search(reply):
        hits.append("dollar amount")
    if caller_id_digits and re.search(rf"\b{re.escape(caller_id_digits)}\b", reply):
        hits.append("echoed ID digits")
    return sorted(set(hits))


def _walk(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def _money(text_amount: str, cents: str | None) -> float:
    return float(text_amount.replace(",", "") + "." + (cents or "00"))


DAYS_RE = re.compile(r"\b(\d{1,3})\s+(business\s+|calendar\s+)?days?\b", re.IGNORECASE)


MONEY_KEY_RE = re.compile(r"amount|net_pay|net_fee", re.IGNORECASE)


def _amount_values(obj: Any, key: str = "") -> Iterable[float]:
    """Money in the data: values under money keys (numbers or decimal strings), plus $ figures in text."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _amount_values(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _amount_values(v, key)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool) and MONEY_KEY_RE.search(key) or isinstance(obj, str) and MONEY_KEY_RE.search(key) and re.fullmatch(r"\d+(\.\d+)?", obj.strip()):
        yield round(float(obj), 2)
    elif isinstance(obj, str):
        for m in MONEY_RE.finditer(obj):
            yield round(_money(m.group(1), m.group(2)), 2)


def grounding_check(reply: str, data: dict[str, Any]) -> list[str]:
    """Return the facts in the reply that cannot be traced to the allowed data.

    This is a tripwire for IDs, codes, money, dates and day counts. It does not judge
    paraphrased meaning; the responder prompt restricts that.
    """
    blob = json.dumps(data, default=str)
    blob_upper = blob.upper()

    amounts = set(_amount_values(data))
    derived = {round(abs(a - b), 2) for a, b in combinations(amounts, 2)}
    dates: set[tuple[int, int, int]] = set()
    for v in _walk(data):
        if isinstance(v, str):
            for m in ISO_DATE_RE.finditer(v):
                dates.add((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    day_counts = {int(m.group(1)) for m in DAYS_RE.finditer(blob)}

    problems: list[str] = []
    for ident in ID_RE.findall(reply):
        if ident.upper() not in blob_upper:
            problems.append(ident)
    for m in MONEY_RE.finditer(reply):
        amt = round(_money(m.group(1), m.group(2)), 2)
        if amt not in amounts and amt not in derived:
            problems.append(m.group(0))
    for m in ISO_DATE_RE.finditer(reply):
        if (int(m.group(1)), int(m.group(2)), int(m.group(3))) not in dates:
            problems.append(m.group(0))
    for m in LONG_DATE_RE.finditer(reply):
        month = [x[:3] for x in MONTHS].index(m.group(1).lower()[:3]) + 1
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        ok = any(d[1] == month and d[2] == day and (year is None or d[0] == year) for d in dates)
        if not ok:
            problems.append(m.group(0))
    for m in DAYS_RE.finditer(reply):
        if int(m.group(1)) not in day_counts:
            problems.append(m.group(0))
    return problems
