#!/usr/bin/env python3
"""Keep prompt_budget_guard wired as a synchronous warn-only UserPromptSubmit hook."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[3]
GUARD_SCRIPT = WORKSPACE / "aios" / "memory-os" / "hooks" / "prompt_budget_guard.py"
REQUIRED_COMMAND = f"python3 {GUARD_SCRIPT}"
REQUIRED_ENTRY: dict[str, Any] = {
    "matcher": "*",
    "hooks": [
        {
            "type": "command",
            "command": REQUIRED_COMMAND,
            "timeout": 3,
            "async": False,
        }
    ],
}


def default_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_SETTINGS_PATH", "~/.claude/settings.json")).expanduser()


def _command_matches(command: Any) -> bool:
    return isinstance(command, str) and "prompt_budget_guard.py" in command


def _normalise_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return entry, False

    changed = False
    new_entry = dict(entry)
    if new_entry.get("matcher") != "*":
        new_entry["matcher"] = "*"
        changed = True

    new_hooks: list[Any] = []
    for hook in hooks:
        if not isinstance(hook, dict) or not _command_matches(hook.get("command")):
            new_hooks.append(hook)
            continue
        new_hook = dict(hook)
        expected = REQUIRED_ENTRY["hooks"][0]
        for key, expected_value in expected.items():
            if new_hook.get(key) != expected_value:
                new_hook[key] = expected_value
                changed = True
        new_hooks.append(new_hook)
    new_entry["hooks"] = new_hooks
    return new_entry, changed


def reconcile_settings(settings_path: Path, write: bool = False) -> dict[str, Any]:
    raw = settings_path.read_text(encoding="utf-8")
    settings = json.loads(raw)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.hooks must be an object")

    entries = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(entries, list):
        raise ValueError("settings.hooks.UserPromptSubmit must be a list")

    found_index: int | None = None
    changed = False
    normalised_entries: list[Any] = []
    duplicate_indexes: list[int] = []

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            normalised_entries.append(entry)
            continue
        entry_has_guard = any(
            isinstance(hook, dict) and _command_matches(hook.get("command"))
            for hook in entry.get("hooks", [])
            if isinstance(entry.get("hooks"), list)
        )
        if not entry_has_guard:
            normalised_entries.append(entry)
            continue
        if found_index is not None:
            duplicate_indexes.append(index)
            changed = True
            continue
        found_index = len(normalised_entries)
        normalised, entry_changed = _normalise_entry(entry)
        changed = changed or entry_changed
        normalised_entries.append(normalised)

    if found_index is None:
        normalised_entries.insert(0, REQUIRED_ENTRY)
        found_index = 0
        changed = True
    elif found_index != 0:
        guard_entry = normalised_entries.pop(found_index)
        normalised_entries.insert(0, guard_entry)
        found_index = 0
        changed = True

    hooks["UserPromptSubmit"] = normalised_entries

    if write and changed:
        settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "changed": changed,
        "settings_path": str(settings_path),
        "guard_index": found_index,
        "duplicate_indexes_removed": duplicate_indexes,
        "command": REQUIRED_COMMAND,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, default=default_settings_path())
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    try:
        result = reconcile_settings(args.settings.expanduser(), write=args.write)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
