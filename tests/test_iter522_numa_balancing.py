"""
iter522: numa_balancing — Access-Pattern Importance Rebalancing 测试

OS 类比：Linux Automatic NUMA Balancing (Ingo Molnár / Peter Zijlstra, 2012)
验证双向 importance 重平衡：promote（热迁移）+ demote（冷迁移）。
"""
import sys
import os
import time
from pathlib import Path

# ── 测试隔离（tmpfs 风格）──
import tempfile
_tmpdir = tempfile.mkdtemp(prefix="test_numa_")
os.environ["MEMORY_OS_DIR"] = _tmpdir
os.environ["MEMORY_OS_DB"] = os.path.join(_tmpdir, "store.db")

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory_os.store.api import open_db, ensure_schema, insert_chunk, bump_chunk_version
from memory_os.store.mm import numa_balancing
from memory_os.config.sysctl import get as _cfg
from datetime import datetime, timezone, timedelta
import json


def _make_chunk(conn, summary, importance=0.5, access_count=0,
                chunk_type="decision", project="test_proj",
                age_days=5, oom_adj=0):
    """创建测试 chunk 并插入 DB。"""
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    chunk_id = f"numa-{summary[:8]}-{access_count}-{int(importance*100)}"
    conn.execute("""
        INSERT OR REPLACE INTO memory_chunks
        (id, summary, content, importance, chunk_type, project,
         access_count, created_at, last_accessed, source_session, oom_adj, lru_gen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (chunk_id, summary, summary, importance, chunk_type, project,
          access_count, created, datetime.now(timezone.utc).isoformat(),
          "test-session", oom_adj))
    conn.commit()
    return chunk_id


def test_promote_high_access_low_importance():
    """T1: 高访问+低 importance → promote 上调。"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "frequently-recalled-rule", importance=0.3, access_count=10)
    result = numa_balancing(conn, "test_proj")
    assert result["promoted"] >= 1, f"Expected promote, got {result}"
    # 验证 importance 被上调
    new_imp = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    assert new_imp > 0.3, f"Expected imp > 0.3, got {new_imp}"
    assert new_imp >= 0.70, f"Expected imp >= promote_floor(0.70), got {new_imp}"
    conn.close()


def test_demote_high_importance_zero_access():
    """T2: 高 importance + 零访问 + 超龄 → demote 下调。"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "never-recalled-noise", importance=0.95, access_count=0, age_days=10)
    result = numa_balancing(conn, "test_proj")
    assert result["demoted"] >= 1, f"Expected demote, got {result}"
    # 验证 importance 被下调
    new_imp = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    assert new_imp < 0.95, f"Expected imp < 0.95, got {new_imp}"
    assert abs(new_imp - 0.95 * 0.70) < 0.01, f"Expected ~{0.95*0.7:.3f}, got {new_imp}"
    conn.close()


def test_no_demote_new_chunk():
    """T3: 零访问但年龄 < demote_min_age_days → 不 demote（宽限期）。"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "brand-new-chunk", importance=0.90, access_count=0, age_days=1)
    result = numa_balancing(conn, "test_proj")
    # 新 chunk 不应被 demote
    new_imp = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    assert new_imp == 0.90, f"New chunk should not be demoted, imp={new_imp}"
    conn.close()


def test_no_demote_design_constraint():
    """T4: design_constraint 类型不被 demote（架构约束始终重要）。"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "arch-constraint", importance=0.90, access_count=0,
                      chunk_type="design_constraint", age_days=10)
    result = numa_balancing(conn, "test_proj")
    new_imp = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    assert new_imp == 0.90, f"design_constraint should not be demoted, imp={new_imp}"
    conn.close()


def test_mlock_protection():
    """T5: oom_adj <= -500 (mlock) → 不参与 promote/demote。"""
    conn = open_db()
    ensure_schema(conn)
    # 低 importance + 高 access 但 mlock → 不 promote
    cid1 = _make_chunk(conn, "mlock-low-imp", importance=0.2, access_count=20, oom_adj=-1000)
    # 高 importance + 零 access 但 mlock → 不 demote
    cid2 = _make_chunk(conn, "mlock-high-imp", importance=0.99, access_count=0,
                       oom_adj=-1000, age_days=10)
    result = numa_balancing(conn, "test_proj")
    assert result["skipped_protected"] >= 1, f"Expected skip, got {result}"
    # 验证两者 importance 不变
    imp1 = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid1,)).fetchone()[0]
    imp2 = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid2,)).fetchone()[0]
    assert imp1 == 0.2, f"mlock chunk should not be promoted, imp={imp1}"
    assert imp2 == 0.99, f"mlock chunk should not be demoted, imp={imp2}"
    conn.close()


def test_promote_formula_log2():
    """T6: promote 公式验证：floor + 0.05 * log2(access_count)。"""
    import math
    conn = open_db()
    ensure_schema(conn)
    # access=8 → floor(0.70) + 0.05*log2(8) = 0.70 + 0.15 = 0.85
    cid = _make_chunk(conn, "access-8-chunk", importance=0.3, access_count=8)
    numa_balancing(conn, "test_proj")
    new_imp = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    expected = 0.70 + 0.05 * math.log2(8)
    assert abs(new_imp - expected) < 0.01, f"Expected ~{expected:.3f}, got {new_imp}"
    conn.close()


def test_promote_cap_095():
    """T7: promote 不超过 0.95 上限。"""
    conn = open_db()
    ensure_schema(conn)
    # access=1000 → floor(0.70) + 0.05*log2(1000) ≈ 0.70+0.50 = 1.20 → capped 0.95
    cid = _make_chunk(conn, "hyper-access-chunk", importance=0.1, access_count=1000)
    numa_balancing(conn, "test_proj")
    new_imp = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    assert new_imp == 0.95, f"Expected cap at 0.95, got {new_imp}"
    conn.close()


def test_no_promote_already_high():
    """T8: 已经高 importance + 高 access → 不 promote（避免无效 UPDATE）。"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "already-high", importance=0.92, access_count=5)
    result = numa_balancing(conn, "test_proj")
    new_imp = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    # importance 已经 >= promote 公式结果，不应变化
    assert new_imp == 0.92, f"Already high imp should not change, got {new_imp}"
    conn.close()


def test_project_isolation():
    """T9: project 参数隔离 — 只影响指定 project。"""
    conn = open_db()
    ensure_schema(conn)
    cid_a = _make_chunk(conn, "proj-a-demote", importance=0.90, access_count=0,
                        project="proj_a", age_days=10)
    cid_b = _make_chunk(conn, "proj-b-demote", importance=0.90, access_count=0,
                        project="proj_b", age_days=10)
    # 只 rebalance proj_a
    result = numa_balancing(conn, "proj_a")
    imp_a = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid_a,)).fetchone()[0]
    imp_b = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid_b,)).fetchone()[0]
    assert imp_a < 0.90, f"proj_a should be demoted, imp={imp_a}"
    assert imp_b == 0.90, f"proj_b should not be affected, imp={imp_b}"
    conn.close()


def test_global_scan():
    """T10: project=None → 扫描全部项目。"""
    conn = open_db()
    ensure_schema(conn)
    cid_a = _make_chunk(conn, "global-a-demote", importance=0.85, access_count=0,
                        project="proj_x", age_days=10)
    cid_b = _make_chunk(conn, "global-b-demote", importance=0.85, access_count=0,
                        project="proj_y", age_days=10)
    result = numa_balancing(conn, project=None)
    imp_a = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid_a,)).fetchone()[0]
    imp_b = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid_b,)).fetchone()[0]
    assert imp_a < 0.85, f"global scan should demote proj_x, imp={imp_a}"
    assert imp_b < 0.85, f"global scan should demote proj_y, imp={imp_b}"
    conn.close()


def test_empty_db_safe():
    """T11: 空 DB 不崩溃。"""
    conn = open_db()
    ensure_schema(conn)
    result = numa_balancing(conn, "empty_proj")
    assert result["promoted"] == 0
    assert result["demoted"] == 0
    assert result["duration_ms"] >= 0
    conn.close()


def test_performance():
    """T12: 100 chunks 场景下性能 < 50ms。"""
    conn = open_db()
    ensure_schema(conn)
    for i in range(100):
        _make_chunk(conn, f"perf-chunk-{i}",
                    importance=0.85 if i % 2 == 0 else 0.3,
                    access_count=0 if i % 2 == 0 else 5,
                    project="perf_proj", age_days=10)
    t0 = time.time()
    result = numa_balancing(conn, "perf_proj")
    elapsed = (time.time() - t0) * 1000
    assert elapsed < 50, f"Performance too slow: {elapsed:.1f}ms"
    assert result["promoted"] + result["demoted"] > 0
    conn.close()


def test_task_state_excluded():
    """T13: task_state 类型不参与 promote/demote。"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "task-state-chunk", importance=0.90, access_count=0,
                      chunk_type="task_state", age_days=10)
    result = numa_balancing(conn, "test_proj")
    new_imp = conn.execute("SELECT importance FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    assert new_imp == 0.90, f"task_state should not be demoted, imp={new_imp}"
    conn.close()


def test_bump_chunk_version():
    """T14: rebalance 后触发 chunk_version bump（TLB 失效）。"""
    from memory_os.store.vfs_compat import CHUNK_VERSION_FILE
    conn = open_db()
    ensure_schema(conn)
    # 获取当前 version（文件系统）
    v_before = 0
    if CHUNK_VERSION_FILE.exists():
        try:
            v_before = int(CHUNK_VERSION_FILE.read_text().strip())
        except Exception:
            pass
    _make_chunk(conn, "version-test", importance=0.90, access_count=0, age_days=10)
    numa_balancing(conn, "test_proj")
    v_after = 0
    if CHUNK_VERSION_FILE.exists():
        try:
            v_after = int(CHUNK_VERSION_FILE.read_text().strip())
        except Exception:
            pass
    assert v_after > v_before, f"chunk_version should be bumped: {v_before} → {v_after}"
    conn.close()


if __name__ == "__main__":
    import shutil
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for t in tests:
        try:
            # 每个测试用新 DB（清理旧数据）
            for f in Path(_tmpdir).glob("*"):
                f.unlink()
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
    print(f"\n{'='*60}")
    print(f"iter522 numa_balancing: {passed}/{passed+failed} passed")
    if failed:
        sys.exit(1)
    # cleanup
    shutil.rmtree(_tmpdir, ignore_errors=True)
