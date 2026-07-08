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
import time
from pathlib import Path
from typing import Any, TextIO, TypedDict


def _claude_project_slug(cwd: str) -> str:
    resolved = str(Path(cwd).expanduser().resolve())
    return resolved.replace("/", "-")

MEMORY_OS_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(__file__).resolve().parents[3]
for import_root in (MEMORY_OS_ROOT, WORKSPACE):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

try:
    from harness_obs.heartbeat import record_run  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - hook must not fail if observability import breaks
    record_run = None  # type: ignore[assignment]

try:
    from memory_os.runtime.context.kernel_compat import (  # type: ignore[import-untyped]
        build_working_set_manifest,
        extract_transcript_pages,
        manifest_context,
    )
except Exception:  # pragma: no cover - prompt guard must keep working without package imports
    build_working_set_manifest = None  # type: ignore[assignment]
    extract_transcript_pages = None  # type: ignore[assignment]
    manifest_context = None  # type: ignore[assignment]

from context_governor import (  # noqa: E402
    command_name,
    enforce_additional_context,
    format_emergency_notice,
    format_working_set_notice,
    is_local_command,
    maybe_enter_emergency,
    prompt_text,
    record_context_oom,
    write_context_mode,
    write_pressure_state,
    write_rss_snapshot,
    write_working_set,
)
from lib.prompt_io import read_hook_input  # noqa: E402

DEFAULT_PROMPT_CHAR_BUDGET = 120_000
DEFAULT_TOTAL_WARN_CHAR_BUDGET = 240_000
DEFAULT_TOTAL_HARD_CHAR_BUDGET = 320_000
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
MEMORY_OS_DIR = Path(os.environ.get("MEMORY_OS_DIR", Path.home() / ".claude" / "memory-os")).expanduser()
THRASHING_STATE_FILE = MEMORY_OS_DIR / "thrashing_state.json"
TRANSCRIPT_RECLAIM_MAX_STDIO_CHARS = int(os.environ.get("MEMORY_OS_TRANSCRIPT_RECLAIM_MAX_STDIO_CHARS", "4000"))
TRANSCRIPT_RECLAIM_MAX_CONTENT_CHARS = int(os.environ.get("MEMORY_OS_TRANSCRIPT_RECLAIM_MAX_CONTENT_CHARS", "2500"))
TRANSCRIPT_RECLAIM_MAX_FIELD_CHARS = int(os.environ.get("MEMORY_OS_TRANSCRIPT_RECLAIM_MAX_FIELD_CHARS", "2000"))
TRANSCRIPT_RECLAIM_MAX_RECORD_CHARS = int(os.environ.get("MEMORY_OS_TRANSCRIPT_RECLAIM_MAX_RECORD_CHARS", "32000"))
BIG_PAYLOAD_KEYS = frozenset({
    "old_string", "new_string", "oldString", "newString", "content", "command",
    "stdout", "stderr", "text", "file_content",
})


class BudgetDetail(TypedDict):
    prompt_chars: int
    prompt_budget: int
    transcript_chars: int
    transcript_accounting: str
    static_reserve_chars: int
    downstream_context_reserve_chars: int
    projected_context_chars: int
    total_context_warn_chars: int
    total_context_hard_chars: int


def _read_input() -> dict[str, Any]:
    return read_hook_input()


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


def _candidate_transcript_dirs(data: dict[str, Any]) -> list[Path]:
    values = [data.get("cwd"), os.environ.get("CLAUDE_CWD"), os.getcwd()]
    dirs: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            project_dir = Path.home() / ".claude" / "projects" / _claude_project_slug(value)
        except OSError:
            continue
        if project_dir not in seen:
            seen.add(project_dir)
            dirs.append(project_dir)
    return dirs


def _latest_transcript_for_session(data: dict[str, Any]) -> Path | None:
    session_id = _session_id(data)
    latest: tuple[float, Path] | None = None
    for project_dir in _candidate_transcript_dirs(data):
        if not project_dir.is_dir():
            continue
        if session_id:
            direct = project_dir / f"{session_id}.jsonl"
            if direct.exists() and direct.is_file():
                return direct
            continue
        try:
            candidates = list(project_dir.glob("*.jsonl"))
        except OSError:
            continue
        for candidate in candidates:
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if not candidate.is_file():
                continue
            if latest is None or stat.st_mtime > latest[0]:
                latest = (stat.st_mtime, candidate)
    return latest[1] if latest is not None else None


def _transcript_path(data: dict[str, Any]) -> Path | None:
    value = data.get("transcript_path") or data.get("transcriptPath") or os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
    if isinstance(value, str) and value.strip():
        path = Path(value).expanduser()
        if path.exists() and path.is_file():
            return path
    return _latest_transcript_for_session(data)


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


def _hash_text(value: str) -> str:
    digest = 2166136261
    for char in value:
        digest ^= ord(char)
        digest = (digest * 16777619) & 0xFFFFFFFF
    return f"{digest:08x}"


def _truncate_text_field(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    head = int(limit * 0.6)
    tail = limit - head
    return (
        value[:head]
        + f"\n...[transcript-reclaim truncated {len(value) - limit} chars, hash={_hash_text(value)}]...\n"
        + value[-tail:]
    )


def _sanitize_large_payload(value: Any, key: str = "", depth: int = 0) -> Any:
    if isinstance(value, str):
        limit = TRANSCRIPT_RECLAIM_MAX_FIELD_CHARS if key in BIG_PAYLOAD_KEYS else TRANSCRIPT_RECLAIM_MAX_FIELD_CHARS * 2
        return _truncate_text_field(value, limit)
    if isinstance(value, list):
        return [_sanitize_large_payload(item, key, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        if depth > 8:
            return "[transcript-reclaim truncated: max depth]"
        return {child_key: _sanitize_large_payload(child_value, child_key, depth + 1) for child_key, child_value in value.items()}
    return value


def _shrink_posttool_attachment(attachment: dict[str, Any]) -> bool:
    changed = False
    for key, limit in (
        ("stdout", TRANSCRIPT_RECLAIM_MAX_STDIO_CHARS),
        ("stderr", TRANSCRIPT_RECLAIM_MAX_STDIO_CHARS),
        ("content", TRANSCRIPT_RECLAIM_MAX_CONTENT_CHARS),
    ):
        value = attachment.get(key)
        if isinstance(value, str) and len(value) > limit:
            attachment[key] = _truncate_text_field(value, limit)
            changed = True
    if "response" in attachment:
        before = json.dumps(attachment["response"], ensure_ascii=False, sort_keys=True)
        attachment["response"] = _sanitize_large_payload(attachment["response"])
        after = json.dumps(attachment["response"], ensure_ascii=False, sort_keys=True)
        changed = changed or len(after) < len(before)
    return changed


def _shrink_message_content(entry: dict[str, Any]) -> bool:
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    before = json.dumps(content, ensure_ascii=False, sort_keys=True)
    changed = False
    if isinstance(content, str) and len(content) > TRANSCRIPT_RECLAIM_MAX_RECORD_CHARS:
        message["content"] = _truncate_text_field(content, TRANSCRIPT_RECLAIM_MAX_RECORD_CHARS)
        changed = True
    elif isinstance(content, list):
        new_content: list[Any] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"tool_result", "text"}:
                item = dict(item)
                if "content" in item:
                    item["content"] = _sanitize_large_payload(item["content"], "content")
                if "text" in item:
                    item["text"] = _sanitize_large_payload(item["text"], "text")
            new_content.append(item)
        message["content"] = new_content
        changed = len(json.dumps(new_content, ensure_ascii=False, sort_keys=True)) < len(before)
    return changed


def _reclaim_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    before = len(json.dumps(entry, ensure_ascii=False))
    changed = False
    attachment = entry.get("attachment")
    if isinstance(attachment, dict) and str(attachment.get("hookName", "")).startswith("PostToolUse"):
        changed = _shrink_posttool_attachment(attachment) or changed
    changed = _shrink_message_content(entry) or changed
    encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > TRANSCRIPT_RECLAIM_MAX_RECORD_CHARS and isinstance(attachment, dict):
        attachment.clear()
        attachment.update({
            "type": "hook_reclaimed",
            "hookName": "PostToolUse:reclaimed",
            "_transcript_reclaim": {
                "original_record_chars": before,
                "max_record_chars": TRANSCRIPT_RECLAIM_MAX_RECORD_CHARS,
            },
        })
        changed = True
    return entry, changed or len(json.dumps(entry, ensure_ascii=False)) < before


def _append_tail_if_any(path: Path, dst: TextIO, processed_bytes: int) -> int:
    try:
        current_size = path.stat().st_size
    except OSError:
        return processed_bytes
    if current_size <= processed_bytes:
        return processed_bytes
    with path.open("rb") as live:
        live.seek(processed_bytes)
        copied = processed_bytes
        for raw in live:
            copied += len(raw)
            dst.write(raw.decode("utf-8", errors="replace"))
        return copied


def _append_old_inode_tail_after_replace(src: Any, path: Path, copied_bytes: int) -> int:
    """Copy appends that landed on the old transcript inode around os.replace()."""
    stable_rounds = 0
    while stable_rounds < 2:
        try:
            old_size = os.fstat(src.fileno()).st_size
        except OSError:
            return copied_bytes
        if old_size <= copied_bytes:
            stable_rounds += 1
            time.sleep(0.01)
            continue
        src.seek(copied_bytes)
        with path.open("ab") as dst:
            while copied_bytes < old_size:
                chunk = src.read(min(65536, old_size - copied_bytes))
                if not chunk:
                    break
                dst.write(chunk)
                copied_bytes += len(chunk)
        stable_rounds = 0
    return copied_bytes


def reclaim_transcript_context(path: Path) -> dict[str, int | str]:
    """Shrink oversized hook attachments in-place before the next model request.

    This is an automatic shrinker, not a user-visible /compact. It keeps JSONL
    structure and semantic pointers while removing duplicated hook stdio/payloads
    that otherwise block the user with API 400.
    """
    try:
        old_size = path.stat().st_size
    except OSError:
        return {"changed_records": 0, "old_bytes": 0, "new_bytes": 0, "saved_bytes": 0, "backup": ""}

    backup = path.with_suffix(path.suffix + f".bak-auto-reclaim-{int(time.time())}")
    tmp = path.with_suffix(path.suffix + f".tmp-auto-reclaim-{os.getpid()}")
    changed_records = 0
    processed_bytes = 0
    try:
        backup.write_bytes(path.read_bytes())
        with path.open("rb") as src, tmp.open("w", encoding="utf-8") as dst:
            for raw in src:
                processed_bytes += len(raw)
                line = raw.decode("utf-8", errors="replace")
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    dst.write(line)
                    continue
                if not isinstance(entry, dict):
                    dst.write(line)
                    continue
                entry, changed = _reclaim_entry(entry)
                if changed:
                    changed_records += 1
                dst.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            processed_bytes = _append_tail_if_any(path, dst, processed_bytes)
        with path.open("rb") as old_inode:
            os.replace(tmp, path)
            processed_bytes = _append_old_inode_tail_after_replace(old_inode, path, processed_bytes)
        new_size = path.stat().st_size
        return {
            "changed_records": changed_records,
            "old_bytes": old_size,
            "new_bytes": new_size,
            "saved_bytes": max(0, old_size - new_size),
            "backup": str(backup),
        }
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"changed_records": 0, "old_bytes": old_size, "new_bytes": old_size, "saved_bytes": 0, "backup": str(backup)}


def _session_id(data: dict[str, Any]) -> str:
    value = data.get("session_id") or data.get("sessionId") or os.environ.get("CLAUDE_SESSION_ID", "")
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


def _budget_detail(data: dict[str, Any], prompt: str) -> BudgetDetail:
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
        cmd = command_name(prompt)
        if cmd in {"/compact", "/clear"}:
            write_pressure_state("low", f"rescue command {cmd} reset context pressure")
            write_context_mode("normal", f"rescue command {cmd} reset context mode")
        _record(True, f"skipped local command {cmd}")
        sys.exit(0)

    detail = _budget_detail(data, prompt)
    transcript = _transcript_path(data)
    reclaim_result: dict[str, int | str] | None = None
    if transcript and detail["projected_context_chars"] > detail["total_context_warn_chars"]:
        reclaim_result = reclaim_transcript_context(transcript)
        if int(reclaim_result.get("saved_bytes", 0) or 0) > 0:
            detail = _budget_detail(data, prompt)
    manifest_notice = ""
    if transcript and build_working_set_manifest is not None and manifest_context is not None:
        try:
            if extract_transcript_pages is not None and detail["projected_context_chars"] > detail["total_context_warn_chars"]:
                extraction = extract_transcript_pages(transcript, max_lines=400)
                manifest_notice = manifest_context(extraction.manifest)
            else:
                manifest_notice = manifest_context(build_working_set_manifest(prompt=prompt))
        except Exception:
            manifest_notice = ""
    snapshot = write_rss_snapshot(dict(detail), prompt=prompt, transcript_path=transcript)
    if reclaim_result:
        snapshot["transcript_reclaim"] = reclaim_result
    if manifest_notice:
        snapshot["working_set_manifest_context_chars"] = len(manifest_notice)

    trace_id = ""
    if "trace_id:" in prompt:
        trace_id = prompt.rsplit("trace_id:", 1)[-1].strip().split()[0].strip("，,。.;")

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
        write_context_mode(
            "pressure",
            f"prompt chars={detail['prompt_chars']} exceeds budget={detail['prompt_budget']}",
        )
        _record(True, f"warn prompt chars={detail['prompt_chars']} budget={detail['prompt_budget']}")
        sys.stdout.write(json.dumps({"decision": "approve", "reason": reason, "detail": detail}, ensure_ascii=False))
        sys.exit(0)

    if detail["projected_context_chars"] > detail["total_context_hard_chars"]:
        pressure_reason = (
            "projected context "
            f"chars={detail['projected_context_chars']} exceeds hard={detail['total_context_hard_chars']}"
        )
        write_pressure_state("critical", pressure_reason)
        if trace_id:
            record_context_oom(trace_id, reason="user reported context window 400 during hard overflow", snapshot=snapshot)
        emergency = maybe_enter_emergency(dict(detail), reason=pressure_reason, trace_id=trace_id)
        if not emergency:
            write_context_mode("working_set", pressure_reason)
        working_set = write_working_set(prompt, _transcript_path(data))
        notice = format_emergency_notice(dict(detail)) if emergency else format_working_set_notice(working_set)
        if manifest_notice:
            notice = (notice + "\n" + manifest_notice)[:6000]
        reason = (
            "[prompt_budget_guard] CRITICAL: projected request context "
            f"chars={detail['projected_context_chars']} exceeds hard budget={detail['total_context_hard_chars']} "
            f"(warn={detail['total_context_warn_chars']}, transcript={detail['transcript_chars']}, "
            f"prompt={detail['prompt_chars']}, static_reserve={detail['static_reserve_chars']}, "
            f"downstream_reserve={detail['downstream_context_reserve_chars']}). "
            f"已自动回收 transcript saved_bytes={int((reclaim_result or {}).get('saved_bytes', 0) or 0)}；"
            "不阻断用户输入；已强制接管上下文治理并压制可选上下文注入。"
        )
        _record(
            True,
            "critical working_set reclaim "
            f"chars={detail['projected_context_chars']} hard={detail['total_context_hard_chars']}",
        )
        output = {
            "decision": "approve",
            "reason": reason,
            "detail": detail,
            "transcript_reclaim": reclaim_result or {},
        }
        governed = enforce_additional_context(
            data,
            notice,
            producer="prompt_budget_guard",
            hook_event_name="UserPromptSubmit",
            mandatory=True,
        )
        if governed:
            output.update(governed)
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
        sys.exit(0)

    if detail["projected_context_chars"] > detail["total_context_warn_chars"]:
        pressure_reason = (
            "projected context "
            f"chars={detail['projected_context_chars']} exceeds warn={detail['total_context_warn_chars']}"
        )
        write_pressure_state("high", pressure_reason)
        write_context_mode("working_set", pressure_reason)
        working_set = write_working_set(prompt, _transcript_path(data))
        notice = format_working_set_notice(working_set)
        if manifest_notice:
            notice = (notice + "\n" + manifest_notice)[:6000]
        reason = (
            "[prompt_budget_guard] WARN: projected request context "
            f"chars={detail['projected_context_chars']} exceeds warning budget={detail['total_context_warn_chars']} "
            f"(hard={detail['total_context_hard_chars']}, transcript={detail['transcript_chars']}, "
            f"prompt={detail['prompt_chars']}, static_reserve={detail['static_reserve_chars']}, "
            f"downstream_reserve={detail['downstream_context_reserve_chars']}). "
            "已提前进入 working-set 模式并压制可选上下文注入，按 kswapd 水位治理无感降载。"
        )
        _record(
            True,
            "warn proactive working_set reclaim "
            f"chars={detail['projected_context_chars']} warn={detail['total_context_warn_chars']} "
            f"hard={detail['total_context_hard_chars']}",
        )
        output = {
            "decision": "approve",
            "reason": reason,
            "detail": detail,
        }
        governed = enforce_additional_context(
            data,
            notice,
            producer="prompt_budget_guard",
            hook_event_name="UserPromptSubmit",
            mandatory=True,
        )
        if governed:
            output.update(governed)
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
        sys.exit(0)

    write_pressure_state("low", "prompt/context within budget")
    write_context_mode("normal", "prompt/context within budget")
    _record(
        True,
        "prompt/context within budget "
        f"prompt={detail['prompt_chars']} projected={detail['projected_context_chars']} "
        f"warn={detail['total_context_warn_chars']} hard={detail['total_context_hard_chars']}",
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
