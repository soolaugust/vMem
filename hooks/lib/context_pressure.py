from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parents[1]
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

try:
    from context_governor import enforce_additional_context, prompt_text, should_shed_optional_context
except Exception:
    def prompt_text(data: dict[str, Any]) -> str:
        return data.get("prompt", "") if isinstance(data, dict) else ""

    def should_shed_optional_context(data: dict[str, Any] | None = None) -> bool:
        return False

    def enforce_additional_context(
        data: dict[str, Any] | None,
        text: str,
        *,
        producer: str,
        hook_event_name: str,
        max_chars: int | None = None,
        mandatory: bool = False,
    ) -> dict[str, Any] | None:
        if not text:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": hook_event_name,
                "additionalContext": text[:max_chars] if max_chars else text,
            }
        }
