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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check vMem production readiness")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    results = run_checks()
    ok = all(item["ok"] for item in results)
    if args.json:
        print(json.dumps({"ok": ok, "checks": results}, ensure_ascii=False, indent=2))
    else:
        for item in results:
            icon = "✅" if item["ok"] else "❌"
            print(f"{icon} {item['name']}: {item['message']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
