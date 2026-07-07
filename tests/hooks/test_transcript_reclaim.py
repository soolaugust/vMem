from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

spec = importlib.util.spec_from_file_location("prompt_budget_guard", HOOKS_DIR / "prompt_budget_guard.py")
assert spec is not None and spec.loader is not None
prompt_budget_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prompt_budget_guard)
reclaim_transcript_context = prompt_budget_guard.reclaim_transcript_context


def test_reclaim_transcript_context_shrinks_large_tool_result_message(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "x" * 100_000},
                ]
            },
        })
        + "\n",
        encoding="utf-8",
    )
    before = transcript.stat().st_size

    result = reclaim_transcript_context(transcript)

    assert result["saved_bytes"] > 0
    assert transcript.stat().st_size < before
    text = transcript.read_text(encoding="utf-8")
    assert "transcript-reclaim truncated" in text


def test_reclaim_preserves_tail_appended_after_replace(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "attachment", "attachment": {"type": "hook_success", "hookName": "PostToolUse:Edit", "stdout": "x" * 100_000}})
        + "\n",
        encoding="utf-8",
    )
    original_replace = prompt_budget_guard.os.replace
    appended = {"done": False}

    def patched_replace(src: str | bytes | os.PathLike[str] | os.PathLike[bytes], dst: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        with transcript.open("ab") as live:
            live.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "late-tail"}]}}).encode() + b"\n")
        appended["done"] = True
        original_replace(src, dst)

    monkeypatch.setattr(prompt_budget_guard.os, "replace", patched_replace)

    reclaim_transcript_context(transcript)

    assert appended["done"] is True
    assert "late-tail" in transcript.read_text(encoding="utf-8")


def test_reclaim_preserves_tail_appended_during_stream(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "attachment", "attachment": {"type": "hook_success", "hookName": "PostToolUse:Edit", "stdout": "x" * 100_000}})
        + "\n",
        encoding="utf-8",
    )
    original_open = Path.open
    appended = {"done": False}

    def patched_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = original_open(self, *args, **kwargs)
        if self == transcript and args and args[0] == "rb" and not appended["done"]:
            with original_open(transcript, "ab") as live:
                live.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "tail"}]}}).encode() + b"\n")
            appended["done"] = True
        return handle

    monkeypatch.setattr(Path, "open", patched_open)

    reclaim_transcript_context(transcript)

    assert "tail" in transcript.read_text(encoding="utf-8")
