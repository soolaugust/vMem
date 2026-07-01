#!/usr/bin/env python3
"""vMem production readiness doctor."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
HOOKS_JSON = ROOT / "hooks" / "hooks.json"
MEMORY_OS_DIR = Path(os.environ.get("MEMORY_OS_DIR", Path.home() / ".claude" / "memory-os"))
REQUIRED_FILES = [
    ROOT / "hooks" / "prompt_budget_guard.py",
    ROOT / "hooks" / "context_governor.py",
    ROOT / "hooks" / "retriever_wrapper.sh",
    ROOT / "hooks" / "retriever_daemon.py",
    ROOT / "hooks" / "retriever.py",
]
INTERNAL_PATTERNS = ("xiao" + "mi", "git.n." + "xiao" + "mi", "@" + "xiao" + "mi", "kernel-cpu/" + "aios")


def _load_hooks() -> dict[str, Any]:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def _hook_commands(config: dict[str, Any], event: str) -> list[str]:
    entries = config.get("hooks", {}).get(event, [])
    return [
        str(hook.get("command", ""))
        for entry in entries
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    ]


def check_hooks() -> tuple[bool, str]:
    config = _load_hooks()
    entries = config.get("hooks", {}).get("UserPromptSubmit", [])
    commands = _hook_commands(config, "UserPromptSubmit")
    guard = next((i for i, command in enumerate(commands) if "prompt_budget_guard.py" in command), None)
    retriever = next((i for i, command in enumerate(commands) if "retriever_wrapper.sh" in command), None)
    if guard is None:
        return False, "prompt_budget_guard.py is not wired into UserPromptSubmit"
    if retriever is None:
        return False, "retriever_wrapper.sh is not wired into UserPromptSubmit"
    first_hook = entries[0].get("hooks", [{}])[0] if entries else {}
    if guard != 0 or first_hook.get("async") is not False:
        return False, "prompt_budget_guard.py must be the first synchronous UserPromptSubmit hook"
    if guard > retriever:
        return False, "prompt_budget_guard.py must run before retriever_wrapper.sh"
    return True, "hook order ok: prompt_budget_guard before retriever"


def check_required_files() -> tuple[bool, str]:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_FILES if not path.exists()]
    if missing:
        return False, "missing files: " + ", ".join(missing)
    return True, "required hook files present"


def check_retriever_pressure() -> tuple[bool, str]:
    missing = []
    for rel in ("hooks/retriever.py", "hooks/retriever_daemon.py"):
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        if "should_shed_optional_context" not in text:
            missing.append(rel)
    if missing:
        return False, "retriever pressure shedding missing: " + ", ".join(missing)
    return True, "retriever fallback and daemon consume pressure state"


def check_memory_dir() -> tuple[bool, str]:
    try:
        MEMORY_OS_DIR.mkdir(parents=True, exist_ok=True)
        probe = MEMORY_OS_DIR / ".doctor_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        return False, f"memory dir not writable: {MEMORY_OS_DIR} ({exc})"
    return True, f"memory dir writable: {MEMORY_OS_DIR}"


def check_internal_strings() -> tuple[bool, str]:
    roots = [ROOT / "README.md", ROOT / "README.zh.md", ROOT / "llms.txt", ROOT / "docs", ROOT / "marketing", ROOT / "paper" / "main.tex"]
    hits: list[str] = []
    for root in roots:
        paths = root.rglob("*") if root.is_dir() else [root]
        for path in paths:
            if not path.is_file() or path.suffix.lower() in {".pdf", ".deb", ".aux", ".out", ".blg"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if any(pattern in text for pattern in INTERNAL_PATTERNS):
                hits.append(str(path.relative_to(ROOT)))
                if len(hits) >= 10:
                    break
    if hits:
        return False, "internal strings found: " + ", ".join(hits)
    return True, "no public internal-string hits"


def default_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_SETTINGS_PATH", Path.home() / ".claude" / "settings.json")).expanduser()


def _plugin_root_placeholder() -> str:
    return "${CLAUDE_PLUGIN_ROOT}"


def desired_user_prompt_entry() -> dict[str, Any]:
    root = _plugin_root_placeholder()
    return {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": f"python3 \"{root}/hooks/prompt_budget_guard.py\"",
                "timeout": 3,
                "async": False,
            }
        ],
    }


def _is_prompt_guard_command(command: Any) -> bool:
    return isinstance(command, str) and "prompt_budget_guard.py" in command


def repair_settings(settings_path: Path, write: bool = True) -> dict[str, Any]:
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("settings root must be an object")
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.hooks must be an object")
    entries = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(entries, list):
        raise ValueError("settings.hooks.UserPromptSubmit must be a list")

    desired = desired_user_prompt_entry()
    changed = False
    kept: list[Any] = []
    removed_duplicates = 0
    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        entry_hooks = entry.get("hooks", [])
        has_guard = isinstance(entry_hooks, list) and any(
            isinstance(hook, dict) and _is_prompt_guard_command(hook.get("command"))
            for hook in entry_hooks
        )
        if not has_guard:
            kept.append(entry)
            continue
        if found:
            removed_duplicates += 1
            changed = True
            continue
        found = True
        if entry != desired:
            changed = True
        kept.append(desired)

    if not found:
        kept.insert(0, desired)
        changed = True
    else:
        guard_index = next(
            index for index, entry in enumerate(kept)
            if isinstance(entry, dict) and any(
                isinstance(hook, dict) and _is_prompt_guard_command(hook.get("command"))
                for hook in entry.get("hooks", [])
            )
        )
        if guard_index != 0:
            guard_entry = kept.pop(guard_index)
            kept.insert(0, guard_entry)
            changed = True

    hooks["UserPromptSubmit"] = kept
    if write and changed:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "changed": changed,
        "settings_path": str(settings_path),
        "guard_index": 0,
        "duplicates_removed": removed_duplicates,
        "command": desired["hooks"][0]["command"],
    }


def run_checks() -> list[dict[str, Any]]:
    checks = [
        ("required_files", check_required_files),
        ("hooks", check_hooks),
        ("retriever_pressure", check_retriever_pressure),
        ("memory_dir", check_memory_dir),
        ("public_hygiene", check_internal_strings),
    ]
    results = []
    for name, func in checks:
        try:
            ok, message = func()
        except Exception as exc:
            ok, message = False, f"{type(exc).__name__}: {exc}"
        results.append({"name": name, "ok": ok, "message": message})
    return results


def _emit_result(payload: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if "checks" in payload:
            for item in payload["checks"]:
                icon = "✅" if item["ok"] else "❌"
                print(f"{icon} {item['name']}: {item['message']}")
        else:
            icon = "✅" if payload.get("ok") else "❌"
            action = payload.get("action", "repair")
            changed = "changed" if payload.get("changed") else "already ok"
            print(f"{icon} {action}: {changed} ({payload.get('settings_path')})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check and repair vMem production readiness")
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser("doctor", help="run readiness checks")
    doctor.add_argument("--json", action="store_true", help="emit JSON")
    for name in ("install", "repair"):
        cmd = sub.add_parser(name, help=f"{name} Claude Code hook settings")
        cmd.add_argument("--settings", type=Path, default=default_settings_path())
        cmd.add_argument("--json", action="store_true", help="emit JSON")
        cmd.add_argument("--check", action="store_true", help="dry-run without writing")
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command in {"install", "repair"}:
        payload = repair_settings(args.settings.expanduser(), write=not args.check)
        payload["action"] = args.command
        _emit_result(payload, args.json)
        return 0 if payload["ok"] else 1

    results = run_checks()
    ok = all(item["ok"] for item in results)
    payload = {"ok": ok, "checks": results}
    _emit_result(payload, bool(getattr(args, "json", False)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
