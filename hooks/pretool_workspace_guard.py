#!/usr/bin/env python3
"""Coalesced PreToolUse workspace guard.

Keeps deterministic guardrails in one hook entry so settings hook count stays within
coalescing budget while preserving the existing sub-guards:
- git hook bypass blocker for Bash
- filesize guard for Read
- loop breaker for Bash/Read/Grep/Edit/Write
- session health check for all tools
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path("/home/mi/ssd/codes/claude-workspace")


def _tool_name(data: dict) -> str:
    return str(data.get("tool_name") or data.get("name") or "")


def _tool_input(data: dict) -> dict:
    value = data.get("tool_input") or data.get("input") or {}
    return value if isinstance(value, dict) else {}


def _block(reason: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    raise SystemExit(2)


def _check_git_bypass(tool_name: str, tool_input: dict) -> None:
    if tool_name != "Bash":
        return
    cmd = str(tool_input.get("command") or "")
    if not re.search(r"\bgit\b", cmd):
        return
    blocked = (
        re.search(r"--no-verify\b", cmd)
        or (re.search(r"\bgit\s+commit\b", cmd) and re.search(r"\s-n(\s|$|[a-zA-Z])", cmd))
        or re.search(r"-c\s+[\"']?core\.hooksPath\s*=", cmd)
    )
    if blocked:
        _block("BLOCKED: --no-verify / hooksPath override is not allowed. Git hooks must not be bypassed.")


def _run_subguard(command: list[str], raw: str, timeout: int) -> None:
    try:
        result = subprocess.run(command, input=raw, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        print(f"[pretool_workspace_guard] subguard unavailable: {command[0]}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.returncode == 2:
        raise SystemExit(2)


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}

    tool_name = _tool_name(data)
    tool_input = _tool_input(data)

    _check_git_bypass(tool_name, tool_input)

    if tool_name == "Read":
        _run_subguard(["node", str(WORKSPACE / "aios/memory-os/hooks/filesize_guard.js")], raw, 5)

    if tool_name in {"Bash", "Read", "Grep", "Edit", "Write"}:
        _run_subguard(["python3", str(WORKSPACE / "aios/memory-os/hooks/loop_breaker.py")], raw, 3)

    _run_subguard(["python3", "/home/mi/.claude/hooks/session-health-check.py"], raw, 5)


if __name__ == "__main__":
    main()
