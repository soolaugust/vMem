from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_vmem_doctor_json_passes(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["MEMORY_OS_DIR"] = str(tmp_path / "memory-os")
    result = subprocess.run(
        [sys.executable, str(ROOT / "mcp_memory_lookup.py"), "doctor", "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["hooks"]["ok"] is True
    assert checks["retriever_pressure"]["ok"] is True
    assert checks["public_hygiene"]["ok"] is True
