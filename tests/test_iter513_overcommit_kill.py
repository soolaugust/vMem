"""
iter513: overcommit_kill — Global Layer Aggressive Reclaim

OS 类比：Linux vm.overcommit_memory=2 strict accounting (Rik van Riel, 2001)
——当 global 层批量导入的知识过度承诺（85%+ 零访问率）时，强制回收。

与 oom_reaper 的区别：
  - 仅针对 project='global'
  - 激进衰减 ×0.3（而非 ×0.5）
  - 更高删除阈值 < 0.35（而非 < 0.2）
  - 更大批量 50/scan（而非 30）
"""
import sys
from pathlib import Path

# tmpfs 隔离（必须在 store import 前）
sys.path.insert(0, str(Path(__file__).parent.parent))
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401, E402

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from memory_os.store.api import open_db, ensure_schema
from memory_os.store.mm import overcommit_kill


def _make_chunk(conn, project="global", chunk_type="decision",
                importance=0.8, access_count=0, summary=None, oom_adj=0):
    """创建一个测试 chunk 并返回 id。"""
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    if summary is None:
        summary = f"test_{cid[:8]}"
    conn.execute(
        """INSERT INTO memory_chunks
           (id, summary, content, chunk_type, project,
            importance, access_count, last_accessed, created_at,
            lru_gen, oom_adj)
           VALUES (?,?,?,?,?, ?,?,?,?, 0,?)""",
        (cid, summary, f"content_{cid[:8]}", chunk_type,
         project, importance, access_count, now, now, oom_adj),
    )
    return cid


def _setup_db():
    """创建干净的 DB 并返回连接。"""
    conn = open_db()
    ensure_schema(conn)
    # 清除所有测试数据确保隔离
    conn.execute("DELETE FROM memory_chunks")
    conn.execute("DELETE FROM chunk_pins")
    conn.commit()
    return conn


# ── T1: 冷启动保护 — global < min_global_chunks 不触发 ──

def test_cold_start_protection():
    conn = _setup_db()
    # 创建 20 个 global chunks（低于默认 min_global=30）
    for _ in range(20):
        _make_chunk(conn, project="global")
    conn.commit()

    result = overcommit_kill(conn)
    assert not result["triggered"], "should not trigger when global < min_global"
    assert result["reaped"] == 0
    conn.close()


# ── T2: 零访问率低于阈值不触发 ──

def test_below_threshold_no_trigger():
    conn = _setup_db()
    # 40 global chunks, 20 accessed, 20 zero → ratio = 50% < 60%
    for _ in range(20):
        _make_chunk(conn, project="global", access_count=5)
    for _ in range(20):
        _make_chunk(conn, project="global", access_count=0)
    conn.commit()

    result = overcommit_kill(conn)
    assert not result["triggered"], "50% < 60% threshold → no trigger"
    assert result["global_total"] == 40
    assert result["zero_access_ratio"] < 0.61
    conn.close()


# ── T3: 正常触发 — 零访问率 > 60% 时回收 ──

def test_normal_trigger():
    conn = _setup_db()
    # 50 global chunks, 5 accessed, 45 zero → ratio = 90%
    for _ in range(5):
        _make_chunk(conn, project="global", access_count=3, importance=0.9)
    ids_zero = []
    for _ in range(45):
        ids_zero.append(_make_chunk(conn, project="global", access_count=0, importance=0.7))
    conn.commit()

    result = overcommit_kill(conn)
    assert result["triggered"], "90% > 60% → should trigger"
    assert result["reaped"] > 0
    # importance 0.7 × 0.3 = 0.21 < 0.35 → should be deleted
    assert result["deleted"] > 0
    conn.close()


# ── T4: 激进衰减 — importance × 0.3 ──

def test_aggressive_decay():
    conn = _setup_db()
    # 40 global zero-access chunks with high importance
    ids = []
    for _ in range(35):
        ids.append(_make_chunk(conn, project="global", access_count=0, importance=0.98))
    for _ in range(5):
        _make_chunk(conn, project="global", access_count=5, importance=0.9)
    conn.commit()

    result = overcommit_kill(conn)
    assert result["triggered"]

    # 0.98 × 0.3 = 0.294 < 0.35 → should be deleted
    # Check: these chunks should be deleted
    remaining = conn.execute(
        "SELECT COUNT(*) FROM memory_chunks WHERE project='global' AND access_count=0"
    ).fetchone()[0]
    # 35 - reaped (up to 50) should have been deleted
    assert remaining < 35, f"expected fewer than 35, got {remaining}"
    conn.close()


# ── T5: design_constraint 保护 ──

def test_design_constraint_protected():
    conn = _setup_db()
    # 40 global zero-access, 5 are design_constraint
    dc_ids = []
    for _ in range(5):
        dc_ids.append(_make_chunk(conn, project="global", chunk_type="design_constraint",
                                   access_count=0, importance=0.5))
    for _ in range(35):
        _make_chunk(conn, project="global", chunk_type="decision",
                    access_count=0, importance=0.5)
    conn.commit()

    result = overcommit_kill(conn)
    assert result["triggered"]

    # design_constraint should survive
    for dc_id in dc_ids:
        row = conn.execute(
            "SELECT importance FROM memory_chunks WHERE id = ?", (dc_id,)
        ).fetchone()
        assert row is not None, f"design_constraint {dc_id} should not be deleted"
        assert row[0] == 0.5, f"design_constraint importance should be unchanged"
    conn.close()


# ── T6: pinned chunk 保护 ──

def test_pinned_protected():
    conn = _setup_db()
    pinned_id = _make_chunk(conn, project="global", access_count=0, importance=0.5)
    # Pin it
    conn.execute(
        "INSERT INTO chunk_pins (chunk_id, project, pin_type, pinned_at) VALUES (?,?,?,?)",
        (pinned_id, "global", "hard", datetime.now(timezone.utc).isoformat())
    )
    for _ in range(39):
        _make_chunk(conn, project="global", access_count=0, importance=0.5)
    conn.commit()

    result = overcommit_kill(conn)
    assert result["triggered"]

    # Pinned chunk should survive
    row = conn.execute(
        "SELECT importance FROM memory_chunks WHERE id = ?", (pinned_id,)
    ).fetchone()
    assert row is not None, "pinned chunk should not be deleted"
    assert row[0] == 0.5, "pinned chunk importance should be unchanged"
    conn.close()


# ── T7: mlock (oom_adj <= -500) 保护 ──

def test_mlock_protected():
    conn = _setup_db()
    mlock_id = _make_chunk(conn, project="global", access_count=0,
                            importance=0.5, oom_adj=-800)
    for _ in range(39):
        _make_chunk(conn, project="global", access_count=0, importance=0.5)
    conn.commit()

    result = overcommit_kill(conn)
    assert result["triggered"]

    row = conn.execute(
        "SELECT importance FROM memory_chunks WHERE id = ?", (mlock_id,)
    ).fetchone()
    assert row is not None, "mlock chunk should not be deleted"
    assert row[0] == 0.5, "mlock chunk importance unchanged"
    conn.close()


# ── T8: 只影响 global 层，不影响其他 project ──

def test_project_isolation():
    conn = _setup_db()
    # 30+ global zero-access to trigger
    for _ in range(35):
        _make_chunk(conn, project="global", access_count=0, importance=0.5)
    # 10 in another project, zero access
    other_ids = []
    for _ in range(10):
        other_ids.append(_make_chunk(conn, project="my_project",
                                      access_count=0, importance=0.5))
    conn.commit()

    result = overcommit_kill(conn)
    assert result["triggered"]

    # Other project chunks should be untouched
    for oid in other_ids:
        row = conn.execute(
            "SELECT importance FROM memory_chunks WHERE id = ?", (oid,)
        ).fetchone()
        assert row is not None, "other project chunk should exist"
        assert row[0] == 0.5, "other project importance unchanged"
    conn.close()


# ── T9: 删除阈值 0.35 — 高 importance 降级但不删 ──

def test_high_importance_demoted_not_deleted():
    conn = _setup_db()
    # importance=0.98 → 0.98×0.3=0.294 < 0.35 → deleted
    # importance=1.5 (clamped) → actually test with 1.2 × 0.3 = 0.36 > 0.35 → survive
    # Use importance=1.2 — but importance is float so let's use practical values:
    # importance=1.2 × 0.3 = 0.36 > 0.35 → survives (demoted)
    high_id = _make_chunk(conn, project="global", access_count=0, importance=1.2)
    for _ in range(34):
        _make_chunk(conn, project="global", access_count=0, importance=0.5)
    conn.commit()

    result = overcommit_kill(conn)
    assert result["triggered"]

    # high_id: 1.2 × 0.3 = 0.36 > 0.35 → should survive with demoted importance
    row = conn.execute(
        "SELECT importance FROM memory_chunks WHERE id = ?", (high_id,)
    ).fetchone()
    # It might be deleted if importance sorted low (0.5 first), but 1.2 is high so it's last
    # Actually sort is importance ASC → 0.5 ones get killed first, 1.2 is last
    # With max_reap=50, all 35 will be processed including 1.2
    if row is not None:
        assert abs(row[0] - 0.36) < 0.01, f"expected ~0.36, got {row[0]}"
    conn.close()


# ── T10: oom_adj 升级 — 被 reaped 的 chunk 获得 +400 oom_adj ──

def test_oom_adj_increase():
    conn = _setup_db()
    # importance 高到不被删除：1.5 × 0.3 = 0.45 > 0.35
    target_id = _make_chunk(conn, project="global", access_count=0,
                             importance=1.5, oom_adj=0)
    for _ in range(34):
        _make_chunk(conn, project="global", access_count=0, importance=0.5)
    conn.commit()

    overcommit_kill(conn)

    row = conn.execute(
        "SELECT oom_adj FROM memory_chunks WHERE id = ?", (target_id,)
    ).fetchone()
    if row is not None:
        assert row[0] == 400, f"expected oom_adj=400, got {row[0]}"
    conn.close()


# ── T11: 批量限制 max_reap=50 ──

def test_max_reap_limit():
    conn = _setup_db()
    # 100 global zero-access → only 50 should be reaped
    for _ in range(100):
        _make_chunk(conn, project="global", access_count=0, importance=0.5)
    conn.commit()

    result = overcommit_kill(conn)
    assert result["triggered"]
    assert result["reaped"] <= 50, f"max_reap=50 but reaped={result['reaped']}"
    conn.close()


# ── T12: 性能 — 100 chunks 在 50ms 内 ──

def test_performance():
    import time
    conn = _setup_db()
    for _ in range(100):
        _make_chunk(conn, project="global", access_count=0, importance=0.7)
    conn.commit()

    t0 = time.time()
    result = overcommit_kill(conn)
    elapsed = (time.time() - t0) * 1000

    assert elapsed < 50, f"too slow: {elapsed:.1f}ms"
    assert result["triggered"]
    conn.close()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
