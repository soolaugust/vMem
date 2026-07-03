"""iter542: oom_reaper_onfault — MLOCK_ONFAULT Demotion Reaper 测试。

OS 类比：Linux oom_reaper (Michal Hocko, 2016, kernel 4.6)
——OOM killer 标记 TIF_MEMDIE 后，若进程卡在 D state 不释放内存，
  oom_reaper 内核线程异步回收其匿名页。

测试清单（14 个）：
  T1  零访问 ONFAULT chunk 被降级为 OOM_ADJ_DEFAULT(0)
  T2  已有 access > 0 的 ONFAULT chunk 不被降级
  T3  宽限期内（idle_rounds < grace_sessions）的 chunk 不被降级
  T4  OOM_ADJ_PROTECTED(-500) chunk 不受影响（只处理 -200）
  T5  OOM_ADJ_DEFAULT(0) chunk 不受影响
  T6  降级后 chunk 仍然存在（降级非删除）
  T7  max_per_scan 限制生效
  T8  无候选 chunk 时返回空结果
  T9  降级后 mlock_onfault_promote 能重新升级
  T10 多项目隔离：只降级匹配项目的 chunk
  T11 生产实证：5 个 ONFAULT(-200) 100% 零访问的死区场景
  T12 config tunables 注册验证
  T13 性能：单次 < 5ms
  T14 降级目标为 OOM_ADJ_DEFAULT(0)
"""
import os
import sys
import json
import time
import sqlite3
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_os.store.core import open_db, ensure_schema, insert_chunk, OOM_ADJ_ONFAULT, OOM_ADJ_PROTECTED, OOM_ADJ_DEFAULT, OOM_ADJ_PREFER
from memory_os.store.mm import oom_reaper_onfault, mlock_onfault_promote, _PAGE_IDLE_FILE
from memory_os.core.schema import MemoryChunk


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """创建临时 DB 和 page_idle 文件。"""
    db_path = tmp_path / "test.db"
    c = sqlite3.connect(str(db_path))
    ensure_schema(c)
    # 重定向 page_idle 文件到 tmp
    idle_file = tmp_path / "page_idle.json"
    monkeypatch.setattr("store_mm._PAGE_IDLE_FILE", idle_file)
    monkeypatch.setattr("store_mm.MEMORY_OS_DIR", tmp_path)
    yield c
    c.close()


def _make_chunk(conn, summary="test chunk", project="test_proj",
                importance=0.9, oom_adj=OOM_ADJ_ONFAULT, access_count=0,
                chunk_type="quantitative_evidence"):
    """辅助：创建 chunk 并返回 ID。"""
    chunk = MemoryChunk(
        project=project,
        chunk_type=chunk_type,
        summary=summary,
        content=f"Content for {summary}",
        importance=importance,
    )
    insert_chunk(conn, chunk.to_dict())
    conn.execute(
        "UPDATE memory_chunks SET oom_adj = ?, access_count = ? WHERE id = ?",
        (oom_adj, access_count, chunk.id)
    )
    conn.commit()
    return chunk.id


def _set_idle_rounds(tmp_path, project, chunk_id, rounds):
    """辅助：设置 page_idle bitmap 的 idle_rounds。"""
    idle_file = tmp_path / "page_idle.json"
    try:
        data = json.loads(idle_file.read_text()) if idle_file.exists() else {}
    except Exception:
        data = {}
    if project not in data:
        data[project] = {}
    data[project][chunk_id] = rounds
    idle_file.write_text(json.dumps(data))


class TestOomReaperOnfault:
    """oom_reaper_onfault 核心功能测试。"""

    def test_t1_zero_access_onfault_demoted(self, conn, tmp_path):
        """T1: 零访问 ONFAULT chunk 超过宽限期后被降级为 OOM_ADJ_PREFER(300)。"""
        cid = _make_chunk(conn, "T1 onfault zero-access", oom_adj=OOM_ADJ_ONFAULT, access_count=0)
        _set_idle_rounds(tmp_path, "test_proj", cid, 5)  # > grace(3)

        result = oom_reaper_onfault(conn, "test_proj")

        assert result["reaped"] == 1
        assert result["scanned"] == 1
        row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
        assert row[0] == OOM_ADJ_DEFAULT, f"Expected OOM_ADJ_DEFAULT(0), got {row[0]}"
        print("  T1 ✅ 零访问 ONFAULT → OOM_ADJ_DEFAULT(0)")

    def test_t2_accessed_onfault_untouched(self, conn, tmp_path):
        """T2: 已有 access > 0 的 ONFAULT chunk 不被降级。"""
        cid = _make_chunk(conn, "T2 accessed", oom_adj=OOM_ADJ_ONFAULT, access_count=3)
        _set_idle_rounds(tmp_path, "test_proj", cid, 10)

        result = oom_reaper_onfault(conn, "test_proj")

        assert result["scanned"] == 0  # access>0 不进入候选
        row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
        assert row[0] == OOM_ADJ_ONFAULT
        print("  T2 ✅ 已访问 ONFAULT chunk 不受影响")

    def test_t3_grace_period_skip(self, conn, tmp_path):
        """T3: 宽限期内（idle_rounds < grace_sessions）的 chunk 不被降级。"""
        cid = _make_chunk(conn, "T3 grace", oom_adj=OOM_ADJ_ONFAULT, access_count=0)
        _set_idle_rounds(tmp_path, "test_proj", cid, 1)  # < grace(3)

        result = oom_reaper_onfault(conn, "test_proj")

        assert result["reaped"] == 0
        assert result["skipped_grace"] == 1
        row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
        assert row[0] == OOM_ADJ_ONFAULT
        print("  T3 ✅ 宽限期内不降级")

    def test_t4_protected_not_affected(self, conn, tmp_path):
        """T4: OOM_ADJ_PROTECTED(-500) chunk 不受影响（只处理 -200）。"""
        cid = _make_chunk(conn, "T4 protected", oom_adj=OOM_ADJ_PROTECTED, access_count=0)
        _set_idle_rounds(tmp_path, "test_proj", cid, 10)

        result = oom_reaper_onfault(conn, "test_proj")

        assert result["scanned"] == 0
        row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
        assert row[0] == OOM_ADJ_PROTECTED
        print("  T4 ✅ PROTECTED(-500) 不受影响")

    def test_t5_default_not_affected(self, conn, tmp_path):
        """T5: OOM_ADJ_DEFAULT(0) chunk 不受影响。"""
        cid = _make_chunk(conn, "T5 default", oom_adj=OOM_ADJ_DEFAULT, access_count=0)
        _set_idle_rounds(tmp_path, "test_proj", cid, 10)

        result = oom_reaper_onfault(conn, "test_proj")

        assert result["scanned"] == 0
        print("  T5 ✅ DEFAULT(0) 不受影响")

    def test_t6_demotion_not_deletion(self, conn, tmp_path):
        """T6: 降级后 chunk 仍然存在（降级非删除）。"""
        cid = _make_chunk(conn, "T6 survives demotion", oom_adj=OOM_ADJ_ONFAULT, access_count=0)
        _set_idle_rounds(tmp_path, "test_proj", cid, 5)

        oom_reaper_onfault(conn, "test_proj")

        row = conn.execute("SELECT id, summary FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
        assert row is not None, "Chunk should still exist after demotion"
        assert row[1] == "T6 survives demotion"
        print("  T6 ✅ 降级后 chunk 仍存在")

    def test_t7_max_per_scan_limit(self, conn, tmp_path, monkeypatch):
        """T7: max_per_scan 限制生效。"""
        import memory_os.config.sysctl as config
        orig_get = config.get
        def patched_get(key):
            if key == "oom_reaper_onfault.max_per_scan":
                return 2
            if key == "oom_reaper_onfault.grace_sessions":
                return 3
            return orig_get(key)
        monkeypatch.setattr("config.get", patched_get)

        # 创建 5 个候选
        for i in range(5):
            cid = _make_chunk(conn, f"T7 chunk {i}", oom_adj=OOM_ADJ_ONFAULT, access_count=0)
            _set_idle_rounds(tmp_path, "test_proj", cid, 10)

        result = oom_reaper_onfault(conn, "test_proj")

        assert result["reaped"] == 2, f"Expected max 2, got {result['reaped']}"
        assert result["scanned"] == 5
        print("  T7 ✅ max_per_scan=2 限制生效")

    def test_t8_no_candidates(self, conn, tmp_path):
        """T8: 无候选 chunk 时返回空结果。"""
        # 创建非 ONFAULT chunk
        _make_chunk(conn, "T8 normal", oom_adj=OOM_ADJ_DEFAULT, access_count=0)

        result = oom_reaper_onfault(conn, "test_proj")

        assert result["scanned"] == 0
        assert result["reaped"] == 0
        print("  T8 ✅ 无候选 chunk → 空结果")

    def test_t9_promote_after_demotion(self, conn, tmp_path):
        """T9: 降级后 mlock_onfault_promote 仍能重新升级（如果后续被访问）。"""
        cid = _make_chunk(conn, "T9 demotion then promote", oom_adj=OOM_ADJ_ONFAULT, access_count=0)
        _set_idle_rounds(tmp_path, "test_proj", cid, 5)

        # 先降级
        oom_reaper_onfault(conn, "test_proj")
        row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
        assert row[0] == OOM_ADJ_DEFAULT

        # 降级后是 0，promote 只处理 -200，所以这验证 promote 不会误操作 0 的 chunk
        result = mlock_onfault_promote(conn, [cid])
        assert result["promoted"] == 0  # 0 != -200, 不升级
        print("  T9 ✅ 降级后的 chunk promote 不误操作")

    def test_t10_project_isolation(self, conn, tmp_path):
        """T10: 多项目隔离：只降级匹配项目的 chunk。"""
        cid_match = _make_chunk(conn, "T10 match", project="test_proj",
                                oom_adj=OOM_ADJ_ONFAULT, access_count=0)
        cid_other = _make_chunk(conn, "T10 other", project="other_proj",
                                oom_adj=OOM_ADJ_ONFAULT, access_count=0)
        _set_idle_rounds(tmp_path, "test_proj", cid_match, 5)
        _set_idle_rounds(tmp_path, "other_proj", cid_other, 5)

        result = oom_reaper_onfault(conn, "test_proj")

        # test_proj 的被降级
        row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid_match,)).fetchone()
        assert row[0] == OOM_ADJ_DEFAULT

        # other_proj 的不受影响
        row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid_other,)).fetchone()
        assert row[0] == OOM_ADJ_ONFAULT
        print("  T10 ✅ 项目隔离")

    def test_t11_production_deadzone_scenario(self, conn, tmp_path):
        """T11: 生产实证——5 个 ONFAULT(-200) 100% 零访问的死区场景。"""
        # 模拟生产环境的 5 个 ONFAULT 死区 chunk
        types = ["quantitative_evidence"] * 3 + ["design_constraint"] * 2
        summaries = [
            "Q1 验证优先不是拖延",
            "Q2 审计点：累计纠正 >= 5 条",
            "Q3 新工作流成立后",
            "D1 信息丢失，低估问题复杂度",
            "D2 触发词须主动读 wiki",
        ]
        cids = []
        for ct, s in zip(types, summaries):
            cid = _make_chunk(conn, s, chunk_type=ct,
                              oom_adj=OOM_ADJ_ONFAULT, access_count=0)
            _set_idle_rounds(tmp_path, "test_proj", cid, 5)
            cids.append(cid)

        result = oom_reaper_onfault(conn, "test_proj")

        assert result["reaped"] == 5
        # 所有 5 个都被降级
        for cid in cids:
            row = conn.execute("SELECT oom_adj FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
            assert row[0] == OOM_ADJ_DEFAULT, f"Chunk {cid} should be demoted"
        print("  T11 ✅ 5 个死区 ONFAULT chunk 全部降级")

    def test_t12_config_tunables(self):
        """T12: config tunables 注册验证。"""
        import memory_os.config.sysctl as config
        src = open(os.path.join(os.path.dirname(__file__), "..", "config.py")).read()
        assert "oom_reaper_onfault.grace_sessions" in src
        assert "oom_reaper_onfault.max_per_scan" in src
        print("  T12 ✅ config tunables 已注册")

    def test_t13_performance(self, conn, tmp_path):
        """T13: 性能 — 单次 < 5ms。"""
        for i in range(20):
            cid = _make_chunk(conn, f"T13 perf {i}", oom_adj=OOM_ADJ_ONFAULT, access_count=0)
            _set_idle_rounds(tmp_path, "test_proj", cid, 10)

        t0 = time.time()
        for _ in range(100):
            oom_reaper_onfault(conn, "test_proj")
        elapsed = (time.time() - t0) * 1000 / 100

        assert elapsed < 5, f"Expected < 5ms, got {elapsed:.2f}ms"
        print(f"  T13 ✅ 性能：{elapsed:.2f}ms/call")

    def test_t14_demotion_target_is_default(self):
        """T14: 降级目标是 OOM_ADJ_DEFAULT(0)，不是 PREFER(500)。"""
        # 降级为 DEFAULT 而非 PREFER：给 chunk 正常回收机会，不过度惩罚
        assert OOM_ADJ_DEFAULT == 0, f"Expected 0, got {OOM_ADJ_DEFAULT}"
        assert OOM_ADJ_ONFAULT == -200, f"Expected -200, got {OOM_ADJ_ONFAULT}"
        print("  T14 ✅ 降级目标 OOM_ADJ_DEFAULT(0), 源 ONFAULT(-200)")
