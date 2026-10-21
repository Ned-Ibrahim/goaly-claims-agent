"""Caller emotion readings across a call (decisions 11a, 11b, 11c)."""

from __future__ import annotations

from .models import Extraction, Session

_RANK = {"none": 0, "mild": 1, "strong": 2}


def record(s: Session, ex: Extraction) -> None:
    s.emotion_log.append({"turn": s.turn, "readings": ex.emotions.model_dump()})


def _level(readings: dict, name: str) -> int:
    return _RANK.get((readings.get(name) or {}).get("level", "none"), 0)


def frustration_shown(s: Session) -> bool:
    """Frustration or anger strong once, or at least mild in two or more caller messages."""
    mild = 0
    for entry in s.emotion_log:
        r = entry.get("readings", {})
        top = max(_level(r, "frustration"), _level(r, "anger"))
        if top >= 2:
            return True
        if top >= 1:
            mild += 1
    return mild >= 2


def resisting(ex: Extraction) -> bool:
    """Refusal or frustration at least mild in this message."""
    return ex.emotions.refusal.level != "none" or ex.emotions.frustration.level != "none"


def tone_lines(ex: Extraction) -> list[str]:
    """Reply instructions, one per emotion present in this message."""
    e = ex.emotions
    lines = []
    if e.frustration.level != "none" or e.anger.level != "none":
        lines.append("The caller is upset: acknowledge it in one sincere sentence and do not argue.")
    if e.anxiety.level != "none":
        lines.append("The caller is anxious: reassure briefly and give the concrete next step.")
    if e.confusion.level != "none":
        lines.append("The caller is confused: explain simply, one step at a time.")
    return lines
