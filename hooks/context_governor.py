#!/usr/bin/env python3
"""Shared context memory governance for memory-os hooks.

This module is the single place for context pressure semantics:
- rescue commands are escape hatches and must not be blocked by context pressure;
- high/critical pressure should shed optional additionalContext injection;
- prompt-size admission remains separate from request-context reclaim.

OS analogy: watermarks + shrinkers + PF_MEMALLOC-style rescue paths.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MEMORY_OS_DIR = Path(os.environ.get("MEMORY_OS_DIR", Path.home() / ".claude" / "memory-os"))
CONTEXT_PRESSURE_STATE_FILE = MEMORY_OS_DIR / "context_pressure_state.json"
DEFAULT_PRESSURE_MAX_AGE_SECS = 600
RESCUE_COMMANDS = frozenset({"/clear", "/compact"})
PRESSURE_LEVELS = frozenset({"high", "critical"})

CONTEXT_MODE_STATE_FILE = MEMORY_OS_DIR / "context_mode_state.json"
WORKING_SET_FILE = MEMORY_OS_DIR / "working_set" / "current.json"
DEFAULT_WORKING_SET_MAX_AGE_SECS = 1800


@dataclass(frozen=True)
class ContextModeState:
    mode: str
    last_seen_at: str
    age_secs: float
    active: bool
    reason: str = ""

@dataclass(frozen=True)
class PressureState:
    level: str
    last_seen_at: str
    age_secs: float
    active: bool


def prompt_text(data: dict[str, Any]) -> str:
    """Extract user prompt from known Claude Code hook payload shapes."""
    hook_specific = data.get("hookSpecificInput")
    if isinstance(hook_specific, dict):
        value = hook_specific.get("userMessage")
        if isinstance(value, str):
            return value
    for key in ("prompt", "user_prompt", "message"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return ""


def command_name(prompt: str) -> str:
    stripped = prompt.strip()
    if not stripped:
        return ""

    local_command = re.search(r"<command-name>\s*(/[^<\s]+)\s*</command-name>", stripped)
    if local_command:
        return local_command.group(1)

    return stripped.split(maxsplit=1)[0]


def is_local_command(prompt: str) -> bool:
    return command_name(prompt).startswith("/")


def is_rescue_command(prompt: str) -> bool:
    return command_name(prompt) in RESCUE_COMMANDS


def _parse_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def write_pressure_state(
    level: str,
    reason: str,
    state_file: Path | None = None,
) -> None:
    """Persist context pressure so optional context producers can shed load."""
    normalized_level = level.lower().strip()
    if normalized_level not in PRESSURE_LEVELS and normalized_level != "low":
        normalized_level = "low"
    path = state_file or CONTEXT_PRESSURE_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_pressure_level": normalized_level,
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def read_pressure_state(
    state_file: Path | None = None,
    max_age_secs: int | None = None,
) -> PressureState:
    path = state_file or CONTEXT_PRESSURE_STATE_FILE
    max_age = max_age_secs or int(os.environ.get(
        "MEMORY_OS_PRESSURE_SHED_MAX_AGE_SECS",
        str(DEFAULT_PRESSURE_MAX_AGE_SECS),
    ))
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return PressureState(level="", last_seen_at="", age_secs=float("inf"), active=False)

    if not isinstance(state, dict):
        return PressureState(level="", last_seen_at="", age_secs=float("inf"), active=False)

    level = str(state.get("last_pressure_level", "")).lower()
    last_seen_at = str(state.get("last_seen_at", ""))
    seen_ts = _parse_timestamp(last_seen_at)
    if seen_ts is None:
        return PressureState(level=level, last_seen_at=last_seen_at, age_secs=float("inf"), active=False)

    age = max(0.0, time.time() - seen_ts)
    return PressureState(
        level=level,
        last_seen_at=last_seen_at,
        age_secs=age,
        active=level in PRESSURE_LEVELS and age <= max_age,
    )


def should_shed_optional_context(
    data: dict[str, Any] | None = None,
    state_file: Path | None = None,
    max_age_secs: int | None = None,
) -> bool:
    """Return True when optional additionalContext producers should stay silent."""
    if data is not None and is_rescue_command(prompt_text(data)):
        return False
    if read_context_mode(max_age_secs=max_age_secs).active:
        return True
    return read_pressure_state(state_file=state_file, max_age_secs=max_age_secs).active

def write_context_mode(
    mode: str,
    reason: str,
    state_file: Path | None = None,
) -> None:
    """Persist context-kernel mode so all context producers share one state."""
    normalized = mode.lower().strip()
    if normalized not in {"normal", "pressure", "working_set"}:
        normalized = "normal"
    path = state_file or CONTEXT_MODE_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mode": normalized,
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def read_context_mode(
    state_file: Path | None = None,
    max_age_secs: int | None = None,
) -> ContextModeState:
    path = state_file or CONTEXT_MODE_STATE_FILE
    max_age = max_age_secs or int(os.environ.get(
        "MEMORY_OS_CONTEXT_MODE_MAX_AGE_SECS",
        str(DEFAULT_WORKING_SET_MAX_AGE_SECS),
    ))
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ContextModeState(mode="", last_seen_at="", age_secs=float("inf"), active=False)
    if not isinstance(state, dict):
        return ContextModeState(mode="", last_seen_at="", age_secs=float("inf"), active=False)
    mode = str(state.get("mode", "")).lower()
    last_seen_at = str(state.get("last_seen_at", ""))
    seen_ts = _parse_timestamp(last_seen_at)
    if seen_ts is None:
        return ContextModeState(mode=mode, last_seen_at=last_seen_at, age_secs=float("inf"), active=False, reason=str(state.get("reason", "")))
    age = max(0.0, time.time() - seen_ts)
    return ContextModeState(
        mode=mode,
        last_seen_at=last_seen_at,
        age_secs=age,
        active=mode in {"pressure", "working_set"} and age <= max_age,
        reason=str(state.get("reason", "")),
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            parts.append(str(item.get("text", "")))
        elif item_type == "tool_use":
            name = item.get("name", "tool")
            parts.append(f"[tool_use:{name}]")
        elif item_type == "tool_result":
            text = _content_text(item.get("content", ""))
            if text:
                parts.append(f"[tool_result:{text[:240]}]")
    return "\n".join(part for part in parts if part)


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    start = max(0, size - max_bytes)
    try:
        with path.open("rb") as f:
            if start:
                f.seek(start)
                f.readline()
            raw = f.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def build_working_set_from_transcript(
    prompt: str,
    transcript_path: Path | None,
    *,
    max_tail_bytes: int = 512_000,
    max_items: int = 24,
) -> dict[str, Any]:
    """Build a deterministic active working set instead of retaining raw history."""
    messages: list[dict[str, str]] = []
    tool_refs: list[dict[str, str]] = []
    if transcript_path and transcript_path.exists():
        for line in _tail_lines(transcript_path, max_tail_bytes):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", ""))
            text = _content_text(message.get("content", "")).strip()
            if not text:
                continue
            if "[tool_result:" in text or "[tool_use:" in text:
                tool_refs.append({"role": role, "summary": text[:300]})
            else:
                messages.append({"role": role, "text": text[:800]})
    recent = messages[-max_items:]
    latest_user = next((m["text"] for m in reversed(recent) if m["role"] == "user"), "")
    return {
        "schema_version": 1,
        "mode": "working_set",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "current_prompt": prompt[:1200],
        "latest_user_goal": latest_user[:800] if latest_user else prompt[:800],
        "recent_messages": recent,
        "tool_evidence_refs": tool_refs[-12:],
        "policy": {
            "resident": "active working set only",
            "swapped": "raw transcript/tool output remains addressable via transcript_path and context pages",
            "optional_context": "shed while working_set mode is active",
        },
        "transcript_path": str(transcript_path) if transcript_path else "",
    }


def write_working_set(
    prompt: str,
    transcript_path: Path | None,
    *,
    working_set_file: Path | None = None,
) -> dict[str, Any]:
    path = working_set_file or WORKING_SET_FILE
    working_set = build_working_set_from_transcript(prompt, transcript_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(working_set, ensure_ascii=False, indent=2), encoding="utf-8")
    return working_set


def format_working_set_notice(working_set: dict[str, Any], *, max_chars: int = 1200) -> str:
    recent = working_set.get("recent_messages", [])
    recent_count = len(recent) if isinstance(recent, list) else 0
    tool_refs = working_set.get("tool_evidence_refs", [])
    tool_count = len(tool_refs) if isinstance(tool_refs, list) else 0
    text = (
        "[context_kernel] 已自动进入 working-set 模式：系统将压制可选上下文注入，"
        "只保留当前目标/近期决策/证据索引，原始 transcript 与工具输出作为可寻址证据留在本地。\n"
        f"working_set={WORKING_SET_FILE} recent_messages={recent_count} tool_refs={tool_count}\n"
        f"latest_goal={str(working_set.get('latest_user_goal', ''))[:360]}"
    )
    return text[:max_chars]

