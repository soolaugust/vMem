from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HOOKS_DIR = Path(__file__).resolve().parents[1]
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


def read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"prompt": raw}
    return data if isinstance(data, dict) else {}


def emit_user_prompt_context(text: str) -> None:
    from context_governor import enforce_additional_context

    output = enforce_additional_context(
        None,
        text,
        producer="prompt_io",
        hook_event_name="UserPromptSubmit",
    )
    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
