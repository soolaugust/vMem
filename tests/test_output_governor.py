from __future__ import annotations

import json
from pathlib import Path

from hooks.output_governor import govern_transcript

HOOKS_JSON = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"


def _assistant_line(text: str) -> bytes:
    return (json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n").encode("utf-8")


def test_stop_hook_order_preserves_extractor_access_to_full_output() -> None:
    config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    commands = [hook["command"] for entry in config["hooks"]["Stop"] for hook in entry.get("hooks", [])]
    extractor_index = next(index for index, command in enumerate(commands) if "extractor.py" in command)
    governor_index = next(index for index, command in enumerate(commands) if "output_governor.py" in command)
    assert extractor_index < governor_index
    assert config["hooks"]["Stop"][extractor_index]["hooks"][0]["async"] is False
    assert config["hooks"]["Stop"][governor_index]["hooks"][0]["async"] is False


def test_output_governor_pages_long_assistant_output(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "memory-os"
    monkeypatch.setattr("hooks.output_governor.MEMORY_OS_DIR", memory_dir)
    monkeypatch.setattr("hooks.output_governor.STATE_FILE", memory_dir / "output_working_set_state.json")
    transcript = tmp_path / "session.jsonl"
    text = "start " + ("x" * 50_000) + " end"
    transcript.write_bytes(_assistant_line(text))

    result = govern_transcript(transcript, max_active_chars=8_000, page_chars=10_000)

    assert result["changed"] is True
    assert result["pages"] == 6
    active = transcript.read_text(encoding="utf-8")
    assert len(active) < len(text)
    assert "vMem output working-set" in active
    assert "start" in active
    assert "end" in active
    evidence = Path(result["evidence_path"])
    assert evidence.read_text(encoding="utf-8") == text
    state = json.loads((memory_dir / "output_working_set_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "paged"
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" in state["notice"]
    assert "已捕获输出" in state["notice"]
    assert "不要把调大 CLAUDE_CODE_MAX_OUTPUT_TOKENS 当默认方案" in state["notice"]
    assert "设置 CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in state["notice"]
    assert "提高 CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in state["notice"]


def test_output_governor_marks_probably_incomplete_output(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "memory-os"
    monkeypatch.setattr("hooks.output_governor.MEMORY_OS_DIR", memory_dir)
    monkeypatch.setattr("hooks.output_governor.STATE_FILE", memory_dir / "output_working_set_state.json")
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(_assistant_line("```python\nprint('unterminated')\n"))

    result = govern_transcript(transcript, max_active_chars=8_000, page_chars=4_000)

    assert result["changed"] is True
    assert result["incomplete"] is True
    active = transcript.read_text(encoding="utf-8")
    assert "incomplete=True" in active
    assert "may be incomplete" in active
    state = json.loads((memory_dir / "output_working_set_state.json").read_text(encoding="utf-8"))
    assert "缺失尾部" in state["notice"]


def test_output_governor_leaves_small_complete_output_untouched(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "memory-os"
    monkeypatch.setattr("hooks.output_governor.MEMORY_OS_DIR", memory_dir)
    monkeypatch.setattr("hooks.output_governor.STATE_FILE", memory_dir / "output_working_set_state.json")
    transcript = tmp_path / "session.jsonl"
    before = _assistant_line("done.")
    transcript.write_bytes(before)

    result = govern_transcript(transcript, max_active_chars=8_000, page_chars=4_000)

    assert result["changed"] is False
    assert transcript.read_bytes() == before
    assert not (memory_dir / "output_working_set_state.json").exists()


def test_output_governor_clears_stale_state_after_normal_output(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "memory-os"
    memory_dir.mkdir()
    state_file = memory_dir / "output_working_set_state.json"
    state_file.write_text(json.dumps({"status": "paged", "evidence_path": "old"}), encoding="utf-8")
    monkeypatch.setattr("hooks.output_governor.MEMORY_OS_DIR", memory_dir)
    monkeypatch.setattr("hooks.output_governor.STATE_FILE", state_file)
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(_assistant_line("done."))

    result = govern_transcript(transcript, max_active_chars=8_000, page_chars=4_000)

    assert result["changed"] is False
    assert not state_file.exists()
