"""
iter531: mlock2(MLOCK_ONFAULT) — Deferred Protection for Unvalidated Knowledge

OS 类比：Linux mlock2(MLOCK_ONFAULT) (Eric B Munson, 2015, kernel 4.4)
  mlock() 立即锁定；mlock2(MLOCK_ONFAULT) 仅标记为"可锁定"，首次 page fault 时才锁入 RAM。

测试清单：
  T1  ONFAULT chunk 被检索命中后升级为 PROTECTED
  T2  已是 PROTECTED 的 chunk 不受影响（幂等）
  T3  已是 MIN(-1000) 的 chunk 不受影响
  T4  DEFAULT(0) chunk 不受影响（非 ONFAULT 不触发）
  T5  空列表不报错
  T6  多个 chunk 混合状态只升级 ONFAULT 的
  T7  升级后 bump_chunk_version 被调用（TLB 失效）
  T8  OOM_ADJ_ONFAULT 值为 -200
  T9  extractor 写入 design_constraint 时使用 ONFAULT 而非 PROTECTED
  T10 extractor 写入 quantitative_evidence 时使用 ONFAULT 而非 PROTECTED
  T11 性能：1000 次 promote < 100ms
  T12 page_idle/numa_balancing 可以降级 ONFAULT chunk（-200 > -500 阈值）
"""
import sys, os, time, sqlite3, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: E402 — 测试隔离
from memory_os.store.mm import mlock_onfault_promote
from memory_os.store.swap import OOM_ADJ_MIN, OOM_ADJ_PROTECTED, OOM_ADJ_ONFAULT, OOM_ADJ_DEFAULT
from memory_os.store.api import open_db, ensure_schema

PROJECT = "test_iter531_mlock_onfault"


def _make_chunk(conn, summary="test", importance=0.9, oom_adj=OOM_ADJ_ONFAULT,
                access_count=0, chunk_type="design_constraint"):
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO memory_chunks (id, summary, content, chunk_type, source_session,
           project, importance, access_count, oom_adj, created_at, last_accessed)
           VALUES (?, ?, '', ?, 'test', ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (cid, summary, chunk_type, PROJECT, importance, access_count, oom_adj))
    conn.commit()
    return cid


def _get_oom_adj(conn, cid):
    row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
    return row[0] if row else None


# ── T1: ONFAULT → PROTECTED on access ──
def test_t1_promote_on_access():
    """ONFAULT(-200) chunk 被检索命中后升级为 PROTECTED(-500)"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "T1 design constraint", oom_adj=OOM_ADJ_ONFAULT)
    result = mlock_onfault_promote(conn, [cid])
    conn.commit()
    assert result["promoted"] == 1, f"Expected 1 promoted, got {result}"
    assert _get_oom_adj(conn, cid) == OOM_ADJ_PROTECTED, \
        f"Expected {OOM_ADJ_PROTECTED}, got {_get_oom_adj(conn, cid)}"
    conn.close()
    print("  T1 ✅ ONFAULT → PROTECTED on first access")


# ── T2: Already PROTECTED → no change ──
def test_t2_already_protected():
    """已是 PROTECTED(-500) 不受影响"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "T2 already protected", oom_adj=OOM_ADJ_PROTECTED)
    result = mlock_onfault_promote(conn, [cid])
    conn.commit()
    assert result["promoted"] == 0
    assert _get_oom_adj(conn, cid) == OOM_ADJ_PROTECTED
    conn.close()
    print("  T2 ✅ Already PROTECTED unchanged")


# ── T3: MIN(-1000) → no change ──
def test_t3_min_unchanged():
    """MIN(-1000) 不受影响"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "T3 absolute lock", oom_adj=OOM_ADJ_MIN)
    result = mlock_onfault_promote(conn, [cid])
    conn.commit()
    assert result["promoted"] == 0
    assert _get_oom_adj(conn, cid) == OOM_ADJ_MIN
    conn.close()
    print("  T3 ✅ MIN unchanged")


# ── T4: DEFAULT(0) → no change ──
def test_t4_default_unchanged():
    """DEFAULT(0) 不是 ONFAULT，不触发"""
    conn = open_db()
    ensure_schema(conn)
    cid = _make_chunk(conn, "T4 default chunk", oom_adj=OOM_ADJ_DEFAULT)
    result = mlock_onfault_promote(conn, [cid])
    conn.commit()
    assert result["promoted"] == 0
    assert _get_oom_adj(conn, cid) == OOM_ADJ_DEFAULT
    conn.close()
    print("  T4 ✅ DEFAULT unchanged")


# ── T5: Empty list ──
def test_t5_empty_list():
    """空列表不报错"""
    conn = open_db()
    ensure_schema(conn)
    result = mlock_onfault_promote(conn, [])
    assert result["promoted"] == 0
    conn.close()
    print("  T5 ✅ Empty list safe")


# ── T6: Mixed states ──
def test_t6_mixed_states():
    """多 chunk 混合状态：只升级 ONFAULT 的"""
    conn = open_db()
    ensure_schema(conn)
    cid_onfault1 = _make_chunk(conn, "T6 onfault A", oom_adj=OOM_ADJ_ONFAULT)
    cid_onfault2 = _make_chunk(conn, "T6 onfault B", oom_adj=OOM_ADJ_ONFAULT)
    cid_protected = _make_chunk(conn, "T6 protected", oom_adj=OOM_ADJ_PROTECTED)
    cid_default = _make_chunk(conn, "T6 default", oom_adj=OOM_ADJ_DEFAULT)

    result = mlock_onfault_promote(conn, [cid_onfault1, cid_onfault2, cid_protected, cid_default])
    conn.commit()

    assert result["promoted"] == 2, f"Expected 2, got {result}"
    assert _get_oom_adj(conn, cid_onfault1) == OOM_ADJ_PROTECTED
    assert _get_oom_adj(conn, cid_onfault2) == OOM_ADJ_PROTECTED
    assert _get_oom_adj(conn, cid_protected) == OOM_ADJ_PROTECTED
    assert _get_oom_adj(conn, cid_default) == OOM_ADJ_DEFAULT
    conn.close()
    print("  T6 ✅ Mixed states: only ONFAULT promoted")


# ── T7: bump_chunk_version called ──
def test_t7_bump_version():
    """升级后 bump_chunk_version 被调用（TLB 失效触发）"""
    conn = open_db()
    ensure_schema(conn)
    # Read initial version
    from memory_os.store.vfs_compat import read_chunk_version
    v_before = read_chunk_version()
    cid = _make_chunk(conn, "T7 version bump", oom_adj=OOM_ADJ_ONFAULT)
    mlock_onfault_promote(conn, [cid])
    conn.commit()
    v_after = read_chunk_version()
    assert v_after > v_before, f"Version should bump: {v_before} → {v_after}"
    conn.close()
    print("  T7 ✅ bump_chunk_version called on promote")


# ── T8: Constant value ──
def test_t8_constant_value():
    """OOM_ADJ_ONFAULT = -200"""
    assert OOM_ADJ_ONFAULT == -200, f"Expected -200, got {OOM_ADJ_ONFAULT}"
    print("  T8 ✅ OOM_ADJ_ONFAULT == -200")


# ── T9: Extractor design_constraint uses ONFAULT ──
def test_t9_extractor_design_constraint():
    """extractor 写入 design_constraint 时应使用 ONFAULT 而非 PROTECTED"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "extractor_src",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "hooks", "extractor.py"))
    # Read the source to check the pattern
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "hooks", "extractor.py")
    with open(src_path) as f:
        src = f.read()
    # Check that constraint path uses ONFAULT
    assert "OOM_ADJ_ONFAULT" in src, "extractor should import OOM_ADJ_ONFAULT"
    # The set_oom_adj for constraints should NOT use OOM_ADJ_PROTECTED directly
    # Find the constraint assignment section
    constraint_section = src[src.find("为设计约束 chunk 设置"):]
    constraint_section = constraint_section[:constraint_section.find("迭代40")]
    assert "OOM_ADJ_ONFAULT" in constraint_section, \
        "design_constraint should use OOM_ADJ_ONFAULT at write time"
    print("  T9 ✅ Extractor design_constraint uses ONFAULT")


# ── T10: Extractor quantitative_evidence uses ONFAULT ──
def test_t10_extractor_quant():
    """extractor 写入 quantitative_evidence 时应使用 ONFAULT"""
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "hooks", "extractor.py")
    with open(src_path) as f:
        src = f.read()
    quant_section = src[src.find("为量化证据 chunk 设置"):]
    quant_section = quant_section[:quant_section.find("为设计约束")]
    assert "OOM_ADJ_ONFAULT" in quant_section, \
        "quantitative_evidence should use OOM_ADJ_ONFAULT at write time"
    print("  T10 ✅ Extractor quantitative_evidence uses ONFAULT")


# ── T11: Performance ──
def test_t11_performance():
    """1000 次 promote 调用 < 100ms"""
    conn = open_db()
    ensure_schema(conn)
    ids = []
    for i in range(100):
        cid = _make_chunk(conn, f"T11 perf {i}", oom_adj=OOM_ADJ_ONFAULT)
        ids.append(cid)

    t0 = time.time()
    for _ in range(10):  # 10 rounds × 100 ids = 1000 checks
        mlock_onfault_promote(conn, ids)
    elapsed = (time.time() - t0) * 1000

    conn.close()
    assert elapsed < 100, f"1000 promotes took {elapsed:.1f}ms (>100ms)"
    print(f"  T11 ✅ Performance: 1000 promotes in {elapsed:.1f}ms")


# ── T12: page_idle/numa_balancing can demote ONFAULT ──
def test_t12_onfault_reclaimable():
    """ONFAULT(-200) > mlock threshold(-500)，所以 page_idle/numa_balancing 可以降级"""
    # page_idle protects oom_adj <= -500, ONFAULT is -200 so NOT protected
    assert OOM_ADJ_ONFAULT > -500, \
        f"ONFAULT({OOM_ADJ_ONFAULT}) must be > -500 to be reclaimable by page_idle"
    # numa_balancing protects oom_adj <= -500, same logic
    assert OOM_ADJ_ONFAULT > -500
    print("  T12 ✅ ONFAULT is reclaimable by page_idle/numa_balancing")


if __name__ == "__main__":
    print("iter531: mlock2(MLOCK_ONFAULT) — Deferred Protection Tests")
    print("=" * 60)
    test_t1_promote_on_access()
    test_t2_already_protected()
    test_t3_min_unchanged()
    test_t4_default_unchanged()
    test_t5_empty_list()
    test_t6_mixed_states()
    test_t7_bump_version()
    test_t8_constant_value()
    test_t9_extractor_design_constraint()
    test_t10_extractor_quant()
    test_t11_performance()
    test_t12_onfault_reclaimable()
    print("=" * 60)
    print("ALL 12/12 PASS ✅")
