from __future__ import annotations

import json
from pathlib import Path

from memory_os.runtime.context.kernel_compat import (
    build_working_set_manifest,
    extract_transcript_pages,
    iter_pages,
    manifest_context,
    page_fault,
)


def test_transcript_extractor_builds_threaded_pages_and_manifest(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "fix context 400"}]}})
        + "\n"
        + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "root cause is hook bloat"}]}})
        + "\n"
        + json.dumps({"type": "attachment", "attachment": {"type": "hook_success", "hookName": "PostToolUse:Edit", "stdout": "x" * 5000}})
        + "\n",
        encoding="utf-8",
    )

    result = extract_transcript_pages(transcript, root=tmp_path / "kernel", min_offload_chars=1000)

    assert result.pages_created >= 3
    pages = iter_pages(tmp_path / "kernel")
    assert {p.thread for p in pages} >= {"main", "tools"}
    assert any(p.page_type == "hook_payload" and not p.resident for p in pages)
    assert result.manifest.hot_pages
    text = manifest_context(result.manifest)
    assert "working_set_manifest" in text
    assert "cold_refs" in text


def test_page_fault_loads_swapped_evidence_range(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "attachment", "attachment": {"type": "hook_success", "hookName": "PostToolUse:Read", "stdout": "line1\nline2\nline3\n" + "x" * 5000}})
        + "\n",
        encoding="utf-8",
    )
    extract_transcript_pages(transcript, root=tmp_path / "kernel", min_offload_chars=100)
    page = next(p for p in iter_pages(tmp_path / "kernel") if p.page_type == "hook_payload")

    faulted = page_fault(page.page_id, offset=0, limit=2, root=tmp_path / "kernel")

    assert "line1" in faulted
    assert "line2" in faulted


def test_extraction_is_idempotent_for_same_transcript_lines(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "attachment", "attachment": {"type": "hook_success", "hookName": "PostToolUse:Edit", "stdout": "x" * 5000}})
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "kernel"

    first = extract_transcript_pages(transcript, root=root, min_offload_chars=100)
    second = extract_transcript_pages(transcript, root=root, min_offload_chars=100)

    assert first.pages_created == 1
    assert second.pages_created == 0
    assert len(iter_pages(root)) == 1


def test_tailed_extraction_uses_absolute_line_index(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    root = tmp_path / "kernel"
    transcript.write_text(
        "".join(
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": f"intent {idx}"}]}}) + "\n"
            for idx in range(5)
        ),
        encoding="utf-8",
    )

    first = extract_transcript_pages(transcript, root=root, min_offload_chars=1000, max_lines=3)
    with transcript.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "intent new"}]}}) + "\n")
    second = extract_transcript_pages(transcript, root=root, min_offload_chars=1000, max_lines=3)

    assert first.pages_created == 3
    assert second.pages_created == 1


def test_manifest_enforces_thread_budgets(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "".join(
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": f"intent {idx}"}]}}) + "\n"
            for idx in range(20)
        ),
        encoding="utf-8",
    )
    extract_transcript_pages(transcript, root=tmp_path / "kernel", min_offload_chars=1000)

    manifest = build_working_set_manifest(root=tmp_path / "kernel", budgets={"main": 200, "tools": 0, "evidence": 0, "code": 0, "memory": 0, "agents": 0, "governance": 0})

    assert manifest.usage["main"] <= 200
    assert len(manifest.cold_refs) > 0
