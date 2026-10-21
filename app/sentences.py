"""Cut streamed text into sentences.

A sentence ends at . ! or ? (plus closing quotes or brackets) followed by whitespace, or at a
line break. A terminator at the very end of the buffer waits for the next delta, so "$1,450.00"
and "3.5" are never split. Splitting too early is harmless (a shorter chunk); splitting inside
a number would be the only real mistake, and a number is never followed by whitespace mid-token.
"""

from __future__ import annotations

import re

_END = re.compile(r"""(?:[.!?]+["')\]]*\s+)|(?:\n\s*)""")


class SentenceSplitter:
    def __init__(self) -> None:
        self.buf = ""

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        start = 0
        for m in _END.finditer(self.buf):
            piece = self.buf[start : m.end()].strip()
            start = m.end()
            if piece:
                out.append(piece)
        self.buf = self.buf[start:]
        return out

    def flush(self) -> str | None:
        rest, self.buf = self.buf.strip(), ""
        return rest or None


def split_sentences(text: str) -> list[str]:
    sp = SentenceSplitter()
    out = sp.feed(text)
    tail = sp.flush()
    return [*out, tail] if tail else out
