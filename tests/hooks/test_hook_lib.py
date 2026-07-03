import json
import subprocess
import sys
from pathlib import Path


HOOKS = Path(__file__).resolve().parents[2] / "hooks"


def test_prompt_io_reads_hook_specific_shape() -> None:
    script = "from lib.prompt_io import read_hook_input; import json; print(json.dumps(read_hook_input()))"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(HOOKS),
        input=json.dumps({"hookSpecificInput": {"userMessage": "hello"}}),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificInput"]["userMessage"] == "hello"


def test_large_hook_entrypoints_compile() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", "retriever.py", "extractor.py"],
        cwd=str(HOOKS),
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
