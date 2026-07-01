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


def test_vmem_install_repairs_settings(tmp_path: Path) -> None:
    env = os.environ.copy()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "mcp_memory_lookup.py"), "install", "--settings", str(settings), "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["changed"] is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    first_hook = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert "prompt_budget_guard.py" in first_hook["command"]
    assert first_hook["async"] is False

    second = subprocess.run(
        [sys.executable, str(ROOT / "mcp_memory_lookup.py"), "repair", "--settings", str(settings), "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["changed"] is False
