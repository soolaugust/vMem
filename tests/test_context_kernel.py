from __future__ import annotations

from pathlib import Path

from memory_os.runtime.context.kernel_compat import admit_bash, admit_grep, admit_read, iter_pages, update_cgroup_usage


def test_context_kernel_records_page_and_cgroup_for_large_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_KERNEL_DIR", str(tmp_path / "kernel"))
    monkeypatch.setenv("CONTEXT_KERNEL_MAX_OUTPUT_BYTES", "1024")
    big = tmp_path / "big.py"
    big.write_text(("x" * 120 + "\n") * 2000, encoding="utf-8")

    result = admit_read({"file_path": str(big)})

    assert result.decision == "block"
    pages = iter_pages(tmp_path / "kernel")
    assert len(pages) == 1
    assert pages[0].source == "Read"
    assert pages[0].cgroup == "tool_output"
    usage = update_cgroup_usage(tmp_path / "kernel")
    assert usage["tool_output"].swapped_bytes > 0


def test_context_kernel_grep_admission_updates_unbounded_content() -> None:
    result = admit_grep({"pattern": "x", "output_mode": "content", "head_limit": 0})

    assert result.decision == "update"
    assert result.updated_input["head_limit"] == 80


def test_context_kernel_bash_blocks_tee_side_channel() -> None:
    result = admit_bash({"command": "cat huge.log | tee /dev/stderr >/tmp/out"})

    assert result.decision == "block"
    assert "cat huge.log" in result.reason


def test_context_kernel_bash_allows_bounded_segments() -> None:
    result = admit_bash({"command": "git diff --stat; cat huge.log | head -n 20"})

    assert result.decision == "allow"
