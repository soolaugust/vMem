"""
test_context_offload.py — Context Offload Protocol 测试

验证三级注入格式 (full/offload/minimal) 的压缩效果。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_os.runtime.context.offload_compat import (
    format_full, format_offload, format_minimal,
    format_chunks, measure_compression
)


def _sample_chunks(n=5):
    return [
        {
            "id": f"chunk_{i:04d}_abcdefgh",
            "chunk_type": "decision",
            "summary": f"决策{i}：使用 SQLite WAL 模式进行持久化存储避免锁竞争" * 2,
            "importance": 0.6 + i * 0.05,
            "raw_snippet": f"经过测试对比 WAL 模式写入延迟比 DELETE 模式低 40%..." * 3,
        }
        for i in range(n)
    ]


class TestFormatFull:

    def test_includes_summary(self):
        chunks = _sample_chunks(1)
        text = format_full(chunks)
        assert "决策0" in text

    def test_includes_raw_snippet(self):
        chunks = _sample_chunks(1)
        text = format_full(chunks)
        assert "原文" in text

    def test_respects_max_chars(self):
        chunks = _sample_chunks(10)
        text = format_full(chunks, max_chars=200)
        assert len(text) <= 250  # small overshoot ok due to last line


class TestFormatOffload:

    def test_compact_ref_format(self):
        chunks = _sample_chunks(1)
        text = format_offload(chunks)
        assert "[ref:" in text
        assert "decision:" in text

    def test_summary_truncated(self):
        chunks = _sample_chunks(1)
        text = format_offload(chunks)
        lines = text.strip().split("\n")
        ref_line = lines[0]
        # ref line should be short
        assert len(ref_line) < 60

    def test_drill_down_hint(self):
        chunks = _sample_chunks(3)
        text = format_offload(chunks)
        assert "memory_lookup" in text

    def test_no_raw_snippet(self):
        chunks = _sample_chunks(1)
        text = format_offload(chunks)
        assert "原文" not in text


class TestFormatMinimal:

    def test_filters_low_importance(self):
        chunks = [
            {"id": "a", "chunk_type": "decision", "summary": "低价值", "importance": 0.3},
            {"id": "b", "chunk_type": "decision", "summary": "高价值", "importance": 0.9},
        ]
        text = format_minimal(chunks)
        assert "高价值" in text
        assert "低价值" not in text

    def test_empty_when_all_low(self):
        chunks = [
            {"id": "a", "chunk_type": "decision", "summary": "低", "importance": 0.3},
        ]
        text = format_minimal(chunks)
        assert text == ""


class TestFormatChunks:

    def test_pressure_none_uses_full(self):
        chunks = _sample_chunks(3)
        text = format_chunks(chunks, pressure="none")
        assert "原文" in text

    def test_pressure_some_uses_offload(self):
        chunks = _sample_chunks(3)
        text = format_chunks(chunks, pressure="some")
        assert "[ref:" in text
        assert "memory_lookup" in text

    def test_pressure_full_uses_minimal(self):
        chunks = _sample_chunks(3)
        text = format_chunks(chunks, pressure="full")
        # minimal only keeps importance >= 0.8
        # our sample has 0.6-0.8 so some may be filtered
        assert "原文" not in text
        assert "[ref:" not in text


class TestCompression:

    def test_offload_saves_tokens(self):
        chunks = _sample_chunks(5)
        metrics = measure_compression(chunks)
        assert metrics["offload_savings_pct"] > 40, \
            f"Expected >40% savings, got {metrics['offload_savings_pct']}%"

    def test_minimal_saves_more(self):
        chunks = _sample_chunks(5)
        # Make some high importance for minimal to keep
        for c in chunks:
            c["importance"] = 0.9
        metrics = measure_compression(chunks)
        assert metrics["minimal_savings_pct"] > metrics["offload_savings_pct"]
