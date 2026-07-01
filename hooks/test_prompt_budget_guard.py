#!/usr/bin/env python3
"""prompt_budget_guard.py 的最小回归测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("prompt_budget_guard.py")
HOOKS_JSON = Path(__file__).with_name("hooks.json")


def run_guard(
    prompt: str,
    budget: int,
    heartbeat_dir: Path,
    transcript_path: Path | None = None,
    total_budget: int = 1000,
    warn_budget: int | None = None,
    static_reserve: int = 100,
    downstream_reserve: int | None = None,
    session_id: str | None = None,
    state_file: Path | None = None,
    compact_marker_scan_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MEMORY_OS_PROMPT_CHAR_BUDGET"] = str(budget)
    env["MEMORY_OS_TOTAL_CONTEXT_CHAR_BUDGET"] = str(total_budget)
    if warn_budget is not None:
        env["MEMORY_OS_TOTAL_CONTEXT_WARN_CHARS"] = str(warn_budget)
    else:
        env.pop("MEMORY_OS_TOTAL_CONTEXT_WARN_CHARS", None)
    env["MEMORY_OS_STATIC_CONTEXT_RESERVE_CHARS"] = str(static_reserve)
    env.pop("MEMORY_OS_DOWNSTREAM_CONTEXT_RESERVE_CHARS", None)
    if downstream_reserve is not None:
        env["MEMORY_OS_DOWNSTREAM_CONTEXT_RESERVE_CHARS"] = str(downstream_reserve)
    env.pop("MEMORY_OS_COMPACT_MARKER_SCAN_BYTES", None)
    if compact_marker_scan_bytes is not None:
        env["MEMORY_OS_COMPACT_MARKER_SCAN_BYTES"] = str(compact_marker_scan_bytes)
    env["HARNESS_HEARTBEAT_DIR"] = str(heartbeat_dir)
    env["MEMORY_OS_DIR"] = str(heartbeat_dir / "memory-os")
    if state_file is not None:
        env["HOME"] = str(state_file.parent.parent.parent)
    payload = {"prompt": prompt}
    if session_id is not None:
        payload["session_id"] = session_id
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def write_transcript(path: Path, text: str) -> None:
    path.write_text(
        json.dumps({"message": {"content": [{"type": "text", "text": text}]}}) + "\n",
        encoding="utf-8",
    )


def write_compacted_transcript(path: Path, before: str, after: str) -> None:
    path.write_text(
        json.dumps({"message": {"content": [{"type": "text", "text": before}]}})
        + "\n"
        + json.dumps({"attachment": {"type": "hook_success", "hookEvent": "SessionStart", "hookName": "SessionStart:compact"}})
        + "\n"
        + json.dumps({"message": {"content": [{"type": "text", "text": after}]}})
        + "\n",
        encoding="utf-8",
    )


def write_stdout_compacted_transcript(path: Path, before: str, after: str) -> None:
    path.write_text(
        json.dumps({"message": {"content": [{"type": "text", "text": before}]}})
        + "\n"
        + json.dumps({
            "attachment": {
                "type": "hook_success",
                "hookName": "SessionStart:compact",
                "hookEvent": "SessionStart",
                "stdout": json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart:compact"}}),
            }
        })
        + "\n"
        + json.dumps({"message": {"content": [{"type": "text", "text": after}]}})
        + "\n",
        encoding="utf-8",
    )


def write_marker_transcript(path: Path, marker: str | dict[str, object]) -> None:
    marker_line = marker if isinstance(marker, str) else json.dumps(marker)
    path.write_text(
        json.dumps({"message": {"content": [{"type": "text", "text": "h" * 5000}]}})
        + "\n"
        + marker_line
        + "\n"
        + json.dumps({"message": {"content": [{"type": "text", "text": "fresh"}]}})
        + "\n",
        encoding="utf-8",
    )


def assert_prompt_budget_guard_wired_first() -> None:
    config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    user_prompt_submit = config["hooks"]["UserPromptSubmit"]
    commands = [
        hook.get("command", "")
        for entry in user_prompt_submit
        for hook in entry.get("hooks", [])
    ]
    guard_index = next(i for i, command in enumerate(commands) if "prompt_budget_guard.py" in command)
    retriever_index = next(i for i, command in enumerate(commands) if "retriever_wrapper.sh" in command)
    assert guard_index == 0
    assert guard_index < retriever_index
    assert user_prompt_submit[0]["hooks"][0]["async"] is False


def main() -> None:
    assert_prompt_budget_guard_wired_first()

    with tempfile.TemporaryDirectory() as tmp:
        heartbeat_dir = Path(tmp)
        transcript = heartbeat_dir / "transcript.jsonl"

        allowed = run_guard("hello", 10, heartbeat_dir, total_budget=50_000)
        assert allowed.returncode == 0, allowed.stdout + allowed.stderr
        heartbeat_path = heartbeat_dir / "prompt_budget_guard.last_run.json"
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        assert heartbeat["explicit_ok"] is True
        assert "within budget" in heartbeat["summary"]

        oversized_prompt = run_guard("x" * 11, 10, heartbeat_dir, total_budget=50_000)
        assert oversized_prompt.returncode == 0, oversized_prompt.stdout + oversized_prompt.stderr
        payload = json.loads(oversized_prompt.stdout)
        assert payload["decision"] == "approve"
        assert "exceeds local budget" in payload["reason"]
        assert payload["detail"]["prompt_chars"] == 11
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        assert heartbeat["explicit_ok"] is True
        assert "warn prompt" in heartbeat["summary"]
        pressure_state = json.loads((heartbeat_dir / "memory-os" / "context_pressure_state.json").read_text(encoding="utf-8"))
        assert pressure_state["last_pressure_level"] == "high"

        write_transcript(transcript, "t" * 950)
        oversized_context = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=transcript,
            total_budget=41_000,
            static_reserve=100,
        )
        assert oversized_context.returncode == 0, oversized_context.stdout + oversized_context.stderr
        payload = json.loads(oversized_context.stdout)
        assert payload["decision"] == "approve"
        assert "projected request context" in payload["reason"]
        assert "critical pressure" in payload["reason"]
        assert payload["detail"]["transcript_chars"] >= 950
        assert payload["detail"]["projected_context_chars"] >= 1052
        pressure_state = json.loads((heartbeat_dir / "memory-os" / "context_pressure_state.json").read_text(encoding="utf-8"))
        assert pressure_state["last_pressure_level"] == "critical"

        slash_context_allowed = run_guard(
            "/clear",
            1,
            heartbeat_dir,
            transcript_path=transcript,
            total_budget=41_000,
            static_reserve=100,
        )
        assert slash_context_allowed.returncode == 0, slash_context_allowed.stdout + slash_context_allowed.stderr

        wrapped_slash_context_allowed = run_guard(
            "<command-name>/clear</command-name>",
            1,
            heartbeat_dir,
            transcript_path=transcript,
            total_budget=41_000,
            static_reserve=100,
        )
        assert wrapped_slash_context_allowed.returncode == 0, (
            wrapped_slash_context_allowed.stdout + wrapped_slash_context_allowed.stderr
        )

        wrapped_local_command_allowed = run_guard(
            "<command-name>/help</command-name>",
            1,
            heartbeat_dir,
            transcript_path=transcript,
            total_budget=41_000,
            static_reserve=100,
        )
        assert wrapped_local_command_allowed.returncode == 0, (
            wrapped_local_command_allowed.stdout + wrapped_local_command_allowed.stderr
        )

        downstream_warned = run_guard(
            "ok",
            10,
            heartbeat_dir,
            total_budget=1000,
            warn_budget=100,
            static_reserve=80,
            downstream_reserve=30,
        )
        assert downstream_warned.returncode == 0, downstream_warned.stdout + downstream_warned.stderr
        payload = json.loads(downstream_warned.stdout)
        assert payload["decision"] == "approve"
        assert payload["detail"]["downstream_context_reserve_chars"] == 30
        assert "downstream_reserve=30" in payload["reason"]
        pressure_state = json.loads((heartbeat_dir / "memory-os" / "context_pressure_state.json").read_text(encoding="utf-8"))
        assert pressure_state["last_pressure_level"] == "high"

        raw_transcript = heartbeat_dir / "raw-transcript.jsonl"
        raw_transcript.write_text("not-json\n" + ("z" * 300), encoding="utf-8")
        raw_warned = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=raw_transcript,
            total_budget=1000,
            warn_budget=350,
            static_reserve=100,
            downstream_reserve=1,
        )
        assert raw_warned.returncode == 0, raw_warned.stdout + raw_warned.stderr
        payload = json.loads(raw_warned.stdout)
        assert payload["decision"] == "approve"
        assert payload["detail"]["transcript_chars"] >= 300

        metadata_heavy = heartbeat_dir / "metadata-heavy.jsonl"
        metadata_heavy.write_text(
            json.dumps({"message": {"content": [{"type": "text", "text": "tiny"}]}})
            + "\n"
            + json.dumps({"type": "attachment", "attachment": {"payload": "m" * 5000}})
            + "\n",
            encoding="utf-8",
        )
        metadata_warned = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=metadata_heavy,
            total_budget=10_000,
            warn_budget=5_000,
            static_reserve=100,
            downstream_reserve=1,
        )
        assert metadata_warned.returncode == 0, metadata_warned.stdout + metadata_warned.stderr
        payload = json.loads(metadata_warned.stdout)
        assert payload["decision"] == "approve"
        assert payload["detail"]["transcript_chars"] >= 5000

        home = heartbeat_dir / "home"
        memory_os = home / ".claude" / "memory-os"
        memory_os.mkdir(parents=True)
        state_file = memory_os / "thrashing_state.json"
        state_file.write_text(
            json.dumps({
                "schema_version": 2,
                "session_id": "session-after-compact",
                "epoch_id": 1,
                "last_compact_ts": 1,
                "session_bytes": 100,
                "epoch_bytes": 100,
            }),
            encoding="utf-8",
        )
        huge_historical_transcript = heartbeat_dir / "huge-historical.jsonl"
        write_compacted_transcript(huge_historical_transcript, "h" * 5000, "fresh")
        compact_allowed = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=huge_historical_transcript,
            total_budget=350,
            static_reserve=100,
            downstream_reserve=1,
            session_id="session-after-compact",
            state_file=state_file,
        )
        assert compact_allowed.returncode == 0, compact_allowed.stdout + compact_allowed.stderr

        mismatched_session_allowed = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=huge_historical_transcript,
            total_budget=350,
            static_reserve=100,
            downstream_reserve=1,
            session_id="different-session",
            state_file=state_file,
        )
        assert mismatched_session_allowed.returncode == 0, mismatched_session_allowed.stdout + mismatched_session_allowed.stderr

        stale_epoch_transcript = heartbeat_dir / "stale-epoch.jsonl"
        write_compacted_transcript(stale_epoch_transcript, "h" * 5000, "a" * 500)
        stale_epoch_warned = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=stale_epoch_transcript,
            total_budget=350,
            static_reserve=100,
            downstream_reserve=1,
            session_id="session-after-compact",
            state_file=state_file,
        )
        assert stale_epoch_warned.returncode == 0, stale_epoch_warned.stdout + stale_epoch_warned.stderr
        payload = json.loads(stale_epoch_warned.stdout)
        assert payload["decision"] == "approve"
        assert payload["detail"]["transcript_accounting"] == "compact_epoch"
        assert payload["detail"]["transcript_chars"] >= 500

        stdout_marker_transcript = heartbeat_dir / "stdout-marker.jsonl"
        write_stdout_compacted_transcript(stdout_marker_transcript, "h" * 5000, "fresh")
        stdout_marker_allowed = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=stdout_marker_transcript,
            total_budget=500,
            static_reserve=100,
            downstream_reserve=1,
            session_id="different-session",
            state_file=state_file,
        )
        assert stdout_marker_allowed.returncode == 0, stdout_marker_allowed.stdout + stdout_marker_allowed.stderr

        marker_shapes = [
            {"hook_event_name": "PostCompact"},
            {"hookSpecificOutput": {"hookEventName": "PostCompact"}},
            {"attachment": {"stdout": json.dumps({"hookSpecificOutput": {"hookEventName": "PostCompact"}})}},
            '{"subtype": "compact"}',
        ]
        for index, marker in enumerate(marker_shapes):
            marker_transcript = heartbeat_dir / f"marker-shape-{index}.jsonl"
            write_marker_transcript(marker_transcript, marker)
            marker_allowed = run_guard(
                "ok",
                10,
                heartbeat_dir,
                transcript_path=marker_transcript,
                total_budget=350,
                static_reserve=100,
                downstream_reserve=1,
                session_id="different-session",
                state_file=state_file,
            )
            assert marker_allowed.returncode == 0, marker_allowed.stdout + marker_allowed.stderr

        early_marker_transcript = heartbeat_dir / "early-marker.jsonl"
        write_compacted_transcript(early_marker_transcript, "h" * 5000, "fresh")
        with early_marker_transcript.open("a", encoding="utf-8") as transcript_file:
            transcript_file.write("not-json\n" + ("z" * 4_100_000))
        early_marker_warned = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=early_marker_transcript,
            total_budget=350,
            static_reserve=100,
            downstream_reserve=1,
            session_id="different-session",
            state_file=state_file,
        )
        assert early_marker_warned.returncode == 0, early_marker_warned.stdout + early_marker_warned.stderr
        payload = json.loads(early_marker_warned.stdout)
        assert payload["decision"] == "approve"
        assert payload["detail"]["transcript_accounting"] == "tail"
        assert payload["detail"]["transcript_chars"] >= 4_000_000

        bounded_scan_transcript = heartbeat_dir / "bounded-scan.jsonl"
        write_compacted_transcript(bounded_scan_transcript, "h" * 5000, "fresh")
        bounded_scan_allowed = run_guard(
            "ok",
            10,
            heartbeat_dir,
            transcript_path=bounded_scan_transcript,
            total_budget=10_000,
            static_reserve=100,
            downstream_reserve=1,
            session_id="different-session",
            state_file=state_file,
            compact_marker_scan_bytes=64,
        )
        assert bounded_scan_allowed.returncode == 0, bounded_scan_allowed.stdout + bounded_scan_allowed.stderr
        assert bounded_scan_allowed.stdout == ""

    print("✅ prompt_budget_guard tests passed")


def test_prompt_budget_guard_regression() -> None:
    main()


if __name__ == "__main__":
    main()
