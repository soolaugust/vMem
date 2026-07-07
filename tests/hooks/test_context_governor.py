#!/usr/bin/env python3
"""Regression tests for memory-os context_governor."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from context_governor import (  # noqa: E402
    command_name,
    enforce_additional_context,
    is_rescue_command,
    prompt_text,
    read_context_mode,
    read_pressure_state,
    record_context_oom,
    should_force_takeover,
    should_shed_optional_context,
    write_context_mode,
    write_pressure_state,
    write_rss_snapshot,
)


def write_state(path: Path, level: str, age_secs: int = 0) -> None:
    ts = datetime.fromtimestamp(time.time() - age_secs, tz=timezone.utc).isoformat()
    path.write_text(
        json.dumps({"last_pressure_level": level, "last_seen_at": ts}),
        encoding="utf-8",
    )


def assert_retrievers_consume_pressure_state() -> None:
    hooks_dir = Path(__file__).resolve().parents[2] / "hooks"
    fallback = (hooks_dir / "retriever.py").read_text(encoding="utf-8")
    daemon = (hooks_dir / "retriever_daemon.py").read_text(encoding="utf-8")
    assert "should_shed_optional_context" in fallback
    assert "enforce_additional_context" in fallback
    assert "if should_shed_optional_context(hook_input):" in fallback
    assert "from context_governor import should_shed_optional_context" in daemon
    assert "if should_shed_optional_context(hook_input):" in daemon
    assert daemon.index("if should_shed_optional_context(hook_input):") < daemon.index("# ── Stage 0: SKIP ──")


def assert_configured_additional_context_producers_governed() -> None:
    hooks_dir = Path(__file__).resolve().parents[2] / "hooks"
    config = json.loads((hooks_dir / "hooks.json").read_text(encoding="utf-8"))
    commands: set[str] = set()
    for groups in config.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                command = str(hook.get("command", ""))
                if command.endswith('.js"'):
                    continue
                if "/hooks/" not in command:
                    continue
                name = command.rsplit("/hooks/", 1)[-1].split('"', 1)[0]
                if name.endswith(".py"):
                    commands.add(name)
    for name in sorted(commands):
        source = (hooks_dir / name).read_text(encoding="utf-8")
        emits_json = "print(json.dumps({" in source or "sys.stdout.write(json.dumps({" in source
        if not emits_json or "hookSpecificOutput" not in source or "additionalContext" not in source:
            continue
        assert (
            "enforce_additional_context" in source
            or "emit_user_prompt_context" in source
            or "should_shed_optional_context" in source
        ), f"{name} emits additionalContext without context_governor"


def main() -> None:
    assert_retrievers_consume_pressure_state()
    assert_configured_additional_context_producers_governed()

    assert prompt_text({"prompt": "hello"}) == "hello"
    assert prompt_text({"hookSpecificInput": {"userMessage": "hi"}}) == "hi"
    assert command_name("  /clear now") == "/clear"
    assert is_rescue_command("/clear") is True
    assert is_rescue_command("/compact because large") is True
    assert is_rescue_command("continue work") is False

    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "context_pressure_state.json"
        mode_file = Path(tmp) / "context_mode_state.json"
        snapshot_file = Path(tmp) / "context_rss_snapshot.json"
        events_file = Path(tmp) / "context_oom_events.jsonl"

        assert read_pressure_state(state_file=state_file).active is False

        write_state(state_file, "low")
        assert read_pressure_state(state_file=state_file).active is False
        assert should_shed_optional_context({"prompt": "continue"}, state_file=state_file) is False

        write_state(state_file, "high")
        pressure = read_pressure_state(state_file=state_file)
        assert pressure.active is True
        assert pressure.level == "high"
        assert should_shed_optional_context({"prompt": "continue"}, state_file=state_file) is True
        assert should_shed_optional_context({"prompt": "/clear"}, state_file=state_file) is False
        assert should_shed_optional_context({"prompt": "/compact"}, state_file=state_file) is False

        write_pressure_state("critical", "test pressure", state_file=state_file)
        written_pressure = read_pressure_state(state_file=state_file)
        assert written_pressure.active is True
        assert written_pressure.level == "critical"
        assert should_shed_optional_context({"prompt": "continue"}, state_file=state_file) is True

        write_state(state_file, "critical", age_secs=1200)
        assert read_pressure_state(state_file=state_file, max_age_secs=600).active is False
        assert should_shed_optional_context(
            {"prompt": "continue"},
            state_file=state_file,
            max_age_secs=600,
        ) is False

        write_context_mode("working_set", "test", state_file=mode_file)
        assert read_context_mode(state_file=mode_file).mode == "working_set"

        optional = enforce_additional_context(
            {"prompt": "continue"},
            "optional memory",
            producer="test",
            hook_event_name="UserPromptSubmit",
        )
        # Uses default global state, so it should still emit outside global pressure.
        assert optional is None or "hookSpecificOutput" in optional

        mandatory = enforce_additional_context(
            {"prompt": "continue"},
            "x" * 2000,
            producer="test",
            hook_event_name="UserPromptSubmit",
            mandatory=True,
            max_chars=300,
        )
        assert mandatory is not None
        assert len(mandatory["hookSpecificOutput"]["additionalContext"]) <= 300

        snapshot = write_rss_snapshot(
            {"projected_context_chars": 1234, "total_context_hard_chars": 1000},
            prompt="hello",
            snapshot_file=snapshot_file,
        )
        assert snapshot["projected_context_chars"] == 1234
        event = record_context_oom(
            "trace-1",
            reason="test oom",
            snapshot=snapshot,
            events_file=events_file,
            mode_state_file=mode_file,
        )
        assert event["action"] == "entered_emergency"
        assert events_file.read_text(encoding="utf-8").strip()
        assert read_context_mode(state_file=mode_file).mode == "emergency"

    print("✅ context_governor tests passed")


def test_context_governor_regression() -> None:
    main()


if __name__ == "__main__":
    main()
