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
    return read_pressure_state(state_file=state_file, max_age_secs=max_age_secs).active
