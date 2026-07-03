"""
iter532: cpuset — FTS5 Index Quarantine for Bandwidth Violators

OS 类比：Linux sched_setaffinity() / cpuset (Ingo Molnár, 2004)
  物理隔离：从 FTS5 索引移除垄断 chunk，使搜索物理上不可能命中它。
"""
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401 — must be before store imports for test isolation

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import pytest

from memory_os.store.api import open_db, ensure_schema, bump_chunk_version
from memory_os.store.mm import (
    cpuset_quarantine, _cpuset_load, _cpuset_save, _QUARANTINE_FILE,
)
from memory_os.store.vfs_compat import _cjk_tokenize, _normalize_structured_summary
from memory_os.config.sysctl import get as _cfg


_PROJECT = "test_cpuset_project"


def _make_chunk(conn, summary, chunk_type="decision", importance=0.8,
                access_count=0):
    """Insert a test chunk via raw SQL and return its ID."""
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO memory_chunks
           (id, project, chunk_type, content, summary, importance,
            created_at, updated_at, last_accessed, access_count,
            source_session, encode_context)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, _PROJECT, chunk_type, summary, summary, importance,
         now, now, now, access_count, "test-session", "")
    )
    # Insert FTS5 entry
    rowid = conn.execute(
        "SELECT rowid FROM memory_chunks WHERE id = ?", (cid,)
    ).fetchone()[0]
    fts_text = _cjk_tokenize(_normalize_structured_summary(summary))
    conn.execute(
        "INSERT INTO memory_chunks_fts(rowid_ref, summary, content) VALUES (?, ?, ?)",
        (str(rowid), fts_text, fts_text)
    )
    return cid


def _insert_traces(conn, project, chunk_ids_per_trace):
    """Insert recall_traces with given chunk ID sets."""
    for ids in chunk_ids_per_trace:
        top_k = [{"id": cid, "score": 0.5} for cid in ids]
        conn.execute(
            "INSERT INTO recall_traces (id, project, session_id, prompt_hash, top_k_json, timestamp) "
            "VALUES (?, ?, 'test-sess', 'hash', ?, datetime('now'))",
            (str(uuid.uuid4()), project, json.dumps(top_k))
        )
    conn.commit()


@pytest.fixture
def db():
    conn = open_db()
    ensure_schema(conn)
    yield conn
    conn.close()
    # Clean quarantine file
    if os.path.exists(_QUARANTINE_FILE):
        os.remove(_QUARANTINE_FILE)


class TestCpusetQuarantine:
    """cpuset_quarantine: FTS5 隔离 + cooldown 释放."""

    def test_no_traces_skip(self, db):
        """T1: 无 recall_traces 时跳过（min_traces 保护）"""
        _make_chunk(db, "some chunk for cpuset test")
        db.commit()
        result = cpuset_quarantine(db, _PROJECT)
        assert result["quarantined"] == []
        assert result["released"] == []
        assert result["active"] == 0

    def test_below_threshold_no_quarantine(self, db):
        """T2: 召回率低于阈值时不隔离"""
        cid1 = _make_chunk(db, "normal chunk alpha")
        cid2 = _make_chunk(db, "normal chunk beta")
        db.commit()
        # 20 traces: each chunk appears in ~50% (below default 0.50 threshold)
        traces = []
        for i in range(20):
            if i % 2 == 0:
                traces.append([cid1])
            else:
                traces.append([cid2])
        _insert_traces(db, _PROJECT, traces)
        result = cpuset_quarantine(db, _PROJECT)
        assert result["quarantined"] == []

    def test_violator_gets_quarantined(self, db):
        """T3: 召回率超过阈值 → 从 FTS5 移除"""
        cid_mono = _make_chunk(db, "monopoly chunk for quarantine test")
        cid_other = _make_chunk(db, "other chunk secondary")
        db.commit()
        # 20 traces: monopoly appears in 18/20 = 90% (>> 50% threshold)
        traces = []
        for i in range(20):
            if i < 18:
                traces.append([cid_mono, cid_other])
            else:
                traces.append([cid_other])
        _insert_traces(db, _PROJECT, traces)

        result = cpuset_quarantine(db, _PROJECT)
        assert cid_mono in result["quarantined"]
        assert result["active"] >= 1

        # Verify FTS5 entry removed
        rowid = db.execute(
            "SELECT rowid FROM memory_chunks WHERE id = ?", (cid_mono,)
        ).fetchone()[0]
        fts_row = db.execute(
            "SELECT COUNT(*) FROM memory_chunks_fts WHERE rowid_ref = ?",
            (str(rowid),)
        ).fetchone()[0]
        assert fts_row == 0, "quarantined chunk should be removed from FTS5"

        # Verify main table still has the chunk
        main_row = db.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE id = ?", (cid_mono,)
        ).fetchone()[0]
        assert main_row == 1, "quarantined chunk must remain in main table"

    def test_cooldown_release(self, db):
        """T4: cooldown 到期后自动恢复 FTS5 索引"""
        cid = _make_chunk(db, "chunk to be released from quarantine")
        db.commit()

        # Manually quarantine with sessions_remaining=1
        _cpuset_save({cid: {
            "sessions_remaining": 1,
            "quarantined_at": "2026-01-01T00:00:00",
            "recall_rate": 0.8,
        }})

        # Remove from FTS5 manually (simulate quarantine state)
        rowid = db.execute(
            "SELECT rowid FROM memory_chunks WHERE id = ?", (cid,)
        ).fetchone()[0]
        db.execute("DELETE FROM memory_chunks_fts WHERE rowid_ref = ?",
                   (str(rowid),))
        db.commit()

        # Run quarantine — should release (sessions_remaining 1→0)
        result = cpuset_quarantine(db, _PROJECT)
        assert cid in result["released"]

        # Verify FTS5 restored
        fts_row = db.execute(
            "SELECT COUNT(*) FROM memory_chunks_fts WHERE rowid_ref = ?",
            (str(rowid),)
        ).fetchone()[0]
        assert fts_row == 1, "released chunk should be re-indexed in FTS5"

    def test_max_quarantine_limit(self, db):
        """T5: 同时隔离数不超过 max_quarantine"""
        max_q = int(_cfg("cpuset.max_quarantine"))
        # Create max_q + 2 monopoly chunks
        cids = []
        for i in range(max_q + 2):
            cid = _make_chunk(db, f"monopoly chunk number {i}")
            cids.append(cid)
        db.commit()
        # All chunks appear in every trace (100% recall rate)
        traces = [[cid for cid in cids] for _ in range(20)]
        _insert_traces(db, _PROJECT, traces)

        result = cpuset_quarantine(db, _PROJECT)
        assert len(result["quarantined"]) <= max_q

    def test_idempotent_no_double_quarantine(self, db):
        """T6: 已隔离 chunk 不会被重复隔离"""
        cid = _make_chunk(db, "already quarantined chunk idempotent")
        db.commit()
        traces = [[cid] for _ in range(20)]
        _insert_traces(db, _PROJECT, traces)

        # First run: quarantines
        r1 = cpuset_quarantine(db, _PROJECT)
        assert cid in r1["quarantined"]

        # Second run: already in registry, should not re-quarantine
        r2 = cpuset_quarantine(db, _PROJECT)
        assert cid not in r2["quarantined"]
        assert r2["active"] >= 1  # still quarantined

    def test_registry_persistence(self, db):
        """T7: 隔离注册表 JSON 持久化正确"""
        test_data = {"chunk-123": {
            "sessions_remaining": 3,
            "quarantined_at": "2026-01-01T00:00:00",
            "recall_rate": 0.65,
        }}
        _cpuset_save(test_data)
        loaded = _cpuset_load()
        assert loaded["chunk-123"]["sessions_remaining"] == 3
        assert loaded["chunk-123"]["recall_rate"] == 0.65

    def test_empty_registry_safe(self, db):
        """T8: 空注册表/无文件时安全运行"""
        if os.path.exists(_QUARANTINE_FILE):
            os.remove(_QUARANTINE_FILE)
        loaded = _cpuset_load()
        assert loaded == {}

    def test_fts5_search_excludes_quarantined(self, db):
        """T9: FTS5 搜索确认找不到被隔离的 chunk"""
        cid = _make_chunk(db, "unique searchterm xyzzy for cpuset")
        db.commit()
        # Verify FTS5 can find it before quarantine
        pre_results = db.execute(
            "SELECT COUNT(*) FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ?",
            ("xyzzy",)
        ).fetchone()[0]
        assert pre_results >= 1

        # Quarantine it
        traces = [[cid] for _ in range(20)]
        _insert_traces(db, _PROJECT, traces)
        cpuset_quarantine(db, _PROJECT)

        # FTS5 should no longer find it
        post_results = db.execute(
            "SELECT COUNT(*) FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ?",
            ("xyzzy",)
        ).fetchone()[0]
        assert post_results == 0, "quarantined chunk must not appear in FTS5 search"

    def test_sessions_decrement(self, db):
        """T10: 每次 SessionStart 调用递减 sessions_remaining"""
        cid = "test-decrement-chunk"
        _cpuset_save({cid: {
            "sessions_remaining": 3,
            "quarantined_at": "2026-01-01T00:00:00",
            "recall_rate": 0.7,
        }})
        # Run once — should decrement 3→2
        cpuset_quarantine(db, _PROJECT)
        reg = _cpuset_load()
        assert reg[cid]["sessions_remaining"] == 2

    def test_performance(self, db):
        """T11: 性能 — 50 chunks, 30 traces < 50ms"""
        cids = []
        for i in range(50):
            cid = _make_chunk(db, f"perf chunk {i} for cpuset benchmark")
            cids.append(cid)
        db.commit()
        traces = [[cids[0], cids[1]] for _ in range(30)]
        _insert_traces(db, _PROJECT, traces)

        t0 = time.perf_counter()
        cpuset_quarantine(db, _PROJECT)
        elapsed = (time.perf_counter() - t0) * 1000
        assert elapsed < 50, f"cpuset_quarantine too slow: {elapsed:.1f}ms"

    def test_cooldown_multi_session(self, db):
        """T12: 多 session cooldown 正确递减到释放"""
        cid = _make_chunk(db, "multi session cooldown chunk")
        db.commit()

        # Set cooldown = 2
        _cpuset_save({cid: {
            "sessions_remaining": 2,
            "quarantined_at": "2026-01-01T00:00:00",
            "recall_rate": 0.8,
        }})
        # Remove from FTS5
        rowid = db.execute(
            "SELECT rowid FROM memory_chunks WHERE id = ?", (cid,)
        ).fetchone()[0]
        db.execute("DELETE FROM memory_chunks_fts WHERE rowid_ref = ?",
                   (str(rowid),))
        db.commit()

        # Session 1: decrement 2→1, not released yet
        r1 = cpuset_quarantine(db, _PROJECT)
        assert cid not in r1["released"]
        reg = _cpuset_load()
        assert reg[cid]["sessions_remaining"] == 1

        # Session 2: decrement 1→0, released
        r2 = cpuset_quarantine(db, _PROJECT)
        assert cid in r2["released"]
        reg2 = _cpuset_load()
        assert cid not in reg2
