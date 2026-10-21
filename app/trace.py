"""Masked per-call trace: one JSON line per controller event (decision 9d)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .models import Session


def _scrub(value: Any, s: Session) -> Any:
    """Remove identity values from any string inside an event."""
    if isinstance(value, str):
        from .memory import _REDACTIONS

        for v in s.identity.values():
            v = (v or "").strip()
            if len(v) >= 4:
                value = re.sub(re.escape(v), "[identity]", value, flags=re.IGNORECASE)
        for pattern, repl in _REDACTIONS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, s) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, s) for v in value]
    return value


def write(s: Session, event: dict[str, Any]) -> None:
    """Append the event to <CLAIMS_TRACE_DIR or traces>/<session_id>.jsonl. Never raises."""
    try:
        folder = Path(os.environ.get("CLAIMS_TRACE_DIR") or "traces")
        folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]", "_", s.session_id)
        line = {"turn": s.turn, "phase": s.phase.value, **_scrub(event, s)}
        with open(folder / f"{name}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass
