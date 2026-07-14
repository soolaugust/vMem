#!/usr/bin/env python3
"""Stop hook output working-set governor.

Assistant output can exceed the client/model output cap before any Stop hook can
intervene.  This hook governs the aftermath: it keeps the active transcript
small, preserves full output as evidence pages, and writes a bounded state that
next UserPromptSubmit can surface without recommending larger output caps.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MEMORY_OS_DIR = Path(os.environ.get("MEMORY_OS_DIR", Path.home() / ".claude" / "memory-os")).expanduser()
STATE_FILE = MEMORY_OS_DIR / "output_working_set_state.json"
DEFAULT_MAX_ACTIVE_CHARS = int(os.environ.get("MEMORY_OS_OUTPUT_MAX_ACTIVE_CHARS", "24000"))
DEFAULT_PAGE_CHARS = int(os.environ.get("MEMORY_OS_OUTPUT_PAGE_CHARS", "12000"))
PREVIEW_CHARS = int(os.environ.get("MEMORY_OS_OUTPUT_PREVIEW_CHARS", "4000"))
INCOMPLETE_SUFFIXES = (":", "：", "、", ",", "，", "and", "or", "以及", "然后")


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _transcript_path(data: dict[str, Any]) -> Path | None:
    for key in ("transcript_path", "transcriptPath", "transcript"):
        value = data.get(key)
        if isinstance(value, str) and value:
            path = Path(value).expanduser()
            if path.exists():
                return path
    env_path = os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
    if env_path:
        path = Path(env_path).expanduser()
        if path.exists():
            return path
    return None


def _assistant_text(entry: dict[str, Any]) -> str:
    if entry.get("type") != "assistant":
        return ""
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _replace_assistant_text(entry: dict[str, Any], text: str) -> dict[str, Any]:
    message = entry.setdefault("message", {})
    if not isinstance(message, dict):
        return entry
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = text
    elif isinstance(content, list):
        replaced = False
        new_content = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                if not replaced:
                    clone = dict(item)
                    clone["text"] = text
                    new_content.append(clone)
                    replaced = True
                continue
            new_content.append(item)
        if not replaced:
            new_content.append({"type": "text", "text": text})
        message["content"] = new_content
    else:
        message["content"] = [{"type": "text", "text": text}]
    return entry


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    return text[:head] + "\n...[vMem output page cache; use evidence/page refs for full response]...\n" + text[-tail:]


def _pages(text: str, page_chars: int) -> list[dict[str, Any]]:
    pages = []
    start = 0
    page = 1
    while start < len(text):
        end = min(len(text), start + page_chars)
        pages.append({"page": page, "start_char": start, "end_char": end, "chars": end - start})
        start = end
        page += 1
    return pages


def _is_probably_incomplete(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped.count("```") % 2 == 1:
        return True
    if stripped.count("{") > stripped.count("}"):
        return True
    if stripped.count("[") > stripped.count("]"):
        return True
    tail = stripped[-40:].lower()
    return any(tail.endswith(suffix) for suffix in INCOMPLETE_SUFFIXES)


def _claim_paths(transcript: Path) -> tuple[Path, Path]:
    evidence_dir = MEMORY_OS_DIR / "evidence" / "assistant_outputs"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(1000):
        stamp = f"{time.time_ns()}-{os.getpid()}-{attempt}"
        text_path = evidence_dir / f"{transcript.stem}.assistant-output-{stamp}.txt"
        manifest_path = evidence_dir / f"{transcript.stem}.assistant-output-{stamp}.json"
        if not text_path.exists() and not manifest_path.exists():
            return text_path, manifest_path
    raise RuntimeError("could not allocate unique assistant output evidence paths")


def _write_state(state: dict[str, Any]) -> None:
    MEMORY_OS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def govern_transcript(transcript: Path, *, max_active_chars: int = DEFAULT_MAX_ACTIVE_CHARS, page_chars: int = DEFAULT_PAGE_CHARS) -> dict[str, Any]:
    lines = transcript.read_bytes().splitlines(keepends=True)
    assistant_index = -1
    assistant_entry: dict[str, Any] | None = None
    assistant_text = ""
    for index in range(len(lines) - 1, -1, -1):
        try:
            entry = json.loads(lines[index].decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        text = _assistant_text(entry)
        if text:
            assistant_index = index
            assistant_entry = entry
            assistant_text = text
            break

    if assistant_index < 0 or assistant_entry is None:
        try:
            STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True, "changed": False, "reason": "no assistant output found"}

    incomplete = _is_probably_incomplete(assistant_text)
    if len(assistant_text) <= max_active_chars and not incomplete:
        try:
            STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True, "changed": False, "reason": "assistant output within working-set budget", "chars": len(assistant_text)}

    text_path, manifest_path = _claim_paths(transcript)
    text_path.write_text(assistant_text, encoding="utf-8")
    page_manifest = _pages(assistant_text, page_chars)
    manifest = {
        "created_at_epoch": time.time(),
        "transcript_path": str(transcript),
        "evidence_path": str(text_path),
        "chars": len(assistant_text),
        "max_active_chars": max_active_chars,
        "page_chars": page_chars,
        "pages": page_manifest,
        "incomplete": incomplete,
        "policy": "output working-set governor: active transcript keeps digest and page refs; captured assistant output lives in evidence",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    evidence_note = (
        "Captured assistant output was moved to evidence pages; active transcript keeps only this digest."
        if not incomplete else
        "Captured assistant output was paged to evidence; the original response may be incomplete and missing tail content must be regenerated by a focused follow-up."
    )
    digest = (
        "[vMem output working-set]\n"
        f"Assistant output chars={len(assistant_text)} exceeded active budget={max_active_chars}"
        f" or looked incomplete={incomplete}.\n"
        f"{evidence_note}\n"
        f"evidence={text_path}\nmanifest={manifest_path}\n"
        f"pages={len(page_manifest)} page_chars={page_chars}\n\n"
        + _preview(assistant_text)
    )
    assistant_entry = _replace_assistant_text(assistant_entry, digest)
    assistant_entry["_output_working_set"] = {
        "reclaimed": True,
        "original_chars": len(assistant_text),
        "evidence": str(text_path),
        "manifest": str(manifest_path),
        "pages": len(page_manifest),
        "incomplete": incomplete,
    }
    lines[assistant_index] = (json.dumps(assistant_entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    tmp_path = transcript.with_suffix(transcript.suffix + f".output-governor-{os.getpid()}.tmp")
    tmp_path.write_bytes(b"".join(lines))
    tmp_path.replace(transcript)

    state = {
        "schema_version": 1,
        "created_at_epoch": time.time(),
        "status": "paged" if len(assistant_text) > max_active_chars else "incomplete",
        "transcript_path": str(transcript),
        "evidence_path": str(text_path),
        "manifest_path": str(manifest_path),
        "chars": len(assistant_text),
        "active_digest_chars": len(digest),
        "pages": len(page_manifest),
        "page_chars": page_chars,
        "incomplete": incomplete,
        "notice": (
            "上一条 assistant 输出已按 output working-set 策略分页/压缩；evidence/page refs 保存的是已捕获输出。"
            "若 incomplete=true，缺失尾部需要用聚焦 follow-up 重新生成；不要把调大 CLAUDE_CODE_MAX_OUTPUT_TOKENS 当默认方案。"
        ),
    }
    _write_state(state)
    return {"ok": True, "changed": True, **state}


def main() -> int:
    data = _read_input()
    transcript = _transcript_path(data)
    if transcript is None:
        return 0
    try:
        result = govern_transcript(transcript)
        if result.get("changed"):
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
