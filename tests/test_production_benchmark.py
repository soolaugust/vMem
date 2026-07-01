from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_benchmark_report_is_readable(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    markdown = tmp_path / "report.md"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks" / "production" / "runner.py"),
            "--suite",
            "smoke",
            "--report",
            str(report),
            "--markdown",
            str(markdown),
            "--fail-under",
            "70",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["verdict"]["score"] >= 70
    assert payload["verdict"]["gates_failed"] == []
    names = {item["name"] for item in payload["checks"]}
    assert "context_hard_overflow_no_block" in names
    assert "critical_pressure_sheds_retriever" in names
    text = markdown.read_text(encoding="utf-8")
    assert "Value At A Glance" in text
    assert "API 400 prevention path" in text
    assert "Hard Gates" in text
    assert "production" in text.lower()
