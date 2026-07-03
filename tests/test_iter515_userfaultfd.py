"""
test_iter515_userfaultfd.py — userfaultfd Demand-Paged Import 测试

迭代515：OS 类比 Linux userfaultfd (Andrea Arcangeli, 2015)
验证 import chunks 以低 importance 写入，首次检索命中时自动 promote。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: E402, F401 — 测试隔离

import json
import time
import pytest
from datetime import datetime, timezone, timedelta

from memory_os.store.core import open_db, ensure_schema, insert_chunk, bump_chunk_version
from memory_os.store.mm import userfaultfd_promote
from memory_os.config.sysctl import get as cfg


@pytest.fixture
def conn():
    c = open_db()
    ensure_schema(c)
    c.execute("PRAGMA busy_timeout = 5000")
    yield c
    c.close()


def _make_import_chunk(conn, chunk_id, summary, importance=0.15, oom_adj=300,
                       access_count=0, source_session="import:wiki/test.md"):
    """Helper: 创建一个模拟 import chunk。"""
    now = datetime.now(timezone.utc).isoformat()
    chunk = {
        "id": chunk_id,
        "created_at": now,
        "updated_at": now,
        "project": "global",
        "source_session": source_session,
        "chunk_type": "decision",
        "content": f"Test content for {summary}",
        "summary": summary,
        "tags": "[]",
        "importance": importance,
        "retrievability": 1.0,
        "embedding": "[]",
        "access_count": access_count,
        "last_accessed": now,
        "lru_gen": 0,
        "oom_adj": oom_adj,
    }
    insert_chunk(conn, chunk)
    conn.commit()
    return chunk_id


class TestUserfaultfdPromote:
    """userfaultfd_promote 核心功能测试。"""

    def test_basic_promote(self, conn):
        """T1: import chunk 首次命中时 importance 被提升。"""
        cid = _make_import_chunk(conn, "uffd-t1", "test promote basic")
        result = userfaultfd_promote(conn, [cid])
        assert result["promoted"] == 1
        assert cid in result["ids"]
        # 验证 DB 中的值
        row = conn.execute(
            "SELECT importance, oom_adj FROM memory_chunks WHERE id=?", (cid,)
        ).fetchone()
        assert row[0] == cfg("userfaultfd.promote_importance")  # 0.75
        assert row[1] == cfg("userfaultfd.promote_oom_adj")  # 0

    def test_no_promote_organic(self, conn):
        """T2: 非 import 来源的 chunk 不被 promote。"""
        now = datetime.now(timezone.utc).isoformat()
        chunk = {
            "id": "uffd-t2", "created_at": now, "updated_at": now,
            "project": "global", "source_session": "session123",
            "chunk_type": "decision", "content": "organic",
            "summary": "organic chunk", "tags": "[]",
            "importance": 0.15, "retrievability": 1.0,
            "embedding": "[]", "access_count": 0,
            "last_accessed": now, "lru_gen": 0, "oom_adj": 300,
        }
        insert_chunk(conn, chunk)
        conn.commit()
        result = userfaultfd_promote(conn, ["uffd-t2"])
        assert result["promoted"] == 0

    def test_no_promote_already_high(self, conn):
        """T3: 已经有高 importance 的 import chunk 不被重复 promote。"""
        cid = _make_import_chunk(conn, "uffd-t3", "already promoted",
                                 importance=0.75)
        result = userfaultfd_promote(conn, [cid])
        assert result["promoted"] == 0

    def test_no_promote_accessed_many(self, conn):
        """T4: access_count > 1 的 chunk 不触发 promote（已经是 resident page）。"""
        cid = _make_import_chunk(conn, "uffd-t4", "multi accessed",
                                 access_count=3)
        result = userfaultfd_promote(conn, [cid])
        assert result["promoted"] == 0

    def test_empty_ids(self, conn):
        """T5: 空列表不出错。"""
        result = userfaultfd_promote(conn, [])
        assert result["promoted"] == 0

    def test_mixed_batch(self, conn):
        """T6: 混合 batch 中只 promote 满足条件的。"""
        cid1 = _make_import_chunk(conn, "uffd-t6a", "will promote")
        cid2 = _make_import_chunk(conn, "uffd-t6b", "already promoted",
                                  importance=0.8)
        now = datetime.now(timezone.utc).isoformat()
        chunk_organic = {
            "id": "uffd-t6c", "created_at": now, "updated_at": now,
            "project": "global", "source_session": "organic",
            "chunk_type": "decision", "content": "x",
            "summary": "organic", "tags": "[]",
            "importance": 0.15, "retrievability": 1.0,
            "embedding": "[]", "access_count": 0,
            "last_accessed": now, "lru_gen": 0, "oom_adj": 0,
        }
        insert_chunk(conn, chunk_organic)
        conn.commit()

        result = userfaultfd_promote(conn, [cid1, cid2, "uffd-t6c"])
        assert result["promoted"] == 1
        assert cid1 in result["ids"]

    def test_nonexistent_ids(self, conn):
        """T7: 不存在的 ID 不出错。"""
        result = userfaultfd_promote(conn, ["nonexistent-abc"])
        assert result["promoted"] == 0

    def test_access_count_1_still_promotes(self, conn):
        """T8: access_count=1（刚被 update_accessed）仍 promote（<= 1 条件）。"""
        cid = _make_import_chunk(conn, "uffd-t8", "just accessed once",
                                 access_count=1)
        result = userfaultfd_promote(conn, [cid])
        assert result["promoted"] == 1

    def test_idempotent(self, conn):
        """T9: 第二次调用不再 promote（importance 已 >= 0.4）。"""
        cid = _make_import_chunk(conn, "uffd-t9", "idempotent test")
        r1 = userfaultfd_promote(conn, [cid])
        assert r1["promoted"] == 1
        r2 = userfaultfd_promote(conn, [cid])
        assert r2["promoted"] == 0


class TestImportKnowledgeIntegration:
    """import_knowledge.py 集成测试 — 验证新 chunks 使用低 importance。"""

    def test_make_chunk_default_importance(self):
        """T10: make_chunk 不传 importance 时使用 sysctl 默认值。"""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        from import_knowledge import make_chunk
        chunk = make_chunk("decision", "test summary", "test content",
                           tags=["test"], source_file="test.md")
        assert chunk["importance"] == cfg("userfaultfd.import_base_importance")
        assert chunk["oom_adj"] == cfg("userfaultfd.import_oom_adj")

    def test_make_chunk_explicit_none(self):
        """T11: make_chunk importance=None 等同于默认值。"""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        from import_knowledge import make_chunk
        chunk = make_chunk("decision", "test summary 2", "content 2",
                           importance=None, tags=["t"], source_file="x.md")
        assert chunk["importance"] == cfg("userfaultfd.import_base_importance")


class TestPerformance:
    """性能测试。"""

    def test_promote_performance(self, conn):
        """T12: 100 个 chunk 批量 promote < 50ms。"""
        ids = []
        for i in range(100):
            cid = _make_import_chunk(conn, f"uffd-perf-{i}", f"perf test {i}")
            ids.append(cid)
        conn.commit()

        t0 = time.time()
        result = userfaultfd_promote(conn, ids)
        elapsed = (time.time() - t0) * 1000
        assert result["promoted"] == 100
        assert elapsed < 50, f"Too slow: {elapsed:.1f}ms"
