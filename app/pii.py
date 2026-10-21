"""Identity details never reach the model (ADR 0003, option A).

Code reads DOB, ID last four, phone and email out of the caller's text before any
model call. The model sees placeholders only. Three layers:

1. normalize spoken forms (digit words, spoken email, spoken dates);
2. per-detail patterns anchored on nearby words;
3. an optional Presidio pass, then a fail-closed sweep for anything number- or
   email-shaped that is left.

Names are not parsed here: the model sees names.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Iterable

from .store import norm_dob

PLACEHOLDERS = {
    "dob": "[DOB given]",
    "phone": "[phone given]",
    "email": "[email given]",
    "id_last4": "[ID last 4 given]",
}


@dataclass
class ParseResult:
    masked: str  # caller text with each identity value replaced by its placeholder
    values: dict[str, str] = field(default_factory=dict)  # dob YYYY-MM-DD, phone digits, email lower, id_last4


# ---- layer 1: spoken forms ---------------------------------------------------------

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_REPEAT = {"double": 2, "triple": 3}
_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ORD_UNITS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
}
_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_MONTH_RE = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_SEP = r"[\s,.\-]+"


def _alt(words: Iterable[str]) -> str:
    return "(?:" + "|".join(sorted(words, key=len, reverse=True)) + ")"


_DW = r"\b(?:" + _alt(_DIGIT_WORDS) + r"|" + _alt(_REPEAT) + r"\s+" + _alt(_DIGIT_WORDS) + r"|\d)\b"
_RUN = re.compile(rf"(?i){_DW}(?:{_SEP}{_DW})+")


def _run_to_digits(run: str) -> str | None:
    """Digits for a run of digit words, or None when it is not clearly a digit run."""
    toks = re.findall(r"[A-Za-z]+|\d", run)
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i].lower()
        if t in _REPEAT and i + 1 < len(toks):
            out.append(_DIGIT_WORDS.get(toks[i + 1].lower(), toks[i + 1]) * _REPEAT[t])
            i += 2
            continue
        out.append(_DIGIT_WORDS.get(t, t))
        i += 1
    digits = "".join(out)
    # "oh" at either end is usually the interjection, not a zero.
    first, last = toks[0].lower(), toks[-1].lower()
    if (first == "oh" or last == "oh") and len(digits) < 5:
        return None
    return digits if len(digits) >= 3 else None


def digit_words_to_digits(text: str) -> str:
    """'four four oh two' -> '4402'. Runs of 3 or more digit words only ('two weeks' stays)."""

    def sub(m: re.Match) -> str:
        d = _run_to_digits(m.group(0))
        return d if d else m.group(0)

    return _RUN.sub(sub, text)


_EMAIL_STOP = {
    "my", "email", "e-mail", "mail", "address", "is", "its", "it's", "the", "and", "send", "to", "it",
    "me", "use", "a", "an", "please", "or", "yes", "no", "okay", "ok", "that's", "thats", "now", "i",
    "im", "i'm", "on", "file", "also", "can", "you", "reach", "contact", "at", "with", "for", "from",
}
_TLDS = {"com", "org", "net", "edu", "gov", "io", "co", "us", "uk", "info", "biz", "ca", "de", "me", "ai", "app", "dev"}
_LOCAL_WORDS = {"dot": ".", "underscore": "_", "dash": "-", "hyphen": "-", "plus": "+"}
_SPOKEN_EMAIL = re.compile(
    r"(?i)(?:(?<![\w@])at\s+|\s*@\s*)([a-z0-9-]+(?:(?:\s+dot\s+|\.)[a-z0-9-]+)+)(?![\w@-])"
)
_SPELLED_TLD = re.compile(r"(?i)\bdot\s+((?:[a-z]\s+){1,3}[a-z])(?![A-Za-z])")
_MAX_WORDS, _MAX_LETTERS = 6, 40


def _unspell_tlds(text: str) -> str:
    """'dot c o m' -> 'dot com'."""

    def sub(m: re.Match) -> str:
        word = re.sub(r"\s+", "", m.group(1)).lower()
        return f"dot {word}" if word in _TLDS else m.group(0)

    return _SPELLED_TLD.sub(sub, text)


def _local_part(before: str) -> tuple[list[str], int]:
    """The words just before 'at', as address pieces, and where they start in `before`."""
    words = list(re.finditer(r"[A-Za-z0-9_.+-]+'?[a-z]*", before))
    local: list[str] = []
    start = len(before)
    plain = 0
    for i in range(len(words) - 1, -1, -1):
        low = words[i].group(0).lower()
        single = len(low) == 1
        if low in _EMAIL_STOP:
            # "a" and "i" are letters inside a spelled-out run ("m a r g a r e t")
            spelled = single and ((local and len(local[0]) == 1) or (i > 0 and len(words[i - 1].group(0)) == 1))
            if not spelled:
                break
        if single:
            if len(local) >= _MAX_LETTERS:
                break
        else:
            plain += 1
            if plain > _MAX_WORDS:
                break
        local.insert(0, _LOCAL_WORDS.get(low, _DIGIT_WORDS.get(low, low)) if low != "oh" else "0")
        start = words[i].start()
    return local, start


def _spoken_emails(text: str) -> tuple[str, list[str]]:
    """Replace 'm chen at example dot com' with 'mchen@example.com'. Returns text and addresses."""
    text = _unspell_tlds(text)
    found: list[str] = []
    out: list[str] = []
    pos = 0
    for m in _SPOKEN_EMAIL.finditer(text):
        if m.start() < pos:
            continue
        labels = re.split(r"\s+dot\s+|\.", m.group(1).lower())
        if len(labels) < 2 or labels[-1] not in _TLDS:
            continue
        before = text[pos:m.start()]
        local, start = _local_part(before)
        if not local:
            continue
        addr = "".join(local).strip(".") + "@" + ".".join(labels)
        found.append(addr)
        out.append(before[:start] + addr)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), found


_EMAIL_CUE = re.compile(r"(?i)\b(?:e-?)?mail\b")
_EMAIL_FILL = {
    "is", "its", "it's", "address", "to", "me", "use", "the", "at", "it", "send", "my", "please", "also",
    "and", "can", "you", "reach", "contact", "on", "file", "with", "for", "from", "would", "be", "should",
}


def _mask_email_tail(text: str) -> str:
    """Fail closed: after the word email or mail, mask an address-like run that did not parse."""
    spans: list[tuple[int, int]] = []
    for cue in _EMAIL_CUE.finditer(text):
        if spans and cue.start() < spans[-1][1]:
            continue
        base = cue.end()
        toks = list(re.finditer(r"@|[\w.+'-]+", text[base:base + 160]))
        i = 0
        while i < len(toks) and toks[i].group(0).lower() in _EMAIL_FILL:
            i += 1
        first = i
        # tokens must be joined by plain whitespace (a comma or stop ends the run)
        last = first
        while last + 1 < len(toks) and last - first < 14 and not text[
            base + toks[last].end():base + toks[last + 1].start()
        ].strip():
            last += 1
        run = [t.group(0).lower() for t in toks[first:last + 1]]
        if not run:
            continue
        at = next((j for j, w in enumerate(run) if (w in ("at", "@") and j > 0) or "@" in w), None)
        dot = next((j for j, w in enumerate(run) if w == "dot"), None)
        if at is None and dot is None:
            continue
        end = max(j for j in (at, dot) if j is not None)
        end = min(end + 1, len(run) - 1)  # the domain word after "at" or "dot"
        while end + 1 < len(run) and (run[end + 1] == "dot" or len(run[end]) == 1 and len(run[end + 1]) == 1):
            end = min(end + 2, len(run) - 1) if run[end + 1] == "dot" else end + 1
        spans.append((base + toks[first].start(), base + toks[first + end].end()))
    for a, b in reversed(spans):
        text = text[:a] + PLACEHOLDERS["email"] + text[b:]
    return text


# ---- dates -------------------------------------------------------------------------


_UNIT_RE = _alt(_UNITS)
_NUM_WORD = _alt([*_UNITS, *_TEENS, *_TENS])
_SPOKEN_YEAR = (
    rf"(?:(?:nineteen|twenty)\s+(?:(?:oh|zero)\s+{_UNIT_RE}|{_alt(_TENS)}(?:[\s-]+{_UNIT_RE})?|{_alt(_TEENS)})"
    rf"|two\s+thousand(?:\s+and)?(?:\s+{_alt(_TENS)}[\s-]+{_UNIT_RE}|\s+{_alt(_TEENS)}|\s+{_alt(_TENS)}|\s+{_UNIT_RE})?)"
)
_YEAR2W = rf"(?:{_alt(_TENS)}[\s-]+{_UNIT_RE}|(?:oh|zero)\s+{_UNIT_RE})"
# Four digits, a spoken year, a two-digit year ("eighty-five", "85"). Two-digit digits and a lone
# tens word count only after a birth cue ("weak").
_YEAR = rf"(?:\d{{4}}|{_SPOKEN_YEAR}|{_YEAR2W}|\d{{2}}|{_alt(_TENS)})(?![A-Za-z0-9])"
_DAY_WORD = (
    rf"(?:{_alt(_TENS)}[\s-]+(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)"
    rf"|{_alt(_ORD_UNITS)}|{_alt(_TENS)}[\s-]+{_UNIT_RE}|{_alt(_TEENS)}|{_UNIT_RE}|{_alt(_TENS)})"
)
_DAY = rf"(?:\d{{1,2}}(?:st|nd|rd|th)?|{_DAY_WORD})"
_MONTH_SP = rf"(?:(?:oh|zero)\s+{_UNIT_RE}|ten|eleven|twelve|{_UNIT_RE})"
_FILL = r"(?:(?:of|in|the|on)\s+)*"


def _pivot(yy: int) -> int:
    return 2000 + yy if yy < 27 else 1900 + yy


def _spoken_number(words: list[str]) -> int | None:
    """0 to 99 from words like ['eighty', 'five'] or ['fifteen']."""
    if len(words) == 1:
        w = words[0]
        return _UNITS.get(w) or _TEENS.get(w) or _TENS.get(w)
    if len(words) == 2 and words[0] in _TENS and words[1] in _UNITS:
        return _TENS[words[0]] + _UNITS[words[1]]
    return None


def _parse_year(s: str) -> int | None:
    s = s.lower().strip()
    if re.fullmatch(r"\d{4}", s):
        return int(s)
    if re.fullmatch(r"\d{2}", s):
        return _pivot(int(s))
    w = [x for x in re.split(r"[\s-]+", s) if x and x != "and"]
    if w[0] == "two" and len(w) >= 2 and w[1] == "thousand":
        rest = _spoken_number(w[2:]) if len(w) > 2 else 0
        return None if rest is None else 2000 + rest
    if w[0] in ("nineteen", "twenty") and len(w) >= 2 and w[1] not in _UNITS:
        base = 1900 if w[0] == "nineteen" else 2000
        if w[1] in ("oh", "zero") and len(w) == 3:
            return base + _UNITS[w[2]]
        rest = _spoken_number(w[1:])
        return None if rest is None else base + rest
    if w[0] in ("oh", "zero") and len(w) == 2:
        return _pivot(_UNITS[w[1]])
    n = _spoken_number(w)
    return None if n is None else _pivot(n)


def _weak_year(s: str) -> bool:
    s = s.lower().strip()
    return bool(re.fullmatch(r"\d{2}", s)) or s in _TENS


def _parse_day(s: str) -> int | None:
    s = s.lower().strip()
    m = re.match(r"(\d{1,2})", s)
    if m:
        return int(m.group(1))
    w = [x for x in re.split(r"[\s-]+", s) if x]
    if len(w) == 1:
        return _ORD_UNITS.get(w[0]) or _spoken_number(w)
    if len(w) == 2 and w[0] in _TENS:
        return _TENS[w[0]] + (_ORD_UNITS.get(w[1]) or _UNITS.get(w[1], 0))
    return None


def _month_num(s: str) -> int:
    key = s.lower().rstrip(".")[:3]
    return next(i + 1 for i, m in enumerate(_MONTH_NAMES) if m.startswith(key))


def _spoken_month(s: str) -> int | None:
    w = s.lower().split()
    if w[0] in ("oh", "zero") and len(w) == 2:
        return _UNITS[w[1]]
    return _UNITS.get(w[0]) or _TEENS.get(w[0])


def _numeric_ymd(a: int, b: int, year: int) -> str | None:
    month, day = (a, b) if a <= 12 else (b, a)
    return date(year, month, day).isoformat()


_NUM_SEP = r"[\s,/.-]+"
_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),  # 0
    re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b"),  # 1
    re.compile(rf"(?i)\b({_MONTH_RE})[\s,]+(?:the\s+)?({_DAY})(?:\s+of)?[\s,]+{_FILL}({_YEAR})"),  # 2 month day year
    re.compile(rf"(?i)\b(?:the\s+)?({_DAY})\s+(?:of\s+)?({_MONTH_RE})[\s,]+{_FILL}({_YEAR})"),  # 3 day month year
    re.compile(rf"(?i)\b({_MONTH_SP})[\s,]+({_DAY_WORD})[\s,]+{_FILL}({_YEAR})"),  # 4 spoken month day year
    re.compile(rf"(?i)\b({_SPOKEN_YEAR})[\s,]+({_MONTH_SP})[\s,]+({_DAY_WORD})(?![A-Za-z0-9])"),  # 5 year month day
    re.compile(rf"(?i)\b(\d{{4}}|{_SPOKEN_YEAR})[\s,]+(?:(?:on|in|of|the)\s+)*({_MONTH_RE})[\s,]+(?:the\s+)?({_DAY})(?![A-Za-z0-9])"),  # 6 year month-name day
    re.compile(rf"\b(\d{{1,2}}){_NUM_SEP}(\d{{1,2}}){_NUM_SEP}(\d{{4}}|\d{{2}})\b"),  # 7 spaced digits, cue only
]
_BIRTH_CUE = re.compile(
    r"(?i)\b(?:born|birth(?:\s*date|day)?|dob|d\.o\.b|date\s+of\s+birth)(?![A-Za-z])"
)


def _iso(m: re.Match, kind: int, cue: bool) -> str | None | bool:
    """YYYY-MM-DD for one date match, None if it does not read as a real date, False to skip it."""
    try:
        if kind == 0:
            return date(*map(int, m.group(0).split("-"))).isoformat()
        if kind in (1, 7):
            raw = m.group(0)
            parts = re.findall(r"\d+", raw)
            if kind == 7 and not cue:
                return False
            if len(parts[2]) == 2:
                return _numeric_ymd(int(parts[0]), int(parts[1]), _pivot(int(parts[2]))) if cue else None
            if kind == 7:
                return _numeric_ymd(int(parts[0]), int(parts[1]), int(parts[2]))
            out = norm_dob(raw.replace(".", "/"))
            return out if re.fullmatch(r"\d{4}-\d{2}-\d{2}", out) else None
        if kind == 6:
            return date(_parse_year(m.group(1)), _month_num(m.group(2)), _parse_day(m.group(3))).isoformat()
        if kind == 5:
            return date(_parse_year(m.group(1)), _spoken_month(m.group(2)), _parse_day(m.group(3))).isoformat()
        if kind == 4:
            if not cue and (_weak_year(m.group(3)) or not re.match(r"(?i)(oh|zero|ten|eleven|twelve)\b", m.group(1))):
                return False
            month, day, year = _spoken_month(m.group(1)), _parse_day(m.group(2)), m.group(3)
            return date(_parse_year(year), month, day).isoformat()
        month, day, year = (
            (m.group(1), m.group(2), m.group(3)) if kind == 2 else (m.group(2), m.group(1), m.group(3))
        )
        if not cue and _weak_year(year):
            return False
        return date(_parse_year(year), _month_num(month), _parse_day(day)).isoformat()
    except (ValueError, TypeError, StopIteration, KeyError, IndexError):
        return None


def find_dates(text: str) -> list[tuple[int, int, str | None]]:
    """(start, end, iso or None) for every date shape in the text, in order, non-overlapping."""
    found: list[tuple[int, int, int, re.Match]] = []
    for kind, pat in enumerate(_DATE_PATTERNS):
        found += [(m.start(), -m.end(), kind, m) for m in pat.finditer(text)]
    spans: list[tuple[int, int, str | None]] = []
    for start, _, kind, m in sorted(found, key=lambda f: f[:3]):  # earliest, then longest, wins
        if any(start < e and s < m.end() for s, e, _ in spans):
            continue
        cue = bool(_BIRTH_CUE.search(text[max(0, start - 45):start]))
        iso = _iso(m, kind, cue)
        if iso is False:
            continue
        spans.append((start, m.end(), iso))
    return sorted(spans)


def _pick_dob(text: str, spans: list[tuple[int, int, str | None]]) -> int | None:
    """Index of the date span that is the caller's date of birth, if any."""
    for i, (start, _, iso) in enumerate(spans):
        if iso and _BIRTH_CUE.search(text[max(0, start - 40):start]):
            return i
    for i, (_, _, iso) in enumerate(spans):
        if iso and int(iso[:4]) <= 2010:  # an unanchored old date; claim dates are recent
            return i
    return None


_ATOM = re.compile(
    rf"(?i)(?:{_MONTH_RE}|\d{{1,4}}(?:st|nd|rd|th)?|"
    rf"{_alt([*_DIGIT_WORDS, *_TEENS, *_TENS, *_ORD_UNITS, 'hundred', 'thousand', 'dash', 'slash'])})(?![A-Za-z0-9])"
)
_LEAD = re.compile(r"(?i)[\s:,.]*(?:(?:is|was|on|in|the|my|date|of|birth|born|it's|its)\s+)*")
_GLUE = re.compile(r"(?i)(?:[\s,/.-]|\b(?:of|in|on|the|and)\b)+")
_WINDOW = 25


def _mask_birth_window(text: str) -> str:
    """Fail closed: after a birth cue, mask date-like tokens in the next ~25 characters."""
    spans: list[tuple[int, int]] = []
    for cue in _BIRTH_CUE.finditer(text):
        limit = cue.end() + _WINDOW
        pos = _LEAD.match(text, cue.end()).end()
        first = _ATOM.match(text, pos)
        if not first or pos >= limit:
            continue
        end = first.end()
        while True:
            g = _GLUE.match(text, end)
            nxt = _ATOM.match(text, g.end()) if g else None
            if not nxt or nxt.start() >= cue.end() + _WINDOW + 20:
                break
            end = nxt.end()
        spans.append((pos, end))
    for a, b in reversed(spans):
        text = text[:a] + "[date]" + text[b:]
    return text


def _mask_dates(t: str, values: dict[str, str]) -> str:
    """Replace every date shape: the birth date with its placeholder, any other with [date]."""
    spans = find_dates(t)
    pick = _pick_dob(t, spans) if "dob" not in values else None
    pieces: list[str] = []
    pos = 0
    for i, (start, end, iso) in enumerate(spans):
        pieces.append(t[pos:start])
        if i == pick:
            values["dob"] = iso  # type: ignore[assignment]
            pieces.append(PLACEHOLDERS["dob"])
        else:
            pieces.append("[date]")
        pos = end
    pieces.append(t[pos:])
    return "".join(pieces)


# ---- spoken separators and tens-style digits -----------------------------------------------

_DW2 = rf"(?:\b(?:{_alt(_DIGIT_WORDS)}|(?:double|triple)\s+{_alt(_DIGIT_WORDS)})\b|\b\d+\b)"
_SEPRUN = re.compile(rf"(?i){_DW2}(?:[\s,]+(?:\b(?:dash|slash)\b[\s,]+)?{_DW2})+")


def _sep_numbers(text: str) -> str:
    """'one nine eight five dash zero three dash one five' -> '1985-03-15'."""

    def sub(m: re.Match) -> str:
        if not re.search(r"(?i)\b(?:dash|slash)\b", m.group(0)):
            return m.group(0)
        toks = re.findall(r"[A-Za-z]+|\d+", m.group(0))
        out: list[str] = []
        i = 0
        while i < len(toks):
            t = toks[i].lower()
            if t in _REPEAT and i + 1 < len(toks):
                out.append(_DIGIT_WORDS.get(toks[i + 1].lower(), toks[i + 1]) * _REPEAT[t])
                i += 2
                continue
            out.append("-" if t == "dash" else "/" if t == "slash" else _DIGIT_WORDS.get(t, t))
            i += 1
        joined = "".join(out)
        return joined if len(re.sub(r"\D", "", joined)) >= 6 else m.group(0)

    return _SEPRUN.sub(sub, text)


_NUMTOK = rf"(?:(?:double|triple)\s+)?{_alt([*_DIGIT_WORDS, *_TEENS, *_TENS])}"
_CUE_ID = r"(?:ssn|social(?:\s+security)?(?:\s+number)?|national\s+id|id(?:\s+number)?|last\s+(?:four|4)(?:\s+digits)?)"
_CUE_PHONE = r"(?:phone|cell|mobile|number|call(?:\s+me)?|reach\s+me)"
_ANCHORED = re.compile(
    rf"(?i)\b({_CUE_ID}|{_CUE_PHONE})\b([^\d]{{0,30}}?)\b({_NUMTOK}(?:[\s,-]+{_NUMTOK})+)\b"
)


def _pairs_to_digits(run: str) -> str:
    """'forty four seventy two' -> '4472'; 'four four oh two' -> '4402'."""
    toks = [t.lower() for t in re.findall(r"[A-Za-z]+", run)]
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _REPEAT and i + 1 < len(toks):
            out.append(_DIGIT_WORDS.get(toks[i + 1], "") * _REPEAT[t])
            i += 2
        elif t in _TENS:
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if nxt in _UNITS:
                out.append(str(_TENS[t] + _UNITS[nxt]))
                i += 2
            else:
                out.append(str(_TENS[t]))
                i += 1
        elif t in _TEENS:
            out.append(str(_TEENS[t]))
            i += 1
        else:
            out.append(_DIGIT_WORDS.get(t, ""))
            i += 1
    return "".join(out)


def _anchored_digits(text: str) -> str:
    """After an ID or phone cue, read number words as digit pairs ('forty four' is 44)."""

    def sub(m: re.Match) -> str:
        digits = _pairs_to_digits(m.group(3))
        need = 4 if re.match(rf"(?i){_CUE_ID}$", m.group(1)) else 7
        if len(digits) < need:
            return m.group(0)
        if need == 7 and len(digits) == 7:
            digits = digits[:3] + "-" + digits[3:]
        return f"{m.group(1)}{m.group(2)}{digits}"

    return _ANCHORED.sub(sub, text)


# ---- layer 2: per-detail patterns ------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ID = re.compile(
    r"(?i)\b(?:ssn|social(?:\s+security)?(?:\s+number)?|national\s+id|id(?:\s+number)?|last\s+(?:four|4))\b"
    r"[^\d]{0,30}?(?<![A-Za-z]-)(?:\d{3}[-\s]?\d{2}[-\s]?)?(\d{4})(?!\d|[-/]\d)"
)
_PHONE = re.compile(r"(?<![\d-])((?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})(?!\d)")
_PHONE7 = re.compile(r"(?<![\w$-])\d{3}[-.]\d{4}(?![\w-])")  # a local number, area code left out
_PHONE7_CUE = re.compile(r"(?i)\b((?:phone|cell|mobile|number|call|reach)\b\D{0,20}?)\d{3}[-.\s]\d{4}(?!\d)")
_ASKED_ID = re.compile(r"(?i)last (?:four|4)|ssn|social|national id|\bid\b")
_BARE4 = re.compile(r"(?<![\w-])\d{4}(?![\w-])")


def parse_identity(text: str, asked: str | None = None) -> ParseResult:
    """Pull identity details out of a caller message.

    `asked` is the agent's previous message: a bare four-digit reply to a question about
    the ID last four counts as the ID last four.
    """
    values: dict[str, str] = {}
    # dates first: the digit-word collapse would eat part of a spoken date
    t = _mask_dates(_sep_numbers(text), values)
    t = _anchored_digits(t)
    t = digit_words_to_digits(t)
    t = _mask_dates(t, values)

    # email: spoken, then written, then fail closed after the word email
    t, _ = _spoken_emails(t)

    def email_sub(m: re.Match) -> str:
        values.setdefault("email", m.group(0).lower().rstrip(".,;:!?"))
        return PLACEHOLDERS["email"]

    t = _EMAIL.sub(email_sub, t)
    t = _mask_email_tail(t)

    # ID last four, anchored
    def id_sub(m: re.Match) -> str:
        values.setdefault("id_last4", m.group(1))
        return _swap_tail(m)

    t = _ID.sub(id_sub, t)

    def phone_sub(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(1))
        values.setdefault("phone", digits[-10:])
        return PLACEHOLDERS["phone"]

    t = _PHONE.sub(phone_sub, t)
    t = _PHONE7.sub(PLACEHOLDERS["phone"], t)
    t = _PHONE7_CUE.sub(lambda m: m.group(1) + PLACEHOLDERS["phone"], t)

    if "id_last4" not in values and asked and _ASKED_ID.search(asked):
        bare = _BARE4.findall(t)
        if len(bare) == 1:
            values["id_last4"] = bare[0]
            t = _BARE4.sub(PLACEHOLDERS["id_last4"], t, count=1)

    t = _presidio_mask(t)
    return ParseResult(sweep(t), values)


def _swap_tail(m: re.Match) -> str:
    """The anchored match with its digits replaced by the ID placeholder."""
    whole = m.group(0)
    cut = m.start(1) - m.start(0)
    head = whole[:cut]
    # drop an optional SSN prefix (three and two digits) that sits just before the last four
    head = re.sub(r"\d{3}[-\s]?\d{2}[-\s]?$", "", head)
    return head + PLACEHOLDERS["id_last4"]


# ---- layer 3: Presidio (optional) and the fail-closed sweep ---------------------------------


@lru_cache(maxsize=1)
def _presidio_engine():
    if os.environ.get("PII_PRESIDIO", "1") == "0":
        return None
    try:
        from presidio_analyzer import AnalyzerEngine

        return AnalyzerEngine()
    except Exception:  # not installed, or no language model: the sweep below still runs
        return None


_PRESIDIO_LABELS = {
    "PHONE_NUMBER": "[number]", "EMAIL_ADDRESS": "[email]", "US_SSN": "[number]",
    "CREDIT_CARD": "[number]", "IBAN_CODE": "[number]",
}


def _presidio_spans(text: str) -> list[tuple[int, int, str]]:
    engine = _presidio_engine()
    if engine is None:
        return []
    try:
        found = engine.analyze(text=text, language="en", entities=list(_PRESIDIO_LABELS))
    except Exception:
        return []
    return [(r.start, r.end, _PRESIDIO_LABELS[r.entity_type]) for r in found]


def _presidio_mask(text: str) -> str:
    spans = sorted(_presidio_spans(text), reverse=True)
    last = len(text) + 1
    for start, end, label in spans:
        if end > last:
            continue  # overlaps a span already masked
        text = text[:start] + label + text[end:]
        last = start
    return text


_SWEEP_DIGITS = re.compile(r"(?<![A-Za-z0-9$])(?<![A-Za-z]-)\d{4,}(?![A-Za-z0-9])")
_AFTER_MONTH = re.compile(rf"(?i)\b{_MONTH_RE}(?:\s+of)?,?\s+$")


def _sweep_digits(m: re.Match) -> str:
    """Keep a bare year right after a month name ('January 2025'): the claim month needs it."""
    if len(m.group(0)) == 4 and _AFTER_MONTH.search(m.string[max(0, m.start() - 16):m.start()]):
        return m.group(0)
    return "[number]"


def sweep(text: str) -> str:
    """Fail-closed pass: mask leftover emails, date shapes and runs of 4 or more digits."""
    text = _EMAIL.sub("[email]", text)
    for start, end, _ in reversed(find_dates(text)):
        text = text[:start] + "[date]" + text[end:]
    text = _mask_birth_window(text)
    return _SWEEP_DIGITS.sub(_sweep_digits, text)


# ---- egress check -------------------------------------------------------------------------

_SEP_DIGITS = r"[\s().+-]{0,2}"


def _digit_pattern(digits: str) -> re.Pattern:
    body = _SEP_DIGITS.join(digits)
    country = r"(?:\+?1[\s.-]?)?" if len(digits) == 10 else ""
    return re.compile(rf"(?<!\d)(?<![A-Za-z]-){country}{body}(?!\d)")


def _value_regexes(value: str) -> list[re.Pattern]:
    v = value.strip()
    if not v:
        return []
    if "@" in v:
        return [re.compile(re.escape(v), re.IGNORECASE)]
    digits = re.sub(r"\D", "", v)
    if digits and not re.search(r"[A-Za-z]", v):
        pats = [_digit_pattern(digits)]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            pats.append(re.compile(re.escape(v)))
        return pats
    return [re.compile(re.escape(v), re.IGNORECASE)]


def _is_iso_date(v: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()))


def _spans_for(text: str, value: str) -> list[tuple[int, int]]:
    spans = [m.span() for p in _value_regexes(value) for m in p.finditer(text)]
    if _is_iso_date(value):  # also catch "March 15, 1985" and "15/03/1985" spellings
        spans += [(s, e) for s, e, iso in find_dates(text) if iso == value.strip()]
    return spans


def egress_hits(text: str, values: Iterable[str]) -> list[str]:
    """Which of the values appear in the text (exact match, digit separators ignored)."""
    if not text:
        return []
    return [v for v in dict.fromkeys(values) if v and _spans_for(text, v)]


def scrub(text: str, values: Iterable[str], label: str = "[removed]") -> str:
    """Replace every occurrence of the values in the text. The fail-closed repair."""
    spans: list[tuple[int, int]] = []
    for v in dict.fromkeys(values):
        if v:
            spans += _spans_for(text, v)
    merged: list[list[int]] = []
    for start, end in sorted(set(spans)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + label + text[end:]
    return text
