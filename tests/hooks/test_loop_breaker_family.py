#!/usr/bin/env python3
"""Minimal regression tests for loop_breaker command family extraction."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "hooks" / "loop_breaker.py"
spec = importlib.util.spec_from_file_location("loop_breaker", SCRIPT)
assert spec and spec.loader
loop_breaker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loop_breaker)


def main() -> None:
    py_a = "python3 - <<'PY'\nprint('a')\nPY"
    py_b = "python3 - <<'PY'\nprint('b')\nPY"
    assert loop_breaker.extract_command_family(py_a) != loop_breaker.extract_command_family(py_b)
    assert loop_breaker.extract_command_family(py_a).startswith("python3 stdin code:")

    script_a = "python3 /tmp/autopilot.py"
    script_b = "python3 /tmp/check_registry.py"
    assert loop_breaker.extract_command_family(script_a) == "python3 script:autopilot.py"
    assert loop_breaker.extract_command_family(script_b) == "python3 script:check_registry.py"

    git_a = "git show origin/for-next:kernel/sched/fair.c"
    git_b = "git show origin/for-next:kernel/sched/core.c"
    assert loop_breaker.extract_command_family(git_a) != loop_breaker.extract_command_family(git_b)

    mm_a = "mm get 67e52e58c1234567890"
    mm_b = "mm get 88e52e58c1234567890"
    assert loop_breaker.extract_command_family(mm_a) == loop_breaker.extract_command_family(mm_b)

    print("✅ loop_breaker family tests passed")


if __name__ == "__main__":
    main()
