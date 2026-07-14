from __future__ import annotations

import json
from pathlib import Path

from hooks import posttool_guard


def test_posttool_guard_critical_enters_working_set_without_manual_compact(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "memory-os"
    monkeypatch.setattr(posttool_guard, "MEMORY_OS_DIR", memory_dir)
    monkeypatch.setattr(posttool_guard, "STATE_FILE", memory_dir / "thrashing_state.json")
    monkeypatch.setattr(posttool_guard, "PROFILE_DB", memory_dir / "tool_profile.db")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(posttool_guard, "write_pressure_state", lambda level, reason: calls.append((level, reason)))
    monkeypatch.setattr(posttool_guard, "write_context_mode", lambda mode, reason: calls.append((mode, reason)))

    state = posttool_guard._load_state()
    notice = posttool_guard._run_thrashing(
        "Bash",
        {"command": "generate"},
        11 * 1024 * 1024,
        "s1",
        0,
        "",
        state,
        100.0,
    )

    assert notice is not None
    assert "working-set/reclaim" in notice
    assert "无需手动 /compact 或 /clear" in notice
    assert "请手动运行" not in notice
    assert "强烈建议立即 /clear" not in notice
    assert ("critical", "thrashing critical: window=11.0MB epoch=11.0MB tool=Bash") in calls
    assert ("working_set", "thrashing critical: window=11.0MB epoch=11.0MB tool=Bash") in calls
    assert state["epoch_bytes"] == 11 * 1024 * 1024


def test_posttool_guard_accounts_non_bash_read_tool_output(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "memory-os"
    monkeypatch.setattr(posttool_guard, "MEMORY_OS_DIR", memory_dir)
    monkeypatch.setattr(posttool_guard, "STATE_FILE", memory_dir / "thrashing_state.json")
    monkeypatch.setattr(posttool_guard, "PROFILE_DB", memory_dir / "tool_profile.db")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(posttool_guard, "write_pressure_state", lambda level, reason: calls.append((level, reason)))
    monkeypatch.setattr(posttool_guard, "write_context_mode", lambda mode, reason: calls.append((mode, reason)))

    state = posttool_guard._load_state()
    notice = posttool_guard._run_thrashing(
        "Agent",
        {"description": "large agent result"},
        len(("x" * (11 * 1024 * 1024)).encode("utf-8")),
        "s1",
        0,
        "",
        state,
        100.0,
    )

    assert notice is not None
    assert "working-set/reclaim" in notice
    assert state["epoch_bytes"] == 11 * 1024 * 1024
    assert any(call[0] == "critical" for call in calls)
    assert any(call[0] == "working_set" for call in calls)


def test_posttool_guard_suppresses_warning_inside_post_compact_grace(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "memory-os"
    monkeypatch.setattr(posttool_guard, "MEMORY_OS_DIR", memory_dir)
    monkeypatch.setattr(posttool_guard, "STATE_FILE", memory_dir / "thrashing_state.json")
    state = {
        "schema_version": 2,
        "session_id": "s1",
        "post_compact_grace_until_ts": 1000.0,
        "post_compact_grace_bytes": 5 * 1024 * 1024,
        "session_bytes": 0,
        "epoch_bytes": 0,
        "window_bytes_history": [],
    }

    notice = posttool_guard._run_thrashing(
        "Bash",
        {"command": "generate"},
        3 * 1024 * 1024,
        "s1",
        0,
        "",
        state,
        120.0,
    )

    assert notice is None
    assert state["epoch_bytes"] == 3 * 1024 * 1024


def test_posttool_guard_session_switch_resets_epoch_state(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "memory-os"
    monkeypatch.setattr(posttool_guard, "MEMORY_OS_DIR", memory_dir)
    monkeypatch.setattr(posttool_guard, "STATE_FILE", memory_dir / "thrashing_state.json")
    state = {
        "schema_version": 2,
        "session_id": "old",
        "session_bytes": 20 * 1024 * 1024,
        "epoch_bytes": 20 * 1024 * 1024,
        "window_bytes_history": [[1, 20 * 1024 * 1024]],
        "compact_count": 2,
    }

    posttool_guard._run_thrashing("Bash", {}, 1, "new", 0, "", state, 200.0)

    assert state["session_id"] == "new"
    assert state["compact_count"] == 2
    assert state["session_bytes"] == 1
    assert state["epoch_bytes"] == 1


def test_filesize_guard_message_uses_governance_not_manual_compact(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "hooks" / "filesize_guard.js"
    text = script.read_text(encoding="utf-8")
    assert "已进入无感 working-set/reclaim 治理" in text
    assert "Autocompact thrashing 风险极高" not in text
    assert "请手动运行 /compact" not in text
