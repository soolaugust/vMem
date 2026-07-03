import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
HOOK = HOOKS_DIR / "parallel_hint.py"


def _run(prompt: str, memory_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt}),
        text=True,
        capture_output=True,
        env={**os.environ, "MEMORY_OS_DIR": str(memory_dir)},
        timeout=5,
    )


def test_parallel_hint_injects_when_signals_match(tmp_path: Path) -> None:
    prompt = "请同时分析 A、B、C，分别给出结论，并对比 A 和 B"

    result = _run(prompt, tmp_path)

    assert result.returncode == 0
    assert "additionalContext" in result.stdout
    assert "[CFS]" in result.stdout


def test_parallel_hint_sheds_under_critical_pressure(tmp_path: Path) -> None:
    (tmp_path / "context_pressure_state.json").write_text(json.dumps({
        "last_pressure_level": "critical",
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "reason": "test",
    }))
    prompt = "请同时分析 A、B、C，分别给出结论，并对比 A 和 B"

    result = _run(prompt, tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""


def test_parallel_hint_skips_continue_prompt_payload_shape(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hookSpecificInput": {"userMessage": "继续刚才的任务"}}),
        text=True,
        capture_output=True,
        env={**os.environ, "MEMORY_OS_DIR": str(tmp_path)},
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == ""
