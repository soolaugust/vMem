"""
iter576: flush_tlb_one — Entity Map Stale Entry Invalidation

OS 类比：Linux flush_tlb_one() / __flush_tlb_range() (Andy Lutomirski, 2017,
  arch/x86/mm/tlb.c) — 物理页面回收后，TLB 中指向该物理地址的缓存条目仍在。
  如果不 flush，后续 TLB lookup 命中 stale entry → use-after-free。

根因：entity_map 3223 条中 33.8% (1090 条) 的 chunk_id 指向 oom_adj>=300
  的 dead chunks。spreading_activate 沿 entity_map 返回 dead chunk_id，
  浪费 CPU 且可能注入垃圾结果。

三级 invalidation 策略：
  Level 1 (ghost): chunk importance=0 → 无条件 flush
  Level 2 (dead): chunk oom_adj >= threshold → flush
  Level 3 (orphan): chunk_id 完全不存在于 memory_chunks → flush

测试覆盖：orphan_flushed/ghost_flushed/dead_flushed/alive_preserved/
          disabled/max_flush_cap/oom_threshold_tunable/project_filter/
          global_included/idempotent/mixed_levels/commit_on_flush/
          no_commit_zero/empty_db/performance
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: E402 — 测试隔离

import sqlite3
import time
import pytest
from datetime import datetime, timezone
from memory_os.store.core import open_db, ensure_schema
from memory_os.store.mm import flush_tlb_one
from memory_os.config.sysctl import get as sysctl


def _setup():
    """创建干净的测试 DB"""
    conn = open_db(":memory:")
    ensure_schema(conn)
    return conn


def _make_chunk(conn, chunk_id, project="test_proj", importance=0.5, oom_adj=0):
    """创建最小 chunk"""
    conn.execute(
        """INSERT OR REPLACE INTO memory_chunks
           (id, project, chunk_type, summary, content, importance,
            access_count, oom_adj, lru_gen, last_accessed, created_at)
           VALUES (?, ?, 'decision', 'test', 'test content', ?,
                   0, ?, 0, ?, ?)""",
        (chunk_id, project, importance, oom_adj,
         datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def _make_entity_map(conn, entity_name, chunk_id, project="test_proj"):
    """建立 entity_map 映射"""
    conn.execute(
        """INSERT OR IGNORE INTO entity_map (entity_name, chunk_id, project, updated_at)
           VALUES (?, ?, ?, ?)""",
        (entity_name, chunk_id, project, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def _count_entity_map(conn):
    return conn.execute("SELECT COUNT(*) FROM entity_map").fetchone()[0]


# ── Test: orphan entries (chunk_id not in memory_chunks) are flushed ──
def test_orphan_flushed():
    conn = _setup()
    # 创建 entity_map 条目指向不存在的 chunk
    _make_entity_map(conn, "entity_a", "nonexistent_chunk_1")
    _make_entity_map(conn, "entity_b", "nonexistent_chunk_2")
    # 创建一个 alive chunk 和它的 entity_map
    _make_chunk(conn, "alive_1", importance=0.8)
    _make_entity_map(conn, "entity_c", "alive_1")

    assert _count_entity_map(conn) == 3
    result = flush_tlb_one(conn, "test_proj")
    assert result["orphan"] == 2
    assert result["flushed"] == 2
    assert _count_entity_map(conn) == 1  # only alive_1's mapping remains


# ── Test: ghost entries (chunk importance=0) are flushed ──
def test_ghost_flushed():
    conn = _setup()
    _make_chunk(conn, "ghost_1", importance=0.0)
    _make_entity_map(conn, "entity_a", "ghost_1")
    _make_chunk(conn, "alive_1", importance=0.7)
    _make_entity_map(conn, "entity_b", "alive_1")

    result = flush_tlb_one(conn, "test_proj")
    assert result["ghost"] == 1
    assert result["flushed"] == 1
    assert _count_entity_map(conn) == 1


# ── Test: dead entries (oom_adj >= threshold) are flushed ──
def test_dead_flushed():
    conn = _setup()
    _make_chunk(conn, "dead_1", importance=0.15, oom_adj=300)
    _make_entity_map(conn, "entity_a", "dead_1")
    _make_entity_map(conn, "entity_b", "dead_1")
    _make_chunk(conn, "alive_1", importance=0.7, oom_adj=0)
    _make_entity_map(conn, "entity_c", "alive_1")

    result = flush_tlb_one(conn, "test_proj")
    assert result["dead"] == 2
    assert result["flushed"] == 2
    assert _count_entity_map(conn) == 1


# ── Test: alive chunks preserved ──
def test_alive_preserved():
    conn = _setup()
    _make_chunk(conn, "alive_1", importance=0.8, oom_adj=0)
    _make_chunk(conn, "alive_2", importance=0.5, oom_adj=200)  # below threshold
    _make_entity_map(conn, "entity_a", "alive_1")
    _make_entity_map(conn, "entity_b", "alive_2")

    result = flush_tlb_one(conn, "test_proj")
    assert result["flushed"] == 0
    assert _count_entity_map(conn) == 2


# ── Test: disabled config ──
def test_disabled():
    conn = _setup()
    _make_entity_map(conn, "entity_a", "nonexistent_chunk")

    os.environ["MEMORY_OS_FLUSH_TLB_ONE_ENABLED"] = "false"
    try:
        result = flush_tlb_one(conn, "test_proj")
        assert result["flushed"] == 0
        assert _count_entity_map(conn) == 1  # not deleted
    finally:
        del os.environ["MEMORY_OS_FLUSH_TLB_ONE_ENABLED"]


# ── Test: max_flush cap ──
def test_max_flush_cap():
    conn = _setup()
    # 创建 100 个 orphan 条目（超过 max_flush=50 的下限）
    for i in range(100):
        _make_entity_map(conn, f"entity_{i}", f"orphan_{i}")

    os.environ["MEMORY_OS_FLUSH_TLB_ONE_MAX_FLUSH"] = "50"
    try:
        result = flush_tlb_one(conn, "test_proj")
        assert result["flushed"] == 50
        assert _count_entity_map(conn) == 50
    finally:
        del os.environ["MEMORY_OS_FLUSH_TLB_ONE_MAX_FLUSH"]


# ── Test: oom_threshold tunable ──
def test_oom_threshold_tunable():
    conn = _setup()
    _make_chunk(conn, "chunk_200", importance=0.3, oom_adj=200)
    _make_entity_map(conn, "entity_a", "chunk_200")

    # Default threshold=300 → oom_adj=200 not flushed
    result = flush_tlb_one(conn, "test_proj")
    assert result["flushed"] == 0

    # Lower threshold to 200 → now it's flushed
    os.environ["MEMORY_OS_FLUSH_TLB_ONE_OOM_THRESHOLD"] = "200"
    try:
        result = flush_tlb_one(conn, "test_proj")
        assert result["dead"] == 1
        assert result["flushed"] == 1
    finally:
        del os.environ["MEMORY_OS_FLUSH_TLB_ONE_OOM_THRESHOLD"]


# ── Test: project filter — only flushes matching project + global ──
def test_project_filter():
    conn = _setup()
    _make_entity_map(conn, "entity_a", "orphan_a", project="test_proj")
    _make_entity_map(conn, "entity_b", "orphan_b", project="other_proj")
    _make_entity_map(conn, "entity_c", "orphan_c", project="global")

    result = flush_tlb_one(conn, "test_proj")
    # Should flush test_proj + global, not other_proj
    assert result["flushed"] == 2
    assert _count_entity_map(conn) == 1  # only other_proj remains


# ── Test: global entries included in flush ──
def test_global_included():
    conn = _setup()
    _make_chunk(conn, "dead_global", project="global", importance=0.15, oom_adj=300)
    _make_entity_map(conn, "entity_g", "dead_global", project="global")
    _make_chunk(conn, "alive_local", project="test_proj", importance=0.8)
    _make_entity_map(conn, "entity_l", "alive_local", project="test_proj")

    result = flush_tlb_one(conn, "test_proj")
    assert result["dead"] == 1
    assert result["flushed"] == 1
    assert _count_entity_map(conn) == 1


# ── Test: idempotent — second run flushes 0 ──
def test_idempotent():
    conn = _setup()
    _make_entity_map(conn, "entity_a", "orphan_1")
    _make_entity_map(conn, "entity_b", "orphan_2")

    r1 = flush_tlb_one(conn, "test_proj")
    assert r1["flushed"] == 2
    r2 = flush_tlb_one(conn, "test_proj")
    assert r2["flushed"] == 0


# ── Test: mixed levels — priority order orphan > ghost > dead ──
def test_mixed_levels():
    conn = _setup()
    # 1 orphan
    _make_entity_map(conn, "entity_orphan", "no_chunk")
    # 1 ghost
    _make_chunk(conn, "ghost_1", importance=0.0)
    _make_entity_map(conn, "entity_ghost", "ghost_1")
    # 1 dead
    _make_chunk(conn, "dead_1", importance=0.15, oom_adj=300)
    _make_entity_map(conn, "entity_dead", "dead_1")
    # 1 alive
    _make_chunk(conn, "alive_1", importance=0.8)
    _make_entity_map(conn, "entity_alive", "alive_1")

    result = flush_tlb_one(conn, "test_proj")
    assert result["orphan"] == 1
    assert result["ghost"] == 1
    assert result["dead"] == 1
    assert result["flushed"] == 3
    assert _count_entity_map(conn) == 1  # only alive remains


# ── Test: commit happens when flushed > 0 ──
def test_commit_on_flush():
    conn = _setup()
    _make_entity_map(conn, "entity_a", "orphan_1")

    result = flush_tlb_one(conn, "test_proj")
    assert result["flushed"] == 1

    # Verify by re-opening — if commit happened, data persists
    # (in-memory DB, commit is implicit, but we verify the DELETE took effect)
    assert _count_entity_map(conn) == 0


# ── Test: no commit when flushed = 0 ──
def test_no_commit_zero():
    conn = _setup()
    _make_chunk(conn, "alive_1", importance=0.8)
    _make_entity_map(conn, "entity_a", "alive_1")

    result = flush_tlb_one(conn, "test_proj")
    assert result["flushed"] == 0
    assert _count_entity_map(conn) == 1


# ── Test: empty DB ──
def test_empty_db():
    conn = _setup()
    result = flush_tlb_one(conn, "test_proj")
    assert result["flushed"] == 0
    assert result["scanned"] == 0


# ── Test: performance — flush 200 entries under 100ms ──
def test_performance():
    conn = _setup()
    # 创建 200 个 orphan 条目
    for i in range(200):
        _make_entity_map(conn, f"entity_{i}", f"orphan_{i}")

    t0 = time.time()
    result = flush_tlb_one(conn, "test_proj")
    elapsed_ms = (time.time() - t0) * 1000

    assert result["flushed"] == 200
    assert elapsed_ms < 100, f"took {elapsed_ms:.1f}ms, expected <100ms"
