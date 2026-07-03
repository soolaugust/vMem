#!/usr/bin/env python3
"""Regression tests for Read PreToolUse input normalization."""
import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "hooks" / "filesize_guard.js"


def run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_read_empty_pages_removed_via_updated_input():
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {
            "file_path": "/tmp/example.txt",
            "limit": 10,
            "offset": 1,
            "pages": "",
        },
    }

    result = run_hook(payload)

    assert result.returncode == 0
    output = json.loads(result.stdout)
    hook_output = output["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "allow"
    assert hook_output["updatedInput"] == {
        "file_path": "/tmp/example.txt",
        "limit": 10,
        "offset": 1,
    }


def test_read_non_empty_pages_not_sanitized():
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {
            "file_path": "/tmp/example.txt",
            "pages": "1-2",
        },
    }

    result = run_hook(payload)

    assert result.returncode == 0
    assert result.stdout == ""
