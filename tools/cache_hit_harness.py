#!/usr/bin/env python3
"""
cache_hit_harness.py — memory-os 缓存命中监测 harness（常规监测 case）

遵循「Code is Harness」原则：确定性的指标采集 + 阈值判断写成代码，
AI 只在需要时解读异常。可被 cron / 手动 / SessionStop hook 触发。

监测三个维度（对应 A/B/C 诊断）：
  A. 空间健康 —— DB 体积 vs 知识本体占比，观测表行数膨胀
  B. Chunk 命中率 —— access_count 分布，冷 chunk 占比，命名空间错位检测
  C. Recall 闭环 —— 检索请求的候选率/注入率/用户反馈精度（端到端命中）

输出：JSON（机器可读，供 dashboard/cron 消费）+ 阈值告警列表。
退出码：0=健康，1=有告警（便于 cron/CI 判断）。

用法：
  python3 tools/cache_hit_harness.py                # 人类可读报告
  python3 tools/cache_hit_harness.py --json         # JSON 输出
  python3 tools/cache_hit_harness.py --db /path/to/store.db
"""
import sqlite3
import json
import os
import sys
import argparse
from pathlib import Path


# ── 阈值配置（确定性判断的边界，集中定义便于调整）──────────────────────────
THRESHOLDS = {
    "cold_chunk_pct_warn": 50.0,        # 冷 chunk(access=0) 占比 > 此值告警
    "bloat_ratio_warn": 10.0,           # DB体积 / 知识本体体积 > 此值告警（观测表膨胀）
    "observability_rows_warn": 5000,    # 单张观测表行数 > 此值告警
    "inject_rate_low_warn": 15.0,       # recall 注入率 < 此值告警（检索到却大量没注入）
    "feedback_precision_warn": 60.0,    # useful / 有反馈 < 此值告警（注入了但用户嫌没用）
    "namespace_mismatch_warn": 10,      # 写入 project 与检索 project 错位的高价值冷 chunk 数
    "fresh_grace_days": 7,              # 新写入 chunk 的"信用期":此期内 access=0 不算孤儿
}

# 观测表（非知识本体，应受 TTL/上限约束）
_OBSERVABILITY_TABLES = [
    "shadow_traces", "session_focus", "checkpoints", "replay_events",
    "priming_state", "tool_patterns", "hook_txn_log", "dmesg",
    "entity_edges", "entity_map", "chunk_coactivation",
]


def _default_db() -> str:
    """定位活跃主库。优先 env，否则常见路径。"""
    env = os.environ.get("MEMORY_OS_DB")
    if env and Path(env).exists():
        return env
    for cand in (
        Path.home() / ".claude" / "memory-os" / "store.db",
        Path.home() / ".memory-os" / "store.db",
    ):
        if cand.exists():
            return str(cand)
    raise SystemExit("找不到 store.db，请用 --db 指定")


def _table_exists(c, name) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def collect_space(c) -> dict:
    """A: 空间健康 —— 体积拆解 + 膨胀比。"""
    ps = c.execute("PRAGMA page_size").fetchone()[0]
    pc = c.execute("PRAGMA page_count").fetchone()[0]
    fl = c.execute("PRAGMA freelist_count").fetchone()[0]
    total_mb = ps * pc / 1e6
    free_mb = ps * fl / 1e6

    # 知识本体 vs 观测表行数
    chunk_rows = c.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
    obs = {}
    for t in _OBSERVABILITY_TABLES:
        if _table_exists(c, t):
            obs[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]

    # 体积按表（dbstat 若可用）。单独查 memory_chunks 体积（可能不在 Top10）。
    by_table = {}
    chunk_mb = None
    try:
        for name, sz in c.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 10"
        ):
            by_table[name] = round(sz / 1e6, 3)
        row = c.execute(
            "SELECT SUM(pgsize) FROM dbstat WHERE name='memory_chunks'"
        ).fetchone()
        if row and row[0]:
            chunk_mb = round(row[0] / 1e6, 3)
    except Exception:
        # dbstat 不可用：用行数 × 估算字节兜底（粗略，仅用于膨胀比量级判断）
        pass

    bloat_ratio = round(total_mb / chunk_mb, 1) if chunk_mb else None

    return {
        "total_mb": round(total_mb, 2),
        "free_mb": round(free_mb, 2),
        "free_pct": round(100 * fl / pc, 1) if pc else 0,
        "chunk_rows": chunk_rows,
        "chunk_body_mb": chunk_mb,
        "bloat_ratio": bloat_ratio,
        "observability_rows": obs,
        "top_tables_mb": by_table,
    }


def collect_chunk_hits(c) -> dict:
    """B: chunk 命中率 —— access 分布 + 冷 chunk + 命名空间错位。"""
    tot = c.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
    if tot == 0:
        return {"total": 0}
    zero = c.execute(
        "SELECT COUNT(*) FROM memory_chunks WHERE COALESCE(access_count,0)=0"
    ).fetchone()[0]
    hit = tot - zero

    dist = {}
    for lo, hi, lab in [(0, 0, "cold"), (1, 2, "1-2"), (3, 5, "3-5"),
                        (6, 10, "6-10"), (11, 10**9, "11+")]:
        dist[lab] = c.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE COALESCE(access_count,0) BETWEEN ? AND ?",
            (lo, hi)
        ).fetchone()[0]

    # per-project 命中率
    by_project = []
    for proj, n, h in c.execute(
        "SELECT project, COUNT(*) n, SUM(CASE WHEN COALESCE(access_count,0)>=1 THEN 1 ELSE 0 END) h "
        "FROM memory_chunks GROUP BY project ORDER BY n DESC LIMIT 15"
    ):
        by_project.append({
            "project": proj or "(空)", "chunks": n, "hit": h or 0,
            "hit_pct": round(100 * (h or 0) / n, 0),
        })

    # 命名空间错位检测：高 importance(>=0.8) 且 access=0 的冷 chunk —— 写了却查不到。
    # 关键修正：排除近 fresh_grace_days 天的新写入（它们 access=0 可能只是"还没被检索"，
    # 不是孤儿）。用 created_at 过滤，给新 chunk 信用期，避免误报活跃项目为错位。
    grace = THRESHOLDS["fresh_grace_days"]
    _aged = f"created_at < datetime('now', '-{grace} days')"
    namespace_mismatch = c.execute(
        f"SELECT COUNT(*) FROM memory_chunks "
        f"WHERE COALESCE(access_count,0)=0 AND importance>=0.8 AND {_aged}"
    ).fetchone()[0]
    # 新写入冷却中（access=0 高价值但仍在信用期）—— 与孤儿分开统计
    fresh_cooling = c.execute(
        f"SELECT COUNT(*) FROM memory_chunks "
        f"WHERE COALESCE(access_count,0)=0 AND importance>=0.8 AND NOT ({_aged})"
    ).fetchone()[0]

    # 只写从不检索的 project（命名空间错位的根因证据）：
    # 写入端有、检索端(recall_traces)无、且无近期写入(不是活跃新项目) → 真孤儿源
    write_only = []
    if _table_exists(c, "recall_traces"):
        written = {r[0] for r in c.execute("SELECT DISTINCT project FROM memory_chunks")}
        queried = {r[0] for r in c.execute("SELECT DISTINCT project FROM recall_traces")}
        for proj in (written - queried):
            # 该 project 是否有近期写入？有则视为活跃新项目，不算孤儿源
            recent = c.execute(
                f"SELECT COUNT(*) FROM memory_chunks WHERE project=? AND NOT ({_aged})",
                (proj,)
            ).fetchone()[0]
            if recent > 0:
                continue
            n = c.execute(
                f"SELECT COUNT(*) FROM memory_chunks WHERE project=? "
                f"AND COALESCE(access_count,0)=0 AND importance>=0.8 AND {_aged}", (proj,)
            ).fetchone()[0]
            if n > 0:
                write_only.append({"project": proj or "(空)", "high_value_cold": n})
        write_only.sort(key=lambda x: -x["high_value_cold"])

    return {
        "total": tot,
        "cold": zero,
        "cold_pct": round(100 * zero / tot, 1),
        "hit": hit,
        "hit_pct": round(100 * hit / tot, 1),
        "distribution": dist,
        "by_project": by_project,
        "high_value_cold": namespace_mismatch,   # 真错位嫌疑（已排除信用期内新写入）
        "fresh_cooling": fresh_cooling,           # 新写入冷却中（access=0 但还在信用期）
        "write_only_projects": write_only,        # 只写不检索且无近期写入（真孤儿源）
    }


def collect_recall_loop(c) -> dict:
    """C: recall 闭环 —— 候选率/注入率/反馈精度（端到端命中）。"""
    if not _table_exists(c, "recall_traces"):
        return {"traces": 0}
    rows = c.execute(
        "SELECT candidates_count, injected, user_feedback FROM recall_traces"
    ).fetchall()
    tot = len(rows)
    if tot == 0:
        return {"traces": 0}
    has_cand = sum(1 for r in rows if (r[0] or 0) > 0)
    injected = sum(1 for r in rows if r[1] == 1)
    fb_useful = sum(1 for r in rows if r[2] == "useful")
    fb_any = sum(1 for r in rows if r[2])

    return {
        "traces": tot,
        "candidate_rate": round(100 * has_cand / tot, 1),   # 检索命中率
        "inject_rate": round(100 * injected / tot, 1),       # 注入率
        "feedback_total": fb_any,
        "feedback_useful": fb_useful,
        "feedback_precision": round(100 * fb_useful / fb_any, 1) if fb_any else None,
    }


def evaluate(space, hits, recall) -> list:
    """确定性阈值判断 —— 返回告警列表（空=健康）。"""
    alerts = []
    T = THRESHOLDS

    if hits.get("total", 0) and hits["cold_pct"] > T["cold_chunk_pct_warn"]:
        alerts.append(f"冷 chunk 占比 {hits['cold_pct']}% > {T['cold_chunk_pct_warn']}% "
                      f"（{hits['cold']}/{hits['total']} 从未被检索命中）")

    if hits.get("high_value_cold", 0) > T["namespace_mismatch_warn"]:
        wo = hits.get("write_only_projects", [])
        evidence = ""
        if wo:
            top = wo[0]
            evidence = f"；主因 project '{top['project']}'({top['high_value_cold']}条) 只写从不检索"
        alerts.append(f"高价值冷 chunk {hits['high_value_cold']} 条（importance>=0.8 却 access=0）"
                      f"——写入/检索 project 命名空间错位{evidence}")

    if space.get("bloat_ratio") and space["bloat_ratio"] > T["bloat_ratio_warn"]:
        alerts.append(f"DB 膨胀比 {space['bloat_ratio']}x > {T['bloat_ratio_warn']}x "
                      f"（库 {space['total_mb']}MB / 知识本体 {space['chunk_body_mb']}MB，观测表占主导）")

    for t, n in space.get("observability_rows", {}).items():
        if n > T["observability_rows_warn"]:
            alerts.append(f"观测表 {t} 行数 {n} > {T['observability_rows_warn']}（缺 TTL/上限清理）")

    if recall.get("traces", 0):
        if recall["inject_rate"] < T["inject_rate_low_warn"]:
            alerts.append(f"recall 注入率 {recall['inject_rate']}% < {T['inject_rate_low_warn']}%"
                          f"（检索到候选但大量未注入，可能 gate 过严）")
        fp = recall.get("feedback_precision")
        if fp is not None and recall["feedback_total"] >= 5 and fp < T["feedback_precision_warn"]:
            alerts.append(f"反馈精度 {fp}% < {T['feedback_precision_warn']}%（注入内容用户嫌没用）")

    return alerts


def run(db_path: str) -> dict:
    c = sqlite3.connect(db_path)
    space = collect_space(c)
    hits = collect_chunk_hits(c)
    recall = collect_recall_loop(c)
    alerts = evaluate(space, hits, recall)
    c.close()
    return {
        "db": db_path,
        "space": space,
        "chunk_hits": hits,
        "recall_loop": recall,
        "alerts": alerts,
        "healthy": len(alerts) == 0,
    }


def _print_human(r: dict):
    s, h, rc = r["space"], r["chunk_hits"], r["recall_loop"]
    print(f"# memory-os 缓存命中监测  ({r['db']})\n")
    print("## A. 空间健康")
    print(f"  库体积 {s['total_mb']}MB | 知识本体 {s.get('chunk_body_mb')}MB | "
          f"膨胀比 {s.get('bloat_ratio')}x | 空闲 {s['free_pct']}%")
    obs = s.get("observability_rows", {})
    big = sorted(obs.items(), key=lambda x: -x[1])[:5]
    print("  观测表行数 (Top5): " + ", ".join(f"{t}={n}" for t, n in big))
    print()
    if h.get("total"):
        print("## B. Chunk 命中率")
        print(f"  总 {h['total']} | 命中 {h['hit']} ({h['hit_pct']}%) | "
              f"冷 {h['cold']} ({h['cold_pct']}%) | 真错位 {h['high_value_cold']} | "
              f"新写入冷却中 {h.get('fresh_cooling', 0)}")
        d = h["distribution"]
        print(f"  access 分布: cold={d['cold']} 1-2={d['1-2']} 3-5={d['3-5']} "
              f"6-10={d['6-10']} 11+={d['11+']}")
        worst = sorted([p for p in h["by_project"] if p["chunks"] >= 3],
                       key=lambda x: x["hit_pct"])[:3]
        if worst:
            print("  最低命中 project: " +
                  ", ".join(f"{p['project'][:24]}({p['hit_pct']:.0f}%/{p['chunks']}条)" for p in worst))
        wo = h.get("write_only_projects", [])
        if wo:
            print("  只写不检索 project (命名空间错位): " +
                  ", ".join(f"{p['project'][:24]}({p['high_value_cold']}条)" for p in wo[:4]))
        print()
    if rc.get("traces"):
        print("## C. Recall 闭环 (端到端)")
        print(f"  请求 {rc['traces']} | 候选率 {rc['candidate_rate']}% | "
              f"注入率 {rc['inject_rate']}% | 反馈精度 "
              f"{rc.get('feedback_precision')}% ({rc['feedback_useful']}/{rc['feedback_total']})")
        print()
    if r["alerts"]:
        print(f"## ⚠️ 告警 ({len(r['alerts'])})")
        for a in r["alerts"]:
            print(f"  - {a}")
    else:
        print("## ✅ 健康（无告警）")


def main():
    ap = argparse.ArgumentParser(description="memory-os 缓存命中监测 harness")
    ap.add_argument("--db", default=None, help="store.db 路径（默认自动定位活跃主库）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    db_path = args.db or _default_db()
    r = run(db_path)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        _print_human(r)

    sys.exit(0 if r["healthy"] else 1)


if __name__ == "__main__":
    main()
