"""
test_pre_compact.py — PreCompact hook 单元测试

验证 suspend_prepare notifier chain 的 pinned + decision 注入逻辑。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sqlite3
import json
from memory_os.store.vfs_compat import open_db, ensure_schema, insert_chunk, pin_chunk, get_pinned_chunks
from hooks.pre_compact import _collect_critical_chunks
from memory_os.core.schema import MemoryChunk
from datetime import datetime, timezone


def _make_chunk(chunk_type="decision", summary="test decision", importance=0.7,
                project="test_proj", **kw):
    now = datetime.now(timezone.utc).isoformat()
    c = MemoryChunk(
        chunk_type=chunk_type, summary=summary, importance=importance,
        project=project, created_at=now, updated_at=now, last_accessed=now,
        content=kw.get("content", summary + " extended content for test"),
        **{k: v for k, v in kw.items() if k != "content"}
    )
    return c.to_dict()


def _get_conn():
    db = os.environ.get("MEMORY_OS_DB", ":memory:")
    conn = open_db(db)
    ensure_schema(conn)
    return conn


class TestCollectCriticalChunks:

    def test_hard_pinned_appear(self):
        conn = _get_conn()
        d = _make_chunk(summary="pinned design constraint", chunk_type="design_constraint")
        insert_chunk(conn, d)
        pin_chunk(conn, d["id"], "test_proj", "hard")
        result = _collect_critical_chunks(conn, "test_proj", 2000, 3, 0.6)
        assert "pinned design constraint" in result
        conn.close()

    def test_decisions_appear_when_no_pins(self):
        conn = _get_conn()
        d = _make_chunk(summary="important decision about API design", importance=0.8)
        insert_chunk(conn, d)
        result = _collect_critical_chunks(conn, "test_proj", 2000, 3, 0.6)
        assert "important decision about API design" in result
        conn.close()

    def test_char_budget_respected(self):
        conn = _get_conn()
        for i in range(20):
            d = _make_chunk(summary=f"decision number {i} " + "x" * 50, importance=0.8)
            insert_chunk(conn, d)
        result = _collect_critical_chunks(conn, "test_proj", 200, 10, 0.6)
        assert len(result) <= 200
        conn.close()

    def test_dedup_pinned_and_decision(self):
        conn = _get_conn()
        d = _make_chunk(summary="shared decision", importance=0.9)
        insert_chunk(conn, d)
        pin_chunk(conn, d["id"], "test_proj", "hard")
        result = _collect_critical_chunks(conn, "test_proj", 2000, 3, 0.6)
        assert result.count("shared decision") == 1
        conn.close()

    def test_empty_db(self):
        conn = _get_conn()
        result = _collect_critical_chunks(conn, "test_proj", 2000, 3, 0.6)
        assert result == ""
        conn.close()

    def test_low_importance_filtered(self):
        conn = _get_conn()
        d = _make_chunk(summary="low importance decision", importance=0.3)
        insert_chunk(conn, d)
        result = _collect_critical_chunks(conn, "test_proj", 2000, 3, 0.6)
        assert "low importance decision" not in result
        conn.close()

    def test_decision_top_k_limit(self):
        conn = _get_conn()
        for i in range(10):
            d = _make_chunk(summary=f"decision {i}", importance=0.8)
            insert_chunk(conn, d)
        result = _collect_critical_chunks(conn, "test_proj", 5000, 3, 0.6)
        lines = [l for l in result.strip().split("\n") if l]
        assert len(lines) <= 3
        conn.close()
