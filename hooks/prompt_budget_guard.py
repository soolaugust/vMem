#!/usr/bin/env python3
"""UserPromptSubmit 本地预算守门。

在模型 API 请求发出前做确定性大小检查，避免超大 prompt 或长会话历史进入
上游后触发 context/window 类 400。PostToolUse/Stop hook 已经太晚，必须在提交
阶段阻断。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[3]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

try:
    from harness_obs.heartbeat import record_run
except Exception:  # pragma: no cover - hook must not fail if observability import breaks
    record_run = None  # type: ignore[assignment]

from context_governor import command_name, is_local_command, prompt_text, write_pressure_state

DEFAULT_PROMPT_CHAR_BUDGET = 120_000
DEFAULT_TOTAL_WARN_CHAR_BUDGET = 420_000
DEFAULT_TOTAL_HARD_CHAR_BUDGET = 900_000
DEFAULT_STATIC_CONTEXT_RESERVE = 180_000
DEFAULT_DOWNSTREAM_CONTEXT_RESERVE = 40_000
TRANSCRIPT_TAIL_BYTES = 4_000_000
COMPACT_MARKER_SCAN_BYTES = int(os.environ.get("MEMORY_OS_COMPACT_MARKER_SCAN_BYTES", "4000000"))
PROMPT_BUDGET_ENV = "MEMORY_OS_PROMPT_CHAR_BUDGET"
TOTAL_WARN_BUDGET_ENV = "MEMORY_OS_TOTAL_CONTEXT_WARN_CHARS"
TOTAL_HARD_BUDGET_ENV = "MEMORY_OS_TOTAL_CONTEXT_HARD_CHARS"
LEGACY_TOTAL_BUDGET_ENV = "MEMORY_OS_TOTAL_CONTEXT_CHAR_BUDGET"
STATIC_RESERVE_ENV = "MEMORY_OS_STATIC_CONTEXT_RESERVE_CHARS"
DOWNSTREAM_RESERVE_ENV = "MEMORY_OS_DOWNSTREAM_CONTEXT_RESERVE_CHARS"
GUARD_NAME = "prompt_budget_guard"
MEMORY_OS_DIR = Path.home() / ".claude" / "memory-os"
THRASHING_STATE_FILE = MEMORY_OS_DIR / "thrashing_state.json"
def _read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _prompt_text(data: dict[str, Any]) -> str:
    return prompt_text(data)


def _is_local_slash_command(prompt: str) -> bool:
    return is_local_command(prompt)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _prompt_budget() -> int:
    return _env_int(PROMPT_BUDGET_ENV, DEFAULT_PROMPT_CHAR_BUDGET)


def _total_warn_budget() -> int:
    return _env_int(TOTAL_WARN_BUDGET_ENV, DEFAULT_TOTAL_WARN_CHAR_BUDGET)


def _total_hard_budget() -> int:
    if os.environ.get(TOTAL_HARD_BUDGET_ENV, "").strip():
        hard_budget = _env_int(TOTAL_HARD_BUDGET_ENV, DEFAULT_TOTAL_HARD_CHAR_BUDGET)
    elif os.environ.get(LEGACY_TOTAL_BUDGET_ENV, "").strip():
        hard_budget = _env_int(LEGACY_TOTAL_BUDGET_ENV, DEFAULT_TOTAL_HARD_CHAR_BUDGET)
    else:
        hard_budget = DEFAULT_TOTAL_HARD_CHAR_BUDGET
    return hard_budget


def _static_reserve() -> int:
    return _env_int(STATIC_RESERVE_ENV, DEFAULT_STATIC_CONTEXT_RESERVE)


def _downstream_reserve() -> int:
    return _env_int(DOWNSTREAM_RESERVE_ENV, DEFAULT_DOWNSTREAM_CONTEXT_RESERVE)


def _transcript_path(data: dict[str, Any]) -> Path | None:
    value = data.get("transcript_path") or os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return path if path.exists() and path.is_file() else None


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0

    total = 0
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            total += len(str(item.get("text", "")))
        elif item_type == "tool_use":
            total += len(json.dumps(item.get("input", {}), ensure_ascii=False))
        elif item_type == "tool_result":
            total += _content_chars(item.get("content", ""))
    return total


def _line_content_chars(line: str) -> tuple[int, bool]:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return len(line), False

    message = entry.get("message", {})
    if isinstance(message, dict):
        return _content_chars(message.get("content", "")), True
    return 0, False


def _transcript_context_chars_from_offset(path: Path, offset: int) -> int:
    try:
        size = path.stat().st_size
    except OSError:
        return 0

    offset = max(0, min(offset, size))
    raw_tail_bytes = size - offset
    chars = 0
    parsed_messages = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as transcript:
            if offset:
                transcript.seek(offset)
                transcript.readline()
            for line in transcript:
                line_chars, parsed = _line_content_chars(line)
                chars += line_chars
                parsed_messages += 1 if parsed else 0
    except OSError:
        return 0
    if parsed_messages and chars:
        return max(chars, raw_tail_bytes)
    return raw_tail_bytes


def _text_has_compact_marker(text: str) -> bool:
    if "SessionStart:compact" in text or "PostCompact" in text:
        return True
    return "compact" in text and "subtype" in text


def _object_has_compact_marker(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("subtype") == "compact":
            return True
        if value.get("hookEventName") == "PostCompact" or value.get("hook_event_name") == "PostCompact":
            return True
        if value.get("hookEvent") == "SessionStart" and value.get("hookName") == "SessionStart:compact":
            return True
        return any(_object_has_compact_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_object_has_compact_marker(item) for item in value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return _text_has_compact_marker(value)
        return _object_has_compact_marker(parsed) or _text_has_compact_marker(value)
    return False


def _is_compact_marker(line: str) -> bool:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return _text_has_compact_marker(line)
    return _object_has_compact_marker(entry)


def _last_compact_offset(path: Path) -> int | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None

    scan_start = max(0, size - COMPACT_MARKER_SCAN_BYTES)
    last_offset: int | None = None
    try:
        with path.open("rb") as transcript:
            if scan_start:
                transcript.seek(scan_start)
                transcript.readline()
            while True:
                line_offset = transcript.tell()
                line = transcript.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                if _is_compact_marker(text):
                    last_offset = line_offset
    except OSError:
        return None
    return last_offset


def _transcript_context_chars(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    return _transcript_context_chars_from_offset(path, max(0, size - TRANSCRIPT_TAIL_BYTES))


def _session_id(data: dict[str, Any]) -> str:
    value = data.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "")
    return value if isinstance(value, str) else ""


def _compact_epoch_chars(data: dict[str, Any], transcript: Path | None) -> int | None:
    if transcript is None:
        return None

    compact_offset = _last_compact_offset(transcript)
    if compact_offset is None:
        return None

    transcript_chars = _transcript_context_chars_from_offset(transcript, compact_offset)
    session_id = _session_id(data)
    try:
        state = json.loads(THRASHING_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    if state and session_id and state.get("session_id") == session_id:
        if int(state.get("epoch_id", 0) or 0) > 0 or float(state.get("last_compact_ts", 0) or 0) > 0:
            epoch_bytes = int(state.get("epoch_bytes", state.get("session_bytes", 0)) or 0)
            return max(transcript_chars, epoch_bytes, 0)

    return transcript_chars


def _budget_detail(data: dict[str, Any], prompt: str) -> dict[str, int | str]:
    transcript = _transcript_path(data)
    compact_epoch_chars = _compact_epoch_chars(data, transcript)
    transcript_chars = compact_epoch_chars if compact_epoch_chars is not None else (
        _transcript_context_chars(transcript) if transcript else 0
    )
    prompt_chars = len(prompt)
    static_reserve = _static_reserve()
    downstream_reserve = _downstream_reserve()
    return {
        "prompt_chars": prompt_chars,
        "prompt_budget": _prompt_budget(),
        "transcript_chars": transcript_chars,
        "transcript_accounting": "compact_epoch" if compact_epoch_chars is not None else "tail",
        "static_reserve_chars": static_reserve,
        "downstream_context_reserve_chars": downstream_reserve,
        "projected_context_chars": prompt_chars + transcript_chars + static_reserve + downstream_reserve,
        "total_context_warn_chars": _total_warn_budget(),
        "total_context_hard_chars": _total_hard_budget(),
    }


def _record(ok: bool, summary: str) -> None:
    if record_run is None:
        return
    try:
        record_run(GUARD_NAME, ok=ok, summary=summary)
    except Exception:
        pass


def main() -> None:
    data = _read_input()
    prompt = _prompt_text(data)
    if _is_local_slash_command(prompt):
        _record(True, f"skipped local command {command_name(prompt)}")
        sys.exit(0)

    detail = _budget_detail(data, prompt)

    if detail["prompt_chars"] > detail["prompt_budget"]:
        reason = (
            "[prompt_budget_guard] WARN: UserPromptSubmit prompt "
            f"chars={detail['prompt_chars']} exceeds local budget={detail['prompt_budget']}. "
            "不阻断用户输入；应在请求组装/上下文注入层降载，避免模型 API context/window 400。"
        )
        write_pressure_state(
            "high",
            f"prompt chars={detail['prompt_chars']} exceeds budget={detail['prompt_budget']}",
        )
        _record(True, f"warn prompt chars={detail['prompt_chars']} budget={detail['prompt_budget']}")
        sys.stdout.write(json.dumps({"decision": "approve", "reason": reason, "detail": detail}, ensure_ascii=False))
        sys.exit(0)

    if detail["projected_context_chars"] > detail["total_context_hard_chars"]:
        reason = (
            "[prompt_budget_guard] CRITICAL: projected request context "
            f"chars={detail['projected_context_chars']} exceeds hard budget={detail['total_context_hard_chars']} "
            f"(warn={detail['total_context_warn_chars']}, transcript={detail['transcript_chars']}, "
            f"prompt={detail['prompt_chars']}, static_reserve={detail['static_reserve_chars']}, "
            f"downstream_reserve={detail['downstream_context_reserve_chars']}). "
            "不阻断用户输入；已进入 critical pressure，后续上下文注入必须自动降载/换出冷内容，避免模型 API context/window 400。"
        )
        write_pressure_state(
            "critical",
            "projected context "
            f"chars={detail['projected_context_chars']} exceeds hard={detail['total_context_hard_chars']}",
        )
        _record(
            True,
            "critical projected context "
            f"chars={detail['projected_context_chars']} hard={detail['total_context_hard_chars']}",
        )
        sys.stdout.write(json.dumps({"decision": "approve", "reason": reason, "detail": detail}, ensure_ascii=False))
        sys.exit(0)

    if detail["projected_context_chars"] > detail["total_context_warn_chars"]:
        reason = (
            "[prompt_budget_guard] WARN: projected request context "
            f"chars={detail['projected_context_chars']} exceeds warning budget={detail['total_context_warn_chars']} "
            f"(hard={detail['total_context_hard_chars']}, transcript={detail['transcript_chars']}, "
            f"prompt={detail['prompt_chars']}, static_reserve={detail['static_reserve_chars']}, "
            f"downstream_reserve={detail['downstream_context_reserve_chars']}). "
            "当前会话偏大，本次不阻断；可在逻辑断点整理上下文。"
        )
        write_pressure_state(
            "high",
            "projected context "
            f"chars={detail['projected_context_chars']} exceeds warn={detail['total_context_warn_chars']}",
        )
        _record(
            True,
            "warn projected context "
            f"chars={detail['projected_context_chars']} warn={detail['total_context_warn_chars']} "
            f"hard={detail['total_context_hard_chars']}",
        )
        sys.stdout.write(json.dumps({"decision": "approve", "reason": reason, "detail": detail}, ensure_ascii=False))
        sys.exit(0)

    _record(
        True,
        "prompt/context within budget "
        f"prompt={detail['prompt_chars']} projected={detail['projected_context_chars']} "
        f"warn={detail['total_context_warn_chars']} hard={detail['total_context_hard_chars']}",
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
