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
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TARGET_BYTES = 4 * 1024 * 1024
DEFAULT_KEEP_TAIL_LINES = 400
DEFAULT_MAX_LINE_BYTES = 32 * 1024
DEFAULT_EVIDENCE_LINES = 200


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

    raw_lines = transcript_path.read_bytes().splitlines(keepends=True)
    tail_start = max(0, len(raw_lines) - keep_tail_lines)
    stamp = str(int(time.time()))
    backup_path = evidence_dir / f"{transcript_path.stem}.bak-reclaim-{stamp}.jsonl"
    manifest_path = evidence_dir / f"{transcript_path.stem}.reclaim-{stamp}.json"

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

    for index, raw in enumerate(raw_lines):
        obj = _safe_json(raw)
        if _should_keep_full(raw, obj, index, tail_start, max_line_bytes):
            output.append(raw if raw.endswith(b"\n") else raw + b"\n")
            kept += 1
            continue
        if index >= tail_start or len(raw) > max_line_bytes:
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
    # If still too large, keep only header + tail lines bounded by target.
    if new_bytes > target_bytes:
        header_line = output[0]
        bounded = [header_line]
        total = len(header_line)
        kept = summarized = dropped = 0
        for raw in reversed(raw_lines):
            line = raw if raw.endswith(b"\n") else raw + b"\n"
            if len(line) > max_line_bytes:
                line = _summarize_line(line, len(raw_lines) - kept - dropped - 1, backup_path)
                summarized += 1
            if total + len(line) > target_bytes:
                dropped += 1
                continue
            bounded.append(line)
            total += len(line)
            kept += 1
        output = [bounded[0]] + list(reversed(bounded[1:]))
        new_bytes = total

    if not dry_run:
        shutil.copy2(transcript_path, backup_path)
        manifest.update({
            "reclaimed_bytes": new_bytes,
            "kept_lines": kept,
            "summarized_lines": summarized,
            "dropped_lines": dropped,
        })
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path = transcript_path.with_suffix(transcript_path.suffix + ".reclaim-tmp")
        tmp_path.write_bytes(b"".join(output))
        tmp_path.replace(transcript_path)

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
