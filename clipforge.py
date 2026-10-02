#!/usr/bin/env python3
"""ClipForge v1.2 bootstrap — extracts the full app on first run."""
import base64, zlib, sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARTS_DIR = ROOT / ".clipforge_data"

def extract():
    parts = []
    i = 0
    while True:
        p = PARTS_DIR / f"part{i}.b64"
        if not p.exists():
            break
        parts.append(p.read_text(encoding="utf-8"))
        i += 1
    if not parts:
        raise SystemExit("Missing .clipforge_data parts — re-clone or re-download.")
    code = zlib.decompress(base64.b64decode("".join(parts)))
    target = Path(__file__).resolve()
    target.write_bytes(code)
    print("ClipForge v1.2 installed. Restarting…", file=sys.stderr)
    os.execv(sys.executable, [sys.executable, str(target)] + sys.argv[1:])

if __name__ == "__main__":
    extract()
