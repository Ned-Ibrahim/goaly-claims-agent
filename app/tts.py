"""Server-side speech output with Kokoro (decision 4b revised).

Loaded lazily on first use. The reply text stays on this machine.
CLAIMS_TTS=browser forces the browser voice.
"""

from __future__ import annotations

import io
import os
import threading
import wave
from pathlib import Path

ROOT = Path(__file__).parent.parent
_lock = threading.Lock()
_kokoro = None


def _paths() -> tuple[Path, Path]:
    model = os.environ.get("CLAIMS_TTS_MODEL", "").strip() or "models/kokoro-v1.0.int8.onnx"
    voices = os.environ.get("CLAIMS_TTS_VOICES", "").strip() or "models/voices-v1.0.bin"
    return ROOT / model, ROOT / voices  # an absolute path wins over ROOT


def available() -> bool:
    """True when Kokoro can speak: not forced to the browser, package importable, files present."""
    if os.environ.get("CLAIMS_TTS", "").strip().lower() == "browser":
        return False
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        return False
    return all(p.is_file() for p in _paths())


def _load():
    """Build the Kokoro session once. The caller holds _lock."""
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro

        model, voices = _paths()
        _kokoro = Kokoro(str(model), str(voices))
    return _kokoro


def synthesize(text: str) -> bytes:
    """Return the text spoken as a 16-bit PCM mono WAV."""
    import numpy as np

    voice = os.environ.get("CLAIMS_TTS_VOICE", "").strip() or "af_heart"
    with _lock:  # one onnx session; run one synthesis at a time
        kokoro = _load()
        samples, rate = kokoro.create(text, voice=voice, speed=1.0, lang="en-us")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
