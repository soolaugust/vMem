from __future__ import annotations

import json
from pathlib import Path

from hooks.transcript_reclaimer import reclaim_transcript


def _line(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def test_transcript_reclaimer_backs_up_and_bounds_active_file(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    lines = []
    for i in range(80):
        lines.append(_line({"type": "user", "message": {"role": "user", "content": f"question {i}"}}))
        lines.append(_line({"type": "assistant", "message": {"role": "assistant", "content": "x" * 4096}}))
    lines.append(_line({"type": "last-prompt", "lastPrompt": "keep me"}))
    transcript.write_bytes(b"".join(lines))
    original = transcript.stat().st_size

    result = reclaim_transcript(
        transcript,
        memory_dir=memory_dir,
        target_bytes=16_000,
        keep_tail_lines=20,
        max_line_bytes=2_000,
        reason="test hard pressure",
    )

    assert result.ok is True
    assert result.changed is True
    assert result.original_bytes == original
    assert transcript.stat().st_size <= 16_000
    assert Path(result.backup_path).exists()
    assert Path(result.manifest_path).exists()
    assert Path(result.backup_path).stat().st_size == original
    active = transcript.read_text(encoding="utf-8")
    assert "vMem-reclaim-header" in active
    assert "keep me" in active
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["original_bytes"] == original
    assert manifest["reclaimed_bytes"] <= 16_000


def test_transcript_reclaimer_dry_run_does_not_modify(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    transcript.write_bytes(_line({"type": "assistant", "message": {"role": "assistant", "content": "x" * 10_000}}))
    before = transcript.read_bytes()
    result = reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=1000, dry_run=True)
    assert result.changed is True
    assert transcript.read_bytes() == before
    assert not Path(result.backup_path).exists()
