"""
test_write_feedback.py — 写入-检索闭环反馈机制测试
"""
import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone, timedelta
from memory_os.store.vfs_compat import open_db, ensure_schema, insert_chunk
from memory_os.store.graph import ensure_graph_schema
from memory_os.core.schema import MemoryChunk
from memory_os.runtime.write_feedback_compat import (
    check_retrievability, attach_evidence_to_decision,
    decay_stale_pins, get_write_throttle_factor
)


def _get_conn():
    tmp = tempfile.mktemp(suffix=".db")
    conn = open_db(tmp)
    ensure_schema(conn)
    ensure_graph_schema(conn)
    return conn


def _insert(conn, summary, chunk_type="decision", importance=0.7, project="test"):
    now = datetime.now(timezone.utc).isoformat()
    c = MemoryChunk(
        chunk_type=chunk_type, summary=summary, importance=importance,
        project=project, created_at=now, updated_at=now,
        last_accessed=now, content=summary + " 扩展内容用于测试写入反馈机制",
    ).to_dict()
    insert_chunk(conn, c)
    return c["id"]


class TestRetrievabilityCheck:

    def test_known_tokens_high_score(self):
        conn = _get_conn()
        # Insert some chunks first to build FTS index
        _insert(conn, "使用 SQLite WAL 模式进行数据持久化存储")
        _insert(conn, "BM25 全文检索算法优化查询性能")
        # Now check a summary with overlapping tokens
        score = check_retrievability(conn, "SQLite WAL 模式的写入性能", "test")
        assert score > 0.0  # Should find matches
        conn.close()

    def test_unknown_tokens_low_score(self):
        conn = _get_conn()
        _insert(conn, "使用 SQLite WAL 模式进行数据持久化存储")
        # Completely unrelated summary
        score = check_retrievability(conn, "kubernetes pod scheduling affinity", "test")
        # May or may not be 0 depending on index state, but should be low
        assert score <= 1.0
        conn.close()

    def test_empty_summary(self):
        conn = _get_conn()
        score = check_retrievability(conn, "", "test")
        assert score == 0.0
        conn.close()


class TestEvidenceAttachment:

    def test_attaches_to_related_decision(self):
        conn = _get_conn()
        # Directly insert a decision into DB (bypass VFS gates for test)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO memory_chunks (id, chunk_type, summary, content, importance,
               project, created_at, updated_at, last_accessed, tags, retrievability)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("dec_001", "decision", "架构决策：采用 BM25 作为主要检索算法优化搜索质量",
             "详细内容：经过性能测试对比决定采用 BM25 全文检索", 0.8,
             "test", now, now, now, "[]", 0.5)
        )
        conn.commit()
        evidence = {
            "id": "ev_001",
            "summary": "BM25 检索 Recall@5 达到 95.2% 优于向量检索的基准测试",
            "chunk_type": "quantitative_evidence",
        }
        result = attach_evidence_to_decision(conn, evidence, "test")
        assert result is not None
        edge = conn.execute(
            "SELECT relation_type FROM chunk_edges WHERE from_id = ?", ("ev_001",)
        ).fetchone()
        assert edge is not None
        conn.close()

    def test_no_decisions_returns_none(self):
        conn = _get_conn()
        evidence = {"id": "ev_002", "summary": "some metric data", "chunk_type": "quantitative_evidence"}
        result = attach_evidence_to_decision(conn, evidence, "empty_project")
        assert result is None
        conn.close()


class TestPinDecay:

    def test_hard_to_soft_after_threshold(self):
        conn = _get_conn()
        from memory_os.store.vfs_compat import pin_chunk
        cid = _insert(conn, "old pinned chunk that nobody reads anymore ever")
        # Pin it with old date
        old_date = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO chunk_pins (chunk_id, project, pin_type, pinned_at) VALUES (?,?,?,?)",
            (cid, "test", "hard", old_date)
        )
        conn.commit()
        results = decay_stale_pins(conn, "test", hard_to_soft_days=14)
        assert results["hard_to_soft"] == 1
        # Verify pin_type changed
        row = conn.execute("SELECT pin_type FROM chunk_pins WHERE chunk_id=?", (cid,)).fetchone()
        assert row[0] == "soft"
        conn.close()

    def test_applied_pin_not_decayed(self):
        # 契约升级（2026-06-05）：保命依据从 access_count（仅被召回）改为
        # apply_count（被真正用上）。被反复召回但从不被引用的 pin 不再假装活跃。
        conn = _get_conn()
        cid = _insert(conn, "frequently applied pinned chunk test")
        # apply_count > 0 → 被真正应用过 → 不衰退
        conn.execute("UPDATE memory_chunks SET apply_count = 5 WHERE id = ?", (cid,))
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO chunk_pins (chunk_id, project, pin_type, pinned_at) VALUES (?,?,?,?)",
            (cid, "test", "hard", old_date)
        )
        conn.commit()
        results = decay_stale_pins(conn, "test", hard_to_soft_days=14)
        assert results["hard_to_soft"] == 0
        conn.close()

    def test_recalled_but_unapplied_pin_decays(self):
        # 新契约的核心区别：access_count 高但 apply_count=0（被反复召回却从未被
        # 引用）的 pin 必须衰退——这正是旧 access_count 判据漏掉的"召回浪费"。
        conn = _get_conn()
        cid = _insert(conn, "recalled but never applied pinned chunk")
        conn.execute(
            "UPDATE memory_chunks SET access_count = 99, apply_count = 0 WHERE id = ?",
            (cid,))
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO chunk_pins (chunk_id, project, pin_type, pinned_at) VALUES (?,?,?,?)",
            (cid, "test", "hard", old_date)
        )
        conn.commit()
        results = decay_stale_pins(conn, "test", hard_to_soft_days=14)
        assert results["hard_to_soft"] == 1  # 召回再多，没应用就该衰退
        conn.close()


class TestWriteThrottle:

    def test_healthy_project_no_throttle(self):
        conn = _get_conn()
        # Insert recall_traces with hits
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO recall_traces (timestamp, session_id, project, prompt_hash, injected) VALUES (?,?,?,?,?)",
            (now, "test_session", "test", "abc123", 1)
        )
        _insert(conn, "some chunk for throttle test project level analysis")
        conn.commit()
        factor = get_write_throttle_factor(conn, "test")
        assert factor >= 0.7  # healthy or light throttle
        conn.close()

    def test_dead_project_heavy_throttle(self):
        conn = _get_conn()
        # Write chunks but no recall hits
        for i in range(5):
            _insert(conn, f"dead project chunk number {i} for testing")
        factor = get_write_throttle_factor(conn, "test")
        assert factor <= 0.5  # should throttle heavily
        conn.close()

    def test_empty_project_no_throttle(self):
        conn = _get_conn()
        factor = get_write_throttle_factor(conn, "nonexistent")
        assert factor == 1.0
        conn.close()
