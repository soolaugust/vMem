from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_prompt_guard(memory_dir: Path, transcript: Path) -> dict:
    env = os.environ.copy()
    env.update({
        "MEMORY_OS_DIR": str(memory_dir),
        "HARNESS_HEARTBEAT_DIR": str(memory_dir),
        "MEMORY_OS_PROMPT_CHAR_BUDGET": "1000",
        "MEMORY_OS_TOTAL_CONTEXT_WARN_CHARS": "100",
        "MEMORY_OS_TOTAL_CONTEXT_HARD_CHARS": "200",
        "MEMORY_OS_STATIC_CONTEXT_RESERVE_CHARS": "100",
        "MEMORY_OS_DOWNSTREAM_CONTEXT_RESERVE_CHARS": "1",
    })
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "prompt_budget_guard.py")],
        input=json.dumps({"prompt": "continue", "transcript_path": str(transcript)}),
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_hard_context_overflow_enters_working_set_and_sheds_retriever(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory-os"
    memory_dir.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"message": {"content": [{"type": "text", "text": "t" * 2000}]}}) + "\n",
        encoding="utf-8",
    )

    payload = _run_prompt_guard(memory_dir, transcript)
    assert payload["decision"] == "approve"
    assert "working-set" in payload["reason"]
    assert "hookSpecificOutput" in payload
    assert len(payload["hookSpecificOutput"]["additionalContext"]) <= 1200
    pressure = json.loads((memory_dir / "context_pressure_state.json").read_text(encoding="utf-8"))
    assert pressure["last_pressure_level"] == "critical"
    mode = json.loads((memory_dir / "context_mode_state.json").read_text(encoding="utf-8"))
    assert mode["mode"] == "working_set"
    working_set = memory_dir / "working_set" / "current.json"
    assert working_set.exists()

    env = os.environ.copy()
    env["MEMORY_OS_DIR"] = str(memory_dir)
    fallback = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "retriever.py")],
        input=json.dumps({"prompt": "need architecture context"}),
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert fallback.returncode == 0, fallback.stdout + fallback.stderr
    assert fallback.stdout == ""

    daemon_text = (ROOT / "hooks" / "retriever_daemon.py").read_text(encoding="utf-8")
    assert "if should_shed_optional_context(hook_input):" in daemon_text
    assert daemon_text.index("if should_shed_optional_context(hook_input):") < daemon_text.index("# ── Stage 0: SKIP ──")
