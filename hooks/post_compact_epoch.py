#!/usr/bin/env python3
"""PostCompact hook: mark a new compact epoch for context guards.

Claude Code keeps the historical transcript on disk after /compact, so guards must
reset pressure counters on the compact boundary instead of judging by transcript
size or lifetime cache counters.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
ROOT = THIS_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.thrashing_detector import reset_compact_epoch


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    session_id = data.get("session_id", "")
    reset_compact_epoch(session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
