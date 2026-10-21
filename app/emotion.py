"""Caller emotion readings across a call (decisions 11a, 11b). Stub: agent B fills this in."""

from __future__ import annotations

from .models import Extraction, Session


def record(s: Session, ex: Extraction) -> None:
    s.emotion_log.append({"turn": s.turn, "readings": ex.emotions.model_dump()})


def frustration_shown(s: Session) -> bool:
    return False


def resisting(ex: Extraction) -> bool:
    return False


def tone_lines(ex: Extraction) -> list[str]:
    return []
