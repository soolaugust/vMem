"""
test_cache_hit_harness.py — 缓存命中监测 harness 测试

验证确定性指标采集 + 阈值判断（A/B/C 三维度）。用合成 DB,不依赖活跃库。

  T1: 空间采集 — 体积/膨胀比/观测表行数
  T2: chunk 命中率 — access 分布/冷占比/高价值冷
  T3: 命名空间错位 — write_only_projects 检测
  T4: recall 闭环 — 候选率/注入率/反馈精度
  T5: 阈值判断 — 健康库无告警,问题库有对应告警
  T6: 退出码语义（healthy ↔ 告警数）
"""
import sys
import os
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT.parent))

import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401
from tools import cache_hit_harness as H


def _make_db(path, chunks, recall=None, observability=None):
    """构造一个最小合成 store.db。
    chunks: [(project, importance, access_count), ...]
    recall: [(project, candidates_count, injected, user_feedback), ...]
    observability: {table: row_count}
    """
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE memory_chunks(
        id TEXT PRIMARY KEY, project TEXT, importance REAL,
        access_count INTEGER, content TEXT, summary TEXT, created_at TEXT)""")
    for i, tup in enumerate(chunks):
        # tup = (project, importance, access_count[, age])
        # age: "old"(默认,>7天 → 算孤儿) 或 "fresh"(近期 → 信用期内)
        proj, imp, ac = tup[0], tup[1], tup[2]
        age = tup[3] if len(tup) > 3 else "old"
        created = "2026-01-01T00:00:00+00:00" if age == "old" else "2099-01-01T00:00:00+00:00"
        c.execute("INSERT INTO memory_chunks VALUES(?,?,?,?,?,?,?)",
                  (f"c{i}", proj, imp, ac, "body", f"summary {i}", created))
    if recall is not None:
        c.execute("""CREATE TABLE recall_traces(
            id TEXT, project TEXT, candidates_count INTEGER,
            injected INTEGER, user_feedback TEXT)""")
        for i, (proj, cc, inj, fb) in enumerate(recall):
            c.execute("INSERT INTO recall_traces VALUES(?,?,?,?,?)",
                      (f"r{i}", proj, cc, inj, fb))
    if observability:
        for t, n in observability.items():
            c.execute(f"CREATE TABLE {t}(id INTEGER PRIMARY KEY, updated_at TEXT)")
            for j in range(n):
                c.execute(f"INSERT INTO {t}(updated_at) VALUES('2026-01-01')")
    c.commit()
    c.close()


def _db_path(tmp_path):
    return str(tmp_path / "synthetic.db")


class TestSpaceCollection:

    def test_observability_rows_counted(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p, [("projA", 0.5, 1)], observability={"shadow_traces": 100})
        c = sqlite3.connect(p)
        space = H.collect_space(c)
        assert space["observability_rows"]["shadow_traces"] == 100
        assert space["chunk_rows"] == 1


class TestChunkHits:

    def test_cold_pct_and_distribution(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p, [("x", 0.5, 0), ("x", 0.5, 0), ("x", 0.5, 5)])
        c = sqlite3.connect(p)
        h = H.collect_chunk_hits(c)
        assert h["total"] == 3
        assert h["cold"] == 2
        assert h["cold_pct"] == round(100 * 2 / 3, 1)
        assert h["distribution"]["cold"] == 2
        assert h["distribution"]["3-5"] == 1

    def test_high_value_cold(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p, [("x", 0.9, 0), ("x", 0.85, 0), ("x", 0.3, 0)])
        c = sqlite3.connect(p)
        h = H.collect_chunk_hits(c)
        assert h["high_value_cold"] == 2  # 只有 importance>=0.8 的两条

    def test_write_only_projects_detected(self, tmp_path):
        p = _db_path(tmp_path)
        # workspace 写入高价值(old)但从不检索；git:x 既写又检索
        _make_db(p,
            chunks=[("workspace", 0.9, 0, "old"), ("workspace", 0.9, 0, "old"),
                    ("git:x", 0.9, 3, "old")],
            recall=[("git:x", 2, 1, "useful")])
        c = sqlite3.connect(p)
        h = H.collect_chunk_hits(c)
        wo = h["write_only_projects"]
        assert len(wo) == 1
        assert wo[0]["project"] == "workspace"
        assert wo[0]["high_value_cold"] == 2


class TestFreshGracePeriod:
    """阶段3:信用期 —— 新写入 access=0 不算孤儿"""

    def test_fresh_not_counted_as_mismatch(self, tmp_path):
        p = _db_path(tmp_path)
        # 全是近期写入的高价值 access=0 → 应进 fresh_cooling 而非 high_value_cold
        _make_db(p, [("git:x", 0.9, 0, "fresh"), ("git:x", 0.9, 0, "fresh")],
                 recall=[("git:y", 2, 1, "useful")])
        c = sqlite3.connect(p)
        h = H.collect_chunk_hits(c)
        assert h["high_value_cold"] == 0
        assert h["fresh_cooling"] == 2

    def test_aged_counted_as_mismatch(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p, [("workspace", 0.9, 0, "old"), ("workspace", 0.9, 0, "old")],
                 recall=[("git:y", 2, 1, "useful")])
        c = sqlite3.connect(p)
        h = H.collect_chunk_hits(c)
        assert h["high_value_cold"] == 2
        assert h["fresh_cooling"] == 0

    def test_fresh_active_project_not_write_only(self, tmp_path):
        p = _db_path(tmp_path)
        # git:new 是活跃新项目(近期写入)，从不检索 → 不应判为孤儿源
        _make_db(p,
            chunks=[("git:new", 0.9, 0, "fresh"), ("git:new", 0.9, 0, "fresh")],
            recall=[("git:other", 2, 1, "useful")])
        c = sqlite3.connect(p)
        h = H.collect_chunk_hits(c)
        assert h["write_only_projects"] == []  # 活跃新项目不算孤儿源

    def test_namespace_alert_excludes_fresh(self, tmp_path):
        p = _db_path(tmp_path)
        # 15 条近期高价值 access=0 → 因信用期，不触发命名空间错位告警
        _make_db(p, [("git:new", 0.9, 0, "fresh")] * 15,
                 recall=[("git:other", 2, 1, "useful")] * 5)
        r = H.run(p)
        assert not any("命名空间错位" in a for a in r["alerts"])


class TestRecallLoop:

    def test_rates(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p, [("x", 0.5, 1)],
                 recall=[("x", 3, 1, "useful"), ("x", 2, 0, None),
                         ("x", 5, 1, "not_useful"), ("x", 0, 0, None)])
        c = sqlite3.connect(p)
        rc = H.collect_recall_loop(c)
        assert rc["traces"] == 4
        assert rc["candidate_rate"] == 75.0   # 3/4 有候选
        assert rc["inject_rate"] == 50.0      # 2/4 注入
        assert rc["feedback_useful"] == 1
        assert rc["feedback_total"] == 2
        assert rc["feedback_precision"] == 50.0


class TestEvaluateThresholds:

    def test_healthy_db_no_alerts(self, tmp_path):
        p = _db_path(tmp_path)
        # 命中良好、无膨胀、注入率高
        _make_db(p,
            chunks=[("git:x", 0.9, 5), ("git:x", 0.8, 3), ("git:x", 0.7, 2)],
            recall=[("git:x", 3, 1, "useful")] * 10,
            observability={"shadow_traces": 100})
        r = H.run(p)
        assert r["healthy"] is True
        assert r["alerts"] == []

    def test_cold_chunk_alert(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p, [("x", 0.5, 0)] * 8 + [("x", 0.5, 1)] * 2)
        r = H.run(p)
        assert any("冷 chunk" in a for a in r["alerts"])
        assert r["healthy"] is False

    def test_namespace_mismatch_alert(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p,
            chunks=[("workspace", 0.9, 0)] * 15,
            recall=[("git:y", 2, 1, "useful")] * 5)
        r = H.run(p)
        assert any("命名空间错位" in a for a in r["alerts"])

    def test_observability_bloat_alert(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p, [("x", 0.5, 1)], observability={"shadow_traces": 6000})
        r = H.run(p)
        assert any("shadow_traces" in a for a in r["alerts"])

    def test_low_inject_rate_alert(self, tmp_path):
        p = _db_path(tmp_path)
        _make_db(p,
            chunks=[("git:x", 0.5, 2)],
            recall=[("git:x", 3, 0, None)] * 20)  # 全有候选但0注入
        r = H.run(p)
        assert any("注入率" in a for a in r["alerts"])
