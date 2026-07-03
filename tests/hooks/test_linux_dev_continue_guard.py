import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


HOOK = Path("/home/mi/.claude/hooks/linux_dev_continue_guard.py")
STATE = Path("/home/mi/ssd/codes/claude-workspace/linux-dev/loop-engineering/active_state.json")


def test_continue_guard_supports_user_message_shape_under_pressure(tmp_path: Path) -> None:
    previous = STATE.read_text(encoding="utf-8") if STATE.exists() else None
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "status": "active",
        "next_step": {"kind": "sample", "instruction": "run next sample"},
    }), encoding="utf-8")
    (tmp_path / "context_pressure_state.json").write_text(json.dumps({
        "last_pressure_level": "critical",
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "reason": "test",
    }))
    try:
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"hookSpecificInput": {"userMessage": "继续刚才的任务"}}),
            text=True,
            capture_output=True,
            env={**os.environ, "MEMORY_OS_DIR": str(tmp_path)},
            timeout=5,
        )
    finally:
        if previous is None:
            STATE.unlink(missing_ok=True)
        else:
            STATE.write_text(previous, encoding="utf-8")

    assert result.returncode == 0
    assert "additionalContext" in result.stdout
    assert "context pressure is high" in result.stdout
    assert "next_step.kind=sample" in result.stdout
    assert "Do not answer with 'No response requested'" not in result.stdout
