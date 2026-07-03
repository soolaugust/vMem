#!/usr/bin/env python3
"""Context Kernel PreToolUse syscall adapter."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_os.runtime.context.kernel_compat import AdmissionResult, admit_tool  # noqa: E402


def _emit(payload: dict[str, Any], code: int = 0) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(code)


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def _tool_name(data: dict[str, Any]) -> str:
    return str(data.get("tool_name") or data.get("toolName") or data.get("name") or "")


def _tool_input(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("tool_input") or data.get("toolInput") or data.get("input") or {}
    return value if isinstance(value, dict) else {}


def _emit_result(result: AdmissionResult) -> None:
    if result.decision == "block":
        _emit({"decision": "block", "reason": result.reason}, 2)
    if result.decision == "update" and result.updated_input is not None:
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": result.reason,
                "updatedInput": result.updated_input,
            }
        })


def main() -> None:
    data = _read_input()
    result = admit_tool(_tool_name(data), _tool_input(data))
    _emit_result(result)


if __name__ == "__main__":
    main()
