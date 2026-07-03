#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hooks import thrashing_detector


def test_reset_compact_epoch_clears_incremental_pressure(tmp_path, monkeypatch):
    state_file = tmp_path / "thrashing_state.json"
    monkeypatch.setattr(thrashing_detector, "STATE_FILE", state_file)
    monkeypatch.setattr(thrashing_detector, "MEMORY_OS_DIR", tmp_path)

    state_file.write_text(json.dumps({
        "session_id": "s1",
        "session_bytes": 20 * 1024 * 1024,
        "window_bytes_history": [[1, 10 * 1024 * 1024]],
        "compact_count": 0,
    }), encoding="utf-8")

    state = thrashing_detector.reset_compact_epoch("s1", now_ts=100.0)

    assert state["session_bytes"] == 0
    assert state["window_bytes_history"] == []
    assert state["compact_count"] == 1
    assert state["post_compact_grace_until_ts"] > 100.0
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["last_epoch_bytes_before_compact"] == 20 * 1024 * 1024


def test_thrashing_detector_suppresses_warning_inside_post_compact_grace(tmp_path, monkeypatch, capsys):
    state_file = tmp_path / "thrashing_state.json"
    monkeypatch.setattr(thrashing_detector, "STATE_FILE", state_file)
    monkeypatch.setattr(thrashing_detector, "MEMORY_OS_DIR", tmp_path)
    monkeypatch.setattr(thrashing_detector, "PROFILE_DB", tmp_path / "tool_profile.db")

    thrashing_detector.reset_compact_epoch("s1", now_ts=100.0)
    monkeypatch.setattr(thrashing_detector.time, "time", lambda: 120.0)
    payload = {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "produce output"},
        "tool_response": "x" * (2 * 1024 * 1024),
    }
    monkeypatch.setattr(sys, "stdin", type("FakeStdin", (), {"read": lambda self: json.dumps(payload)})())
    monkeypatch.setattr(sys, "exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))

    try:
        thrashing_detector.main()
    except SystemExit as exc:
        assert exc.code == 0

    assert capsys.readouterr().out == ""


def test_filesize_guard_ignores_historical_pressure_during_compact_grace(tmp_path):
    memory_dir = tmp_path / ".claude" / "memory-os"
    memory_dir.mkdir(parents=True)
    (memory_dir / "thrashing_state.json").write_text(json.dumps({
        "schema_version": 2,
        "session_id": "s1",
        "post_compact_grace_until_ts": 4102444800,
        "post_compact_grace_bytes": 5 * 1024 * 1024,
        "session_bytes": 20 * 1024 * 1024,
        "epoch_bytes": 512 * 1024,
    }), encoding="utf-8")
    target = tmp_path / "medium.txt"
    target.write_text("x" * (30 * 1024), encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    payload = {
        "session_id": "s1",
        "tool_name": "Read",
        "tool_input": {"file_path": str(target)},
    }
    result = subprocess.run(
        ["node", str(ROOT / "hooks" / "filesize_guard.js")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert "session_guard" not in result.stderr
