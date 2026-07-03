from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parents[1]
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

try:
    from context_governor import prompt_text, should_shed_optional_context
except Exception:
    def prompt_text(data: dict[str, Any]) -> str:
        return data.get("prompt", "") if isinstance(data, dict) else ""

    def should_shed_optional_context(data: dict[str, Any] | None = None) -> bool:
        return False
