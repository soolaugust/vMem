from __future__ import annotations

import json
from pathlib import Path

from hooks.transcript_reclaimer import _append_bounded_tail_bytes, reclaim_transcript


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


def test_transcript_reclaimer_preserves_tail_appended_during_reclaim(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    transcript.write_bytes(
        b"".join(_line({"type": "assistant", "message": {"role": "assistant", "content": "x" * 4096}}) for _ in range(60))
    )
    appended = _line({"type": "user", "message": {"role": "user", "content": "new tail prompt"}})
    original_write_bytes = Path.write_bytes

    def append_before_tmp_write(self: Path, data: bytes) -> int:
        if self.name.endswith(".reclaim-tmp"):
            with transcript.open("ab") as handle:
                handle.write(appended)
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", append_before_tmp_write)
    reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=12_000, keep_tail_lines=10, max_line_bytes=2_000)

    assert transcript.stat().st_size <= 12_000
    assert "new tail prompt" in transcript.read_text(encoding="utf-8")


def test_transcript_reclaimer_preserves_tail_appended_in_replace_window(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    transcript.write_bytes(
        b"".join(_line({"type": "assistant", "message": {"role": "assistant", "content": "x" * 4096}}) for _ in range(60))
    )
    appended = json.dumps({"type": "user", "message": {"role": "user", "content": "late-start " + "q" * 50_000 + " late-end"}}, ensure_ascii=False).encode("utf-8")
    original_replace = Path.replace

    def append_before_replace(self: Path, target: Path) -> Path:
        if self.name.endswith(".reclaim-tmp"):
            with transcript.open("ab") as handle:
                handle.write(appended)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", append_before_replace)
    result = reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=12_000, keep_tail_lines=10, max_line_bytes=2_000)

    active = transcript.read_text(encoding="utf-8")
    backup_bytes = Path(result.backup_path).read_bytes()
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    active_indices = []
    active_original_bytes = {}
    active_summary_count = 0
    active_kept_count = 0
    for line in active.splitlines():
        record = json.loads(line)
        if record.get("type") == "vMem-reclaim-header":
            continue
        if "original_line_index" in record:
            active_indices.append(record["original_line_index"])
            active_original_bytes[record["original_line_index"]] = record["original_bytes"]
            active_summary_count += 1
        else:
            active_kept_count += 1
    assert transcript.stat().st_size <= 12_000
    assert "late-start" in active
    assert "late-end" in active
    assert appended in backup_bytes
    assert active_original_bytes[max(active_original_bytes)] == len(appended)
    assert [entry["line"] for entry in manifest["summaries"]] == active_indices
    assert {entry["line"]: entry["bytes"] for entry in manifest["summaries"]} == active_original_bytes
    assert manifest["summarized_lines"] == active_summary_count == result.summarized_lines
    assert manifest["kept_lines"] == active_kept_count == result.kept_lines
    assert manifest["concurrent_tail_accepted_summarized_lines"] >= 1
    assert "concurrent_tail_evicted_lines" not in manifest
    assert "lines_evicted_to_admit_concurrent_tail" in manifest
    assert result.summarized_lines > 0


def test_transcript_reclaimer_bounds_oversized_concurrent_tail(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    transcript.write_bytes(
        b"".join(_line({"type": "assistant", "message": {"role": "assistant", "content": "x" * 4096}}) for _ in range(60))
    )
    appended = json.dumps({"type": "user", "message": {"role": "user", "content": "tail-start " + "y" * 50_000 + " tail-end"}}, ensure_ascii=False).encode("utf-8")
    original_write_bytes = Path.write_bytes
    appended_once = False

    def append_before_tmp_write(self: Path, data: bytes) -> int:
        nonlocal appended_once
        if self.name.endswith(".reclaim-tmp") and not appended_once:
            appended_once = True
            with transcript.open("ab") as handle:
                handle.write(appended)
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", append_before_tmp_write)
    result = reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=26_000, keep_tail_lines=10, max_line_bytes=2_000)

    active = transcript.read_text(encoding="utf-8")
    backup = Path(result.backup_path).read_text(encoding="utf-8")
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    active_indices = []
    active_original_bytes = {}
    active_summary_count = 0
    active_kept_count = 0
    for line in active.splitlines():
        record = json.loads(line)
        if record.get("type") == "vMem-reclaim-header":
            continue
        if "original_line_index" in record:
            active_indices.append(record["original_line_index"])
            active_original_bytes[record["original_line_index"]] = record["original_bytes"]
            active_summary_count += 1
        else:
            active_kept_count += 1
    assert transcript.stat().st_size <= 26_000
    assert "tail-start" in active
    assert "tail-end" in active
    assert "tail-start" in backup
    assert "tail-end" in backup
    assert [entry["line"] for entry in manifest["summaries"]] == active_indices
    assert {entry["line"]: entry["bytes"] for entry in manifest["summaries"]} == active_original_bytes
    assert active_original_bytes[max(active_original_bytes)] == len(appended)
    assert manifest["summarized_lines"] == active_summary_count == result.summarized_lines
    assert manifest["kept_lines"] == active_kept_count == result.kept_lines
    assert manifest["concurrent_tail_accepted_summarized_lines"] >= 1
    assert "concurrent_tail_evicted_lines" not in manifest
    assert manifest["lines_evicted_to_admit_concurrent_tail"] >= 0
    assert manifest["kept_lines"] + manifest["summarized_lines"] + manifest["dropped_lines"] == 61
    assert result.kept_lines + result.summarized_lines + result.dropped_lines == 61


def test_transcript_reclaimer_uses_unique_evidence_paths(tmp_path: Path, monkeypatch) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    transcript.write_bytes(_line({"type": "assistant", "message": {"role": "assistant", "content": "x" * 10_000}}))
    monkeypatch.setattr("hooks.transcript_reclaimer.time.time_ns", lambda: 123)
    monkeypatch.setattr("hooks.transcript_reclaimer.os.getpid", lambda: 456)

    first = reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=1000)
    transcript.write_bytes(_line({"type": "assistant", "message": {"role": "assistant", "content": "y" * 10_000}}))
    second = reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=1000)

    assert first.backup_path != second.backup_path
    assert Path(first.backup_path).exists()
    assert Path(second.backup_path).exists()


def test_transcript_reclaimer_recent_large_user_line_keeps_bounded_preview(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    content = "important-start " + ("x" * 50_000) + " important-end"
    transcript.write_bytes(
        b"".join(_line({"type": "assistant", "message": {"role": "assistant", "content": "old" * 2000}}) for _ in range(20))
        + _line({"type": "user", "message": {"role": "user", "content": content}})
    )

    result = reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=20_000, keep_tail_lines=5, max_line_bytes=2_000)

    active = transcript.read_text(encoding="utf-8")
    assert result.changed is True
    assert "vMem-reclaimed-recent-line" in active
    assert "important-start" in active
    assert "important-end" in active


def test_transcript_reclaimer_fallback_uses_original_line_indices(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    transcript.write_bytes(
        b"".join(_line({"type": "assistant", "message": {"role": "assistant", "content": f"line-{i}-" + "x" * 4096}}) for i in range(8))
    )

    result = reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=2_500, keep_tail_lines=8, max_line_bytes=100)

    indices = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if "original_line_index" in record:
            indices.append(record["original_line_index"])
    assert indices == sorted(indices)
    assert all(0 <= index < 8 for index in indices)
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert [entry["line"] for entry in manifest["summaries"]] == indices


def test_transcript_reclaimer_manifest_matches_active_after_tail_reserve_eviction(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    memory_dir = tmp_path / "memory-os"
    transcript.write_bytes(
        b"".join(_line({"type": "assistant", "message": {"role": "assistant", "content": f"line-{i}-" + "x" * 4096}}) for i in range(8))
    )

    result = reclaim_transcript(transcript, memory_dir=memory_dir, target_bytes=10_000, keep_tail_lines=8, max_line_bytes=100)

    active_indices = []
    active_original_bytes = {}
    for line in transcript.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if "original_line_index" in record:
            active_indices.append(record["original_line_index"])
            active_original_bytes[record["original_line_index"]] = record["original_bytes"]
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert [entry["line"] for entry in manifest["summaries"]] == active_indices
    assert {entry["line"]: entry["bytes"] for entry in manifest["summaries"]} == active_original_bytes


def test_append_bounded_tail_accounts_for_separator_byte_when_fitting() -> None:
    header = b'{"type":"vMem-reclaim-header"}\n'
    output = [header, b'{"type":"partial"}']
    tail = b'{"type":"tail"}'

    result, kept, summarized, dropped, evicted = _append_bounded_tail_bytes(
        output,
        tail,
        Path("/tmp/evidence.jsonl"),
        target_bytes=sum(len(part) for part in output) + 1 + len(tail) + 1,
        max_line_bytes=1024,
        start_index=0,
    )

    assert b"".join(result).endswith(b'{"type":"partial"}\n{"type":"tail"}\n')
    assert (kept, summarized, dropped, evicted) == (1, 0, 0, 0)


def test_append_bounded_tail_evicts_for_separator_byte() -> None:
    header = b'{"type":"vMem-reclaim-header"}\n'
    output = [header, b'{"type":"partial"}']
    tail = b'{"type":"tail"}'

    result, kept, summarized, dropped, evicted = _append_bounded_tail_bytes(
        output,
        tail,
        Path("/tmp/evidence.jsonl"),
        target_bytes=len(header) + len(tail) + 1,
        max_line_bytes=1024,
        start_index=0,
    )

    assert b"".join(result) == header + tail + b"\n"
    assert (kept, summarized, dropped, evicted) == (1, 0, 0, 1)


def test_append_bounded_tail_drops_when_separator_does_not_fit_without_eviction() -> None:
    output = [b'{"type":"partial"}']
    tail = b'{"type":"tail"}'

    result, kept, summarized, dropped, evicted = _append_bounded_tail_bytes(
        output,
        tail,
        Path("/tmp/evidence.jsonl"),
        target_bytes=sum(len(part) for part in output) + len(tail),
        max_line_bytes=1024,
        start_index=0,
        evict_existing=False,
    )

    assert result == [b'{"type":"partial"}']
    assert (kept, summarized, dropped, evicted) == (0, 0, 1, 0)


def test_append_bounded_tail_no_newline_boundary_records_original_bytes() -> None:
    prefix = b'{"message":{"content":"'
    suffix = b'"}}'
    raw = prefix + (b"x" * 32) + suffix
    max_line_bytes = len(raw)

    result, kept, summarized, dropped, evicted = _append_bounded_tail_bytes(
        [b'{"type":"vMem-reclaim-header"}\n'],
        raw,
        Path("/tmp/evidence.jsonl"),
        target_bytes=4096,
        max_line_bytes=max_line_bytes,
        start_index=3,
    )

    record = json.loads(result[-1])
    assert (kept, summarized, dropped, evicted) == (0, 1, 0, 0)
    assert record["original_line_index"] == 3
    assert record["original_bytes"] == len(raw)
