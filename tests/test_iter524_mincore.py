"""
iter524: mincore — Memory Residency Validation

OS 类比：Linux mincore() (Linus Torvalds, 1994)
  查询 [addr, addr+length) 范围内哪些页面驻留在物理内存中。
  返回位图：1=resident(page cache hit), 0=not resident(需 fault in)。

测试覆盖：
  T1  基本扫描 — 有高 imp + 有访问的不被校准
  T2  anomaly 触发 — 高 imp 段零访问超过 anomaly_ratio
  T3  anomaly 未触发 — 零访问率低于 anomaly_ratio
  T4  保护机制 — mlock (oom_adj <= -500) 不被校准
  T5  保护机制 — design_constraint 不被校准
  T6  保护机制 — task_state 不被校准
  T7  校准效果 — importance 正确衰减
  T8  max_per_scan — 批次限制
  T9  空 DB — 无 chunks 不报错
  T10 project 隔离 — 只校准指定 project
  T11 与 numa_balancing 互补 — mincore 校准后 access 恢复能 promote
  T12 bump_chunk_version — 校准后 TLB 失效
  T13 性能 — 100 chunks < 50ms
  T14 FTS5 一致性 — 校准不破坏 FTS5
"""
import sys
import os
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── test isolation ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: E402, F401 — test isolation (must precede store imports)

from memory_os.store.vfs_compat import open_db, ensure_schema
from memory_os.store.core import bump_chunk_version, dmesg_log, DMESG_INFO
from memory_os.store.mm import mincore


def _setup_db():
    """Create in-memory DB with schema."""
    conn = open_db(":memory:")
    ensure_schema(conn)
    return conn


def _make_chunk(conn, summary, importance=0.9, access_count=0,
                chunk_type="decision", project="test_mincore",
                oom_adj=0, age_days=0):
    """Helper: 插入一个测试 chunk."""
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    chunk_id = f"mc-{summary[:20].replace(' ', '_')}"
    conn.execute("""
        INSERT INTO memory_chunks
        (id, summary, content, chunk_type, importance, project,
         source_session, created_at, last_accessed, access_count,
         lru_gen, oom_adj)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        chunk_id,
        summary, summary, chunk_type, importance, project,
        "test", created, created, access_count,
        0, oom_adj,
    ))
    # FTS5
    conn.execute("""
        INSERT INTO memory_chunks_fts (rowid, summary, content)
        SELECT rowid, summary, content FROM memory_chunks
        WHERE id = ?
    """, (chunk_id,))
    conn.commit()


def test_T1_resident_not_calibrated():
    """高 importance + 有 access 的 chunk 不被校准。"""
    conn = _setup_db()
    _make_chunk(conn, "resident chunk A", importance=0.9, access_count=5)
    _make_chunk(conn, "resident chunk B", importance=0.85, access_count=2)
    result = mincore(conn, "test_mincore")
    assert result["resident"] == 2
    assert result["calibrated"] == 0
    conn.close()


def test_T2_anomaly_triggers():
    """高 imp 段零访问超过 anomaly_ratio 时触发校准。"""
    conn = _setup_db()
    # 3 non-resident, 1 resident → 75% > 50% anomaly_ratio
    _make_chunk(conn, "nr1 cold chunk", importance=0.9, access_count=0)
    _make_chunk(conn, "nr2 cold chunk", importance=0.85, access_count=0)
    _make_chunk(conn, "nr3 cold chunk", importance=0.80, access_count=0)
    _make_chunk(conn, "r1 hot chunk", importance=0.75, access_count=3)
    result = mincore(conn, "test_mincore")
    assert result["anomaly_detected"] is True
    assert result["non_resident"] == 3
    assert result["resident"] == 1
    assert result["calibrated"] == 3
    conn.close()


def test_T3_no_anomaly():
    """零访问率低于 anomaly_ratio 时不触发。"""
    conn = _setup_db()
    # 1 non-resident, 3 resident → 25% < 50%
    _make_chunk(conn, "nr_only one", importance=0.9, access_count=0)
    _make_chunk(conn, "r_hot1", importance=0.85, access_count=5)
    _make_chunk(conn, "r_hot2", importance=0.80, access_count=3)
    _make_chunk(conn, "r_hot3", importance=0.75, access_count=1)
    result = mincore(conn, "test_mincore")
    assert result["anomaly_detected"] is False
    assert result["calibrated"] == 0
    conn.close()


def test_T4_mlock_protected():
    """oom_adj <= -500 (mlock) 不被校准。"""
    conn = _setup_db()
    _make_chunk(conn, "mlock chunk", importance=0.95, access_count=0, oom_adj=-1000)
    _make_chunk(conn, "normal cold", importance=0.90, access_count=0)
    _make_chunk(conn, "normal cold2", importance=0.85, access_count=0)
    result = mincore(conn, "test_mincore")
    assert result["anomaly_detected"] is True
    assert result["skipped_protected"] >= 1
    # mlock chunk 的 importance 不变
    row = conn.execute("SELECT importance FROM memory_chunks WHERE summary LIKE 'mlock%'").fetchone()
    assert row[0] == 0.95
    conn.close()


def test_T5_design_constraint_protected():
    """design_constraint 类型不被校准。"""
    conn = _setup_db()
    _make_chunk(conn, "dc protect", importance=0.95, access_count=0,
                chunk_type="design_constraint")
    _make_chunk(conn, "normal dc cold", importance=0.90, access_count=0)
    _make_chunk(conn, "normal dc cold2", importance=0.85, access_count=0)
    result = mincore(conn, "test_mincore")
    assert result["skipped_protected"] >= 1
    row = conn.execute("SELECT importance FROM memory_chunks WHERE summary = 'dc protect'").fetchone()
    assert row[0] == 0.95
    conn.close()


def test_T6_task_state_protected():
    """task_state 类型不被校准。"""
    conn = _setup_db()
    _make_chunk(conn, "ts protect", importance=0.90, access_count=0,
                chunk_type="task_state")
    _make_chunk(conn, "ts cold normal", importance=0.85, access_count=0)
    _make_chunk(conn, "ts cold normal2", importance=0.80, access_count=0)
    result = mincore(conn, "test_mincore")
    assert result["skipped_protected"] >= 1
    row = conn.execute("SELECT importance FROM memory_chunks WHERE summary = 'ts protect'").fetchone()
    assert row[0] == 0.90
    conn.close()


def test_T7_calibration_decay():
    """校准后 importance 正确衰减（默认 ×0.75）。"""
    conn = _setup_db()
    _make_chunk(conn, "decay target", importance=0.90, access_count=0)
    # 需要触发 anomaly：全部是 non-resident
    _make_chunk(conn, "decay target2", importance=0.80, access_count=0)
    result = mincore(conn, "test_mincore")
    assert result["anomaly_detected"] is True
    row = conn.execute("SELECT importance FROM memory_chunks WHERE summary = 'decay target'").fetchone()
    # 0.90 * 0.75 = 0.675
    assert abs(row[0] - 0.675) < 0.01
    conn.close()


def test_T8_max_per_scan():
    """批次限制：最多校准 max_per_scan 个 chunks。"""
    conn = _setup_db()
    # 插入 50 个高 imp 零 access chunks
    for i in range(50):
        _make_chunk(conn, f"batch {i:03d}", importance=0.90, access_count=0)
    result = mincore(conn, "test_mincore")
    assert result["anomaly_detected"] is True
    # 默认 max_per_scan = 30
    assert result["calibrated"] <= 30
    conn.close()


def test_T9_empty_db():
    """空 DB 不报错。"""
    conn = _setup_db()
    result = mincore(conn, "test_mincore")
    assert result["total_high"] == 0
    assert result["calibrated"] == 0
    assert result["anomaly_detected"] is False
    conn.close()


def test_T10_project_isolation():
    """只校准指定 project 的 chunks。"""
    conn = _setup_db()
    _make_chunk(conn, "proj_a cold", importance=0.90, access_count=0, project="proj_a")
    _make_chunk(conn, "proj_a cold2", importance=0.85, access_count=0, project="proj_a")
    _make_chunk(conn, "proj_b cold", importance=0.90, access_count=0, project="proj_b")

    # 只扫描 proj_a
    result = mincore(conn, "proj_a")
    assert result["total_high"] == 2
    assert result["calibrated"] == 2

    # proj_b 不受影响
    row = conn.execute("SELECT importance FROM memory_chunks WHERE summary = 'proj_b cold'").fetchone()
    assert row[0] == 0.90
    conn.close()


def test_T11_mincore_then_access_promotes():
    """mincore 校准后，如果 chunk 被 access，numa_balancing 可以 promote 回来。"""
    conn = _setup_db()
    _make_chunk(conn, "mc_promote test", importance=0.90, access_count=0)
    _make_chunk(conn, "mc_promote test2", importance=0.85, access_count=0)

    # Step 1: mincore 校准降低 importance
    result = mincore(conn, "test_mincore")
    assert result["calibrated"] >= 1
    row = conn.execute("SELECT importance FROM memory_chunks WHERE summary = 'mc_promote test'").fetchone()
    calibrated_imp = row[0]
    assert calibrated_imp < 0.90  # 确认已降级

    # Step 2: 模拟访问
    conn.execute(
        "UPDATE memory_chunks SET access_count = 5 WHERE summary = 'mc_promote test'"
    )
    conn.commit()

    # Step 3: numa_balancing 可以 promote
    from memory_os.store.mm import numa_balancing
    nb_result = numa_balancing(conn, "test_mincore")
    row2 = conn.execute("SELECT importance FROM memory_chunks WHERE summary = 'mc_promote test'").fetchone()
    # 被 access 后应该被 promote 或至少不低于 calibrated
    assert row2[0] >= calibrated_imp
    conn.close()


def test_T12_bump_chunk_version():
    """校准后触发 chunk_version bump（TLB 失效）。"""
    conn = _setup_db()
    _make_chunk(conn, "ver test 1", importance=0.90, access_count=0)
    _make_chunk(conn, "ver test 2", importance=0.85, access_count=0)

    # 获取初始 version
    from memory_os.store.core import MEMORY_OS_DIR
    ver_file = Path(MEMORY_OS_DIR) / ".chunk_version"
    v_before = 0
    if ver_file.exists():
        try:
            v_before = int(ver_file.read_text().strip())
        except Exception:
            pass

    result = mincore(conn, "test_mincore")
    assert result["calibrated"] > 0

    v_after = 0
    if ver_file.exists():
        try:
            v_after = int(ver_file.read_text().strip())
        except Exception:
            pass
    assert v_after > v_before
    conn.close()


def test_T13_performance():
    """100 chunks 扫描 < 50ms。"""
    conn = _setup_db()
    for i in range(100):
        _make_chunk(conn, f"perf chunk {i:04d}", importance=0.90, access_count=0)

    t0 = time.time()
    result = mincore(conn, "test_mincore")
    elapsed = (time.time() - t0) * 1000
    assert elapsed < 50, f"mincore took {elapsed:.1f}ms for 100 chunks (limit 50ms)"
    conn.close()


def test_T14_fts5_consistency():
    """校准后 FTS5 一致性不被破坏。"""
    conn = _setup_db()
    _make_chunk(conn, "fts test chunk", importance=0.90, access_count=0)
    _make_chunk(conn, "fts test chunk2", importance=0.85, access_count=0)

    # 校准前
    chunks_before = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE project='test_mincore'").fetchone()[0]
    fts_before = conn.execute("""
        SELECT COUNT(*) FROM memory_chunks_fts
        WHERE memory_chunks_fts MATCH 'fts test'
    """).fetchone()[0]

    result = mincore(conn, "test_mincore")

    # 校准后 — chunks 数量不变，FTS5 仍然可查
    chunks_after = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE project='test_mincore'").fetchone()[0]
    fts_after = conn.execute("""
        SELECT COUNT(*) FROM memory_chunks_fts
        WHERE memory_chunks_fts MATCH 'fts test'
    """).fetchone()[0]

    assert chunks_before == chunks_after, "mincore should not delete chunks"
    assert fts_before == fts_after, "mincore should not affect FTS5"
    conn.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
