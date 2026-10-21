"""Download the Kokoro model and voices into models/ (standard library only).

Files come from the kokoro-onnx model-files-v1.1 release. Nothing downloaded is run.
Use --full for the full-precision model instead of the int8 one.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
MODELS = Path(__file__).resolve().parent.parent / "models"
# Approximate sizes in MB, for the progress message; names match the release assets.
FILES = {
    "kokoro-v1.0.int8.onnx": 114.1,
    "kokoro-v1.0.onnx": 325.5,
    "voices-v1.0.bin": 28.2,
}


def fetch(name: str, approx_mb: float) -> None:
    dest = MODELS / name
    marker = MODELS / (name + ".size")
    if dest.exists() and marker.exists() and dest.stat().st_size == int(marker.read_text()):
        print(f"{name}: already present, skipped")
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(BASE + name, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r{name}: {done / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        if total and done != total:
            tmp.unlink(missing_ok=True)
            sys.exit(f"{name}: download incomplete ({done} of {total} bytes)")
    tmp.replace(dest)
    marker.write_text(str(dest.stat().st_size))
    print(f"\n{name}: saved ({dest.stat().st_size / 1e6:.1f} MB, expected about {approx_mb} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true", help="fetch kokoro-v1.0.onnx instead of the int8 model")
    args = ap.parse_args()
    MODELS.mkdir(exist_ok=True)
    model = "kokoro-v1.0.onnx" if args.full else "kokoro-v1.0.int8.onnx"
    for name in (model, "voices-v1.0.bin"):
        fetch(name, FILES[name])


if __name__ == "__main__":
    main()
