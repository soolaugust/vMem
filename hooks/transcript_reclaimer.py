#!/usr/bin/env python3
"""Hard reclaim oversized Claude transcript JSONL files.

This is the request-assembly safety valve that complements vMem retrieval
shedding: when the host transcript itself is too large, optional context
shedding is not enough.  The reclaimer backs up the original transcript and
rewrites the active JSONL to a bounded working set plus evidence references.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TARGET_BYTES = 4 * 1024 * 1024
DEFAULT_KEEP_TAIL_LINES = 400
DEFAULT_MAX_LINE_BYTES = 32 * 1024
DEFAULT_EVIDENCE_LINES = 200
RECENT_PREVIEW_CHARS = 12_000


@dataclass
class ReclaimResult:
    ok: bool
    changed: bool
    transcript_path: str
    original_bytes: int
    reclaimed_bytes: int
    backup_path: str
    manifest_path: str
    kept_lines: int
    summarized_lines: int
    dropped_lines: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "transcript_path": self.transcript_path,
            "original_bytes": self.original_bytes,
            "reclaimed_bytes": self.reclaimed_bytes,
            "backup_path": self.backup_path,
            "manifest_path": self.manifest_path,
            "kept_lines": self.kept_lines,
            "summarized_lines": self.summarized_lines,
            "dropped_lines": self.dropped_lines,
            "reason": self.reason,
        }


def _safe_json(line: bytes) -> dict[str, Any] | None:
    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _message_role(obj: dict[str, Any]) -> str:
    msg = obj.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("role"), str):
        return msg["role"]
    return str(obj.get("role") or obj.get("type") or "")


def _record_type(obj: dict[str, Any] | None) -> str:
    if not obj:
        return "invalid"
    return str(obj.get("type") or _message_role(obj) or "unknown")


def _summarize_line(raw: bytes, index: int, backup_path: Path) -> bytes:
    obj = _safe_json(raw)
    record_type = _record_type(obj)
    role = _message_role(obj) if obj else "invalid"
    summary = {
        "type": "reclaimed-transcript-line",
        "reclaim": True,
        "original_line_index": index,
        "original_bytes": len(raw),
        "original_type": record_type,
        "original_role": role,
        "evidence": str(backup_path),
        "note": "Large or cold transcript line reclaimed by vMem; consult backup only if exact evidence is needed.",
    }
    return (json.dumps(summary, ensure_ascii=False) + "\n").encode("utf-8")


def _preview_text(value: str, limit: int = RECENT_PREVIEW_CHARS) -> str:
    if len(value) <= limit:
        return value
    head = limit // 2
    tail = limit - head
    return value[:head] + "\n...[vMem reclaimed middle; see transcript backup for exact full text]...\n" + value[-tail:]


def _recent_preview(obj: dict[str, Any] | None) -> Any:
    if not obj:
        return None
    msg = obj.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return _preview_text(content)
        if isinstance(content, list):
            preview_items = []
            for item in content[:8]:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    clone = dict(item)
                    clone["text"] = _preview_text(item["text"])
                    preview_items.append(clone)
                else:
                    preview_items.append(item)
            return preview_items
    if isinstance(obj.get("lastPrompt"), str):
        return _preview_text(obj["lastPrompt"])
    return None


def _tiny_preview(raw: bytes, limit: int = 240) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    return text[:head] + "...[vMem evidence]..." + text[-tail:]


def _recent_line_stub(raw: bytes, index: int, backup_path: Path) -> bytes:
    obj = _safe_json(raw)
    summary = {
        "type": "vMem-reclaimed-recent-line-stub",
        "reclaim": True,
        "original_line_index": index,
        "original_bytes": len(raw),
        "original_type": _record_type(obj),
        "original_role": _message_role(obj) if obj else "invalid",
        "preview": _tiny_preview(raw),
        "evidence": str(backup_path),
    }
    return (json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _recent_line_summary(raw: bytes, index: int, backup_path: Path) -> bytes:
    obj = _safe_json(raw)
    record_type = _record_type(obj)
    role = _message_role(obj) if obj else "invalid"
    summary = {
        "type": "vMem-reclaimed-recent-line",
        "reclaim": True,
        "original_line_index": index,
        "original_bytes": len(raw),
        "original_type": record_type,
        "original_role": role,
        "content_preview": _recent_preview(obj),
        "evidence": str(backup_path),
        "note": "Recent/current-goal transcript line was too large for active context; bounded preview is preserved and exact text is in backup.",
    }
    encoded = (json.dumps(summary, ensure_ascii=False) + "\n").encode("utf-8")
    return encoded if len(encoded) <= max(DEFAULT_MAX_LINE_BYTES, 4096) else _recent_line_stub(raw, index, backup_path)


def _should_keep_full(raw: bytes, obj: dict[str, Any] | None, index: int, tail_start: int, max_line_bytes: int) -> bool:
    if index >= tail_start and len(raw) <= max_line_bytes:
        return True
    if not obj:
        return False
    record_type = str(obj.get("type") or "")
    # Keep small session metadata because Claude Code uses it for UI/session state.
    if record_type in {"summary", "system", "mode", "permission-mode", "ai-title", "last-prompt"} and len(raw) <= max_line_bytes:
        return True
    return False


def _bounded_fallback(raw_lines: list[bytes], header_line: bytes, backup_path: Path, target_bytes: int, max_line_bytes: int) -> tuple[list[bytes], int, int, int]:
    bounded = [header_line]
    total = len(header_line)
    kept = summarized = dropped = 0
    indexed_lines = list(enumerate(raw_lines))
    for index, raw in reversed(indexed_lines):
        line = raw if raw.endswith(b"\n") else raw + b"\n"
        line_summarized = False
        if len(line) > max_line_bytes:
            line = _recent_line_summary(line, index, backup_path)
            line_summarized = True
        if total + len(line) > target_bytes:
            dropped += 1
            continue
        bounded.append(line)
        total += len(line)
        if line_summarized:
            summarized += 1
        else:
            kept += 1
    return [bounded[0]] + list(reversed(bounded[1:])), kept, summarized, dropped


def _claim_evidence_paths(evidence_dir: Path, transcript_path: Path) -> tuple[Path, Path]:
    for attempt in range(1000):
        stamp = f"{time.time_ns()}-{os.getpid()}-{attempt}"
        backup_path = evidence_dir / f"{transcript_path.stem}.bak-reclaim-{stamp}.jsonl"
        manifest_path = evidence_dir / f"{transcript_path.stem}.reclaim-{stamp}.json"
        if not backup_path.exists() and not manifest_path.exists():
            return backup_path, manifest_path
    raise RuntimeError("could not allocate unique transcript reclaim evidence paths")


def _append_bounded_tail_bytes(
    output: list[bytes],
    tail: bytes,
    backup_path: Path,
    target_bytes: int,
    max_line_bytes: int,
    start_index: int,
    *,
    force_stub: bool = False,
    evict_existing: bool = True,
) -> tuple[list[bytes], int, int, int]:
    if not tail:
        return output, 0, 0, 0
    total = sum(len(part) for part in output)
    kept = summarized = dropped = 0
    for offset, raw in enumerate(tail.splitlines(keepends=True)):
        line = raw if raw.endswith(b"\n") else raw + b"\n"
        line_summarized = False
        if force_stub or len(line) > max_line_bytes:
            line = _recent_line_stub(raw, start_index + offset, backup_path) if force_stub else _recent_line_summary(line, start_index + offset, backup_path)
            line_summarized = True
        while evict_existing and total + len(line) > target_bytes and len(output) > 1:
            total -= len(output.pop(1))
            dropped += 1
        if total + len(line) > target_bytes and line_summarized:
            line = _recent_line_stub(raw, start_index + offset, backup_path)
            while evict_existing and total + len(line) > target_bytes and len(output) > 1:
                total -= len(output.pop(1))
                dropped += 1
        if total + len(line) > target_bytes:
            dropped += 1
            continue
        if not line.startswith(b"\n") and output and not output[-1].endswith(b"\n"):
            newline = b"\n"
            if total + len(newline) > target_bytes:
                dropped += 1
                continue
            output.append(newline)
            total += len(newline)
        output.append(line)
        total += len(line)
        if line_summarized:
            summarized += 1
        else:
            kept += 1
    return output, kept, summarized, dropped


def _append_bounded_tail(
    output: list[bytes],
    original_bytes: bytes,
    current_bytes: bytes,
    backup_path: Path,
    target_bytes: int,
    max_line_bytes: int,
    start_index: int,
) -> tuple[list[bytes], int, int, int]:
    if len(current_bytes) <= len(original_bytes) or not current_bytes.startswith(original_bytes):
        return output, 0, 0, 0
    return _append_bounded_tail_bytes(
        output,
        current_bytes[len(original_bytes):],
        backup_path,
        target_bytes,
        max_line_bytes,
        start_index,
    )


def _append_old_inode_tail_after_replace(
    output: list[bytes],
    old_inode: Any,
    copied_bytes: int,
    backup_path: Path,
    target_bytes: int,
    max_line_bytes: int,
    start_index: int,
) -> tuple[list[bytes], int, int, int, int]:
    kept = summarized = dropped = 0
    stable_rounds = 0
    while stable_rounds < 2:
        try:
            old_size = os.fstat(old_inode.fileno()).st_size
        except OSError:
            return output, copied_bytes, kept, summarized, dropped
        if old_size <= copied_bytes:
            stable_rounds += 1
            time.sleep(0.01)
            continue
        old_inode.seek(copied_bytes)
        tail = old_inode.read(old_size - copied_bytes)
        copied_bytes = old_size
        if tail:
            with backup_path.open("ab") as evidence:
                evidence.write(tail)
        output, tail_kept, tail_summarized, tail_dropped = _append_bounded_tail_bytes(
            output,
            tail,
            backup_path,
            target_bytes,
            max_line_bytes,
            start_index,
            force_stub=True,
            evict_existing=False,
        )
        start_index += len(tail.splitlines())
        kept += tail_kept
        summarized += tail_summarized
        dropped += tail_dropped
        stable_rounds = 0
    return output, copied_bytes, kept, summarized, dropped


def reclaim_transcript(
    transcript_path: Path,
    *,
    memory_dir: Path | None = None,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    keep_tail_lines: int = DEFAULT_KEEP_TAIL_LINES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    dry_run: bool = False,
    reason: str = "context hard pressure",
) -> ReclaimResult:
    transcript_path = transcript_path.expanduser().resolve()
    memory_dir = (memory_dir or Path(os.environ.get("MEMORY_OS_DIR", Path.home() / ".claude" / "memory-os"))).expanduser()
    evidence_dir = memory_dir / "evidence" / "transcripts"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    original_bytes = transcript_path.stat().st_size
    if original_bytes <= target_bytes:
        return ReclaimResult(True, False, str(transcript_path), original_bytes, original_bytes, "", "", 0, 0, 0, "already below target")

    original_content = transcript_path.read_bytes()
    raw_lines = original_content.splitlines(keepends=True)
    tail_start = max(0, len(raw_lines) - keep_tail_lines)
    backup_path, manifest_path = _claim_evidence_paths(evidence_dir, transcript_path)

    output: list[bytes] = []
    kept = summarized = dropped = 0
    manifest: dict[str, Any] = {
        "created_at_epoch": time.time(),
        "transcript_path": str(transcript_path),
        "backup_path": str(backup_path),
        "original_bytes": original_bytes,
        "target_bytes": target_bytes,
        "keep_tail_lines": keep_tail_lines,
        "max_line_bytes": max_line_bytes,
        "reason": reason,
        "summaries": [],
    }

    header = {
        "type": "vMem-reclaim-header",
        "reclaim": True,
        "original_transcript_bytes": original_bytes,
        "backup_path": str(backup_path),
        "manifest_path": str(manifest_path),
        "reason": reason,
        "note": "Older/cold transcript evidence was moved out of the active context window. Recent lines are preserved below.",
    }
    output.append((json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8"))

    tail_dropped = 0
    for index, raw in enumerate(raw_lines):
        obj = _safe_json(raw)
        if _should_keep_full(raw, obj, index, tail_start, max_line_bytes):
            output.append(raw if raw.endswith(b"\n") else raw + b"\n")
            kept += 1
            continue
        if index >= tail_start:
            summary = _recent_line_summary(raw, index, backup_path)
            if sum(len(part) for part in output) + len(summary) <= target_bytes:
                output.append(summary)
                summarized += 1
                manifest["summaries"].append({"line": index, "bytes": len(raw), "type": _record_type(obj), "recent": True})
            else:
                dropped += 1
                tail_dropped += 1
            continue
        if len(raw) > max_line_bytes:
            summary = _summarize_line(raw, index, backup_path)
            if sum(len(part) for part in output) + len(summary) <= target_bytes:
                output.append(summary)
                summarized += 1
                manifest["summaries"].append({"line": index, "bytes": len(raw), "type": _record_type(obj)})
            else:
                dropped += 1
        else:
            dropped += 1

    new_bytes = sum(len(part) for part in output)
    # If still too large, or if old evidence crowded out current tail, keep only header + bounded recent lines.
    if new_bytes > target_bytes or tail_dropped:
        output, kept, summarized, dropped = _bounded_fallback(raw_lines, output[0], backup_path, target_bytes, max_line_bytes)
        new_bytes = sum(len(part) for part in output)

    if not dry_run:
        backup_path.write_bytes(original_content)
        manifest.update({
            "kept_lines": kept,
            "summarized_lines": summarized,
            "dropped_lines": dropped,
        })
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path = transcript_path.with_suffix(transcript_path.suffix + ".reclaim-tmp")
        tmp_path.write_bytes(b"".join(output))
        current_content = transcript_path.read_bytes()
        if len(current_content) > len(original_content) and current_content.startswith(original_content):
            with backup_path.open("ab") as evidence:
                evidence.write(current_content[len(original_content):])
        output, tail_kept, tail_summarized, tail_dropped = _append_bounded_tail(
            output,
            original_content,
            current_content,
            backup_path,
            target_bytes,
            max_line_bytes,
            len(raw_lines),
        )
        copied_bytes = len(current_content)
        late_tail_reserve = min(4096, max(0, target_bytes - len(output[0])))
        reserve_target = max(len(output[0]), target_bytes - late_tail_reserve)
        while sum(len(part) for part in output) > reserve_target and len(output) > 1:
            output.pop(1)
            dropped += 1
        tmp_path.write_bytes(b"".join(output))
        active_base_bytes = sum(len(part) for part in output)
        with transcript_path.open("rb") as old_inode:
            tmp_path.replace(transcript_path)
            output, copied_bytes, late_kept, late_summarized, late_dropped = _append_old_inode_tail_after_replace(
                output,
                old_inode,
                copied_bytes,
                backup_path,
                target_bytes,
                max_line_bytes,
                len(raw_lines) + tail_kept + tail_summarized + tail_dropped,
            )
        if late_kept or late_summarized or late_dropped:
            with transcript_path.open("ab") as active:
                active.write(b"".join(output)[active_base_bytes:])
        tail_kept += late_kept
        tail_summarized += late_summarized
        tail_dropped += late_dropped
        kept += tail_kept
        summarized += tail_summarized
        dropped += tail_dropped
        new_bytes = sum(len(part) for part in output)
        manifest["reclaimed_bytes"] = new_bytes
        manifest["kept_lines"] = kept
        manifest["summarized_lines"] = summarized
        manifest["dropped_lines"] = dropped
        manifest["concurrent_tail_kept_lines"] = tail_kept
        manifest["concurrent_tail_summarized_lines"] = tail_summarized
        manifest["concurrent_tail_dropped_lines"] = tail_dropped
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return ReclaimResult(True, True, str(transcript_path), original_bytes, new_bytes, str(backup_path), str(manifest_path), kept, summarized, dropped, reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hard reclaim an oversized Claude transcript")
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--memory-dir", type=Path, default=None)
    parser.add_argument("--target-bytes", type=int, default=DEFAULT_TARGET_BYTES)
    parser.add_argument("--keep-tail-lines", type=int, default=DEFAULT_KEEP_TAIL_LINES)
    parser.add_argument("--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reason", default="context hard pressure")
    args = parser.parse_args(argv)
    result = reclaim_transcript(
        args.transcript,
        memory_dir=args.memory_dir,
        target_bytes=args.target_bytes,
        keep_tail_lines=args.keep_tail_lines,
        max_line_bytes=args.max_line_bytes,
        dry_run=args.dry_run,
        reason=args.reason,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
