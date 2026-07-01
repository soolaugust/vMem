#!/usr/bin/env python3
"""Regression tests for memory-os context_governor."""
from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from context_governor import (
    command_name,
    is_rescue_command,
    prompt_text,
    read_pressure_state,
    should_shed_optional_context,
    write_pressure_state,
)


def write_state(path: Path, level: str, age_secs: int = 0) -> None:
    ts = datetime.fromtimestamp(time.time() - age_secs, tz=timezone.utc).isoformat()
    path.write_text(
        json.dumps({"last_pressure_level": level, "last_seen_at": ts}),
        encoding="utf-8",
    )


def main() -> None:
    assert prompt_text({"prompt": "hello"}) == "hello"
    assert prompt_text({"hookSpecificInput": {"userMessage": "hi"}}) == "hi"
    assert command_name("  /clear now") == "/clear"
    assert is_rescue_command("/clear") is True
    assert is_rescue_command("/compact because large") is True
    assert is_rescue_command("continue work") is False

    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "context_pressure_state.json"

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

    print("✅ context_governor tests passed")


if __name__ == "__main__":
    main()
