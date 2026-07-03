#!/usr/bin/env python3
"""prompt_budget_settings_reconcile.py regression tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from prompt_budget_settings_reconcile import REQUIRED_COMMAND, reconcile_settings


def write_settings(path: Path, user_prompt_submit: list[object]) -> None:
    path.write_text(
        json.dumps({"hooks": {"UserPromptSubmit": user_prompt_submit}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Path(tmp) / "settings.json"

        write_settings(settings, [])
        result = reconcile_settings(settings, write=False)
        assert result["changed"] is True
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["hooks"]["UserPromptSubmit"] == []

        result = reconcile_settings(settings, write=True)
        assert result["changed"] is True
        data = json.loads(settings.read_text(encoding="utf-8"))
        first_hook = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        assert first_hook["command"] == REQUIRED_COMMAND
        assert first_hook["async"] is False
        assert first_hook["timeout"] == 3

        result = reconcile_settings(settings, write=False)
        assert result["changed"] is False
        assert result["guard_index"] == 0

        write_settings(settings, [
            {"matcher": "*", "hooks": [{"type": "command", "command": "python3 /tmp/other.py", "timeout": 3}]},
            {"hooks": [{"type": "command", "command": "python3 /old/prompt_budget_guard.py", "timeout": 30, "async": True}]},
            {"hooks": [{"type": "command", "command": "python3 /dup/prompt_budget_guard.py", "timeout": 3, "async": False}]},
        ])
        result = reconcile_settings(settings, write=True)
        assert result["changed"] is True
        assert result["duplicate_indexes_removed"] == [2]
        data = json.loads(settings.read_text(encoding="utf-8"))
        entries = data["hooks"]["UserPromptSubmit"]
        assert entries[0]["matcher"] == "*"
        assert entries[0]["hooks"][0]["command"] == REQUIRED_COMMAND
        assert entries[0]["hooks"][0]["async"] is False
        assert sum(
            1
            for entry in entries
            for hook in entry.get("hooks", [])
            if "prompt_budget_guard.py" in hook.get("command", "")
        ) == 1

    print("✅ prompt_budget_settings_reconcile tests passed")


if __name__ == "__main__":
    main()
