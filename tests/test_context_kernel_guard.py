from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "context_kernel_guard.py"


def run_guard(payload: dict, tmp_path: Path, max_output_bytes: int = 1024) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CONTEXT_KERNEL_DIR"] = str(tmp_path / "context-kernel")
    env["CONTEXT_KERNEL_MAX_OUTPUT_BYTES"] = str(max_output_bytes)
    env["CONTEXT_KERNEL_DEFAULT_READ_LIMIT_LINES"] = "2000"
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_blocks_large_default_read_slice_without_limit(tmp_path: Path) -> None:
    big = tmp_path / "big.py"
    big.write_text(("x" * 120 + "\n") * 2000, encoding="utf-8")

    result = run_guard({"tool_name": "Read", "tool_input": {"file_path": str(big)}}, tmp_path)

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "default Read slice" in payload["reason"]
    page_table = tmp_path / "context-kernel" / "page_table.jsonl"
    assert page_table.exists()
    assert str(big) in page_table.read_text(encoding="utf-8")


def test_allows_large_file_when_default_read_slice_is_small(tmp_path: Path) -> None:
    big = tmp_path / "sparse.log"
    big.write_text(("x\n" * 2000) + ("y" * 4096), encoding="utf-8")

    result = run_guard({"tool_name": "Read", "tool_input": {"file_path": str(big)}}, tmp_path, max_output_bytes=8192)

    assert result.returncode == 0
    assert result.stdout == ""


def test_blocks_large_read_slice(tmp_path: Path) -> None:
    big = tmp_path / "big.log"
    big.write_text("line\n" * 2000, encoding="utf-8")

    result = run_guard({"tool_name": "Read", "tool_input": {"file_path": str(big), "limit": 1000}}, tmp_path)

    assert result.returncode == 2
    assert "requested slice" in json.loads(result.stdout)["reason"]


def test_allows_small_read_slice(tmp_path: Path) -> None:
    big = tmp_path / "big.log"
    big.write_text("line\n" * 2000, encoding="utf-8")

    result = run_guard({"tool_name": "Read", "tool_input": {"file_path": str(big), "limit": 20}}, tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""


def test_supports_camel_case_tool_input(tmp_path: Path) -> None:
    big = tmp_path / "big.py"
    big.write_text(("x" * 120 + "\n") * 2000, encoding="utf-8")

    result = run_guard({"toolName": "Read", "toolInput": {"file_path": str(big)}}, tmp_path)

    assert result.returncode == 2
    assert "default Read slice" in json.loads(result.stdout)["reason"]


def test_clamps_grep_content_output(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Grep", "tool_input": {"pattern": "x", "output_mode": "content", "head_limit": 1000}}, tmp_path)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["updatedInput"]["head_limit"] == 80


def test_clamps_unbounded_grep_content_output(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Grep", "tool_input": {"pattern": "x", "output_mode": "content", "head_limit": 0}}, tmp_path)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["updatedInput"]["head_limit"] == 80


def test_blocks_unbounded_bash_scan(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "cat /var/log/syslog"}}, tmp_path)

    assert result.returncode == 2
    assert "unbounded" in json.loads(result.stdout)["reason"]


def test_blocks_unbounded_git_diff(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "git diff"}}, tmp_path)

    assert result.returncode == 2
    assert "potentially unbounded" in json.loads(result.stdout)["reason"]


def test_allows_bounded_git_diff_stat(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "git diff --stat"}}, tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""


def test_blocks_verbose_pytest_stream(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "pytest -vv -s"}}, tmp_path)

    assert result.returncode == 2
    assert "potentially unbounded" in json.loads(result.stdout)["reason"]


def test_blocks_unbounded_segment_even_with_later_bounded_marker(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "cat huge.log; echo --stat"}}, tmp_path)

    assert result.returncode == 2
    assert "cat huge.log" in json.loads(result.stdout)["reason"]


def test_allows_each_high_output_segment_when_bounded(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "git diff --stat; cat huge.log | head -n 20"}}, tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""


def test_blocks_stderr_only_redirect_bypass(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "cat huge.log 2>/tmp/err"}}, tmp_path)

    assert result.returncode == 2
    assert "cat huge.log" in json.loads(result.stdout)["reason"]


def test_blocks_tee_side_channel_before_head(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "cat huge.log | tee /dev/stderr | head -n 1"}}, tmp_path)

    assert result.returncode == 2
    assert "cat huge.log" in json.loads(result.stdout)["reason"]


def test_allows_stdout_redirected_scan(tmp_path: Path) -> None:
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": "cat huge.log >/tmp/out"}}, tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""


def test_blocks_tee_side_channel_even_with_stdout_redirect(tmp_path: Path) -> None:
    cmd = "cat huge.log | " + "tee /dev/stderr >/tmp/out"
    result = run_guard({"tool_name": "Bash", "tool_input": {"command": cmd}}, tmp_path)

    assert result.returncode == 2
    assert "cat huge.log" in json.loads(result.stdout)["reason"]
