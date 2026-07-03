#!/usr/bin/env python3
"""
lesson → harness 晋升扫描器（缺口2：Capability Governor 自动化）

背景：
  CLAUDE.md 的 Capability Governor 要求「学到的能力必须固化到代码」，但「哪条 lesson 该
  固化成 harness」此前靠人脑三问判断。本脚本把它变成数据驱动的候选清单。

判据（全部确定性，零 LLM）：
  1. 高真实应用：apply_count >= MIN_APPLY（被模型实际用上，而非仅被召回）
  2. 高召回：access_count >= MIN_RECALL
  3. 流程性类型：chunk_type ∈ {decision, design_constraint, excluded_path}
     （这些是「该怎么做/不该怎么做」的可固化规则，区别于纯事实型知识）
  4. 跨 session 反复应用：被 >=2 个不同 session 应用过（排除单 session 循环刷量）

依赖 ROI 信号底座（apply_count / recall_traces.applied_ids_json），需先积累几周数据。

用法：
  python tools/harness_promotion_scan.py [--project PROJ] [--min-apply N] [--min-recall N] [--json]
  输出候选清单，由人/独立 AI 步骤决定是否真的固化成 harness 代码。**本脚本只读，无副作用。**
"""
import argparse
import json
import os
import sys
from collections import defaultdict

# 允许从 memory-os 根目录的模块 import
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

PROMOTABLE_TYPES = ("decision", "design_constraint", "excluded_path")
DEFAULT_MIN_APPLY = 3
DEFAULT_MIN_RECALL = 5
MIN_DISTINCT_SESSIONS = 2


def _distinct_apply_sessions(conn, project: str) -> dict:
    """解析 recall_traces.applied_ids_json，统计每个 chunk 被多少个不同 session 应用过。"""
    sess_by_chunk = defaultdict(set)
    try:
        rows = conn.execute(
            """SELECT session_id, applied_ids_json FROM recall_traces
               WHERE project=? AND applied_ids_json IS NOT NULL
                 AND applied_ids_json != '[]'""",
            (project,),
        ).fetchall()
    except Exception:
        return {}
    for sess, ids_json in rows:
        try:
            ids = json.loads(ids_json) if ids_json else []
        except Exception:
            continue
        for cid in ids:
            sess_by_chunk[cid].add(sess)
    return {cid: len(s) for cid, s in sess_by_chunk.items()}


def scan(project: str, min_apply: int, min_recall: int):
    from memory_os.store.api import open_db, ensure_schema

    conn = open_db()
    ensure_schema(conn)  # 确保 apply_count / applied_ids_json 列存在（老库惰性添加）

    _ph = ",".join("?" * len(PROMOTABLE_TYPES))
    try:
        rows = conn.execute(
            f"""SELECT id, chunk_type, summary,
                       COALESCE(access_count,0), COALESCE(apply_count,0)
                FROM memory_chunks
                WHERE project=?
                  AND COALESCE(apply_count,0) >= ?
                  AND COALESCE(access_count,0) >= ?
                  AND chunk_type IN ({_ph})
                ORDER BY COALESCE(apply_count,0) DESC, COALESCE(access_count,0) DESC""",
            (project, min_apply, min_recall, *PROMOTABLE_TYPES),
        ).fetchall()
    except Exception as e:
        conn.close()
        return {"error": f"query failed: {e}", "candidates": []}

    distinct = _distinct_apply_sessions(conn, project)

    candidates = []
    for cid, ctype, summary, ac, apc in rows:
        n_sess = distinct.get(cid, 0)
        if n_sess < MIN_DISTINCT_SESSIONS:
            continue  # 排除单 session 刷量
        candidates.append({
            "id": cid,
            "chunk_type": ctype,
            "summary": (summary or "")[:160],
            "access_count": ac,
            "apply_count": apc,
            "apply_ratio": round(apc / ac, 3) if ac else 0.0,
            "distinct_sessions": n_sess,
        })

    # 全库分布（供阈值校准）
    dist = conn.execute(
        """SELECT chunk_type, COUNT(*), MAX(COALESCE(apply_count,0)),
                  MAX(COALESCE(access_count,0))
           FROM memory_chunks WHERE project=? GROUP BY chunk_type""",
        (project,),
    ).fetchall()
    conn.close()

    return {
        "project": project,
        "thresholds": {"min_apply": min_apply, "min_recall": min_recall,
                       "min_distinct_sessions": MIN_DISTINCT_SESSIONS},
        "candidate_count": len(candidates),
        "candidates": candidates,
        "distribution": [
            {"chunk_type": d[0], "count": d[1], "max_apply": d[2], "max_recall": d[3]}
            for d in dist
        ],
    }


def main():
    ap = argparse.ArgumentParser(description="lesson→harness 晋升候选扫描（只读）")
    ap.add_argument("--project", default=None, help="项目 ID（默认自动解析当前目录）")
    ap.add_argument("--min-apply", type=int, default=DEFAULT_MIN_APPLY)
    ap.add_argument("--min-recall", type=int, default=DEFAULT_MIN_RECALL)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    project = args.project
    if not project:
        try:
            from memory_os.core.utils import resolve_project_id
            project = resolve_project_id(os.getcwd())
        except Exception:
            project = "unknown"

    result = scan(project, args.min_apply, args.min_recall)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if result.get("error"):
        print(f"❌ {result['error']}")
        return

    print(f"🔧 Harness 晋升候选 (project={result['project']})")
    print(f"   判据: apply>={args.min_apply} recall>={args.min_recall} "
          f"跨session>={MIN_DISTINCT_SESSIONS} 类型∈{PROMOTABLE_TYPES}")
    print(f"   候选数: {result['candidate_count']}\n")
    if not result["candidates"]:
        print("   暂无候选（数据未积累足够，或无高应用流程性知识）。")
    for c in result["candidates"]:
        print(f"   [{c['chunk_type']}] apply={c['apply_count']} recall={c['access_count']} "
              f"ratio={c['apply_ratio']} sessions={c['distinct_sessions']}")
        print(f"     {c['summary']}")
    print("\n   全库分布（校准阈值用）:")
    for d in result["distribution"]:
        print(f"     {d['chunk_type']:20s}: n={d['count']} max_apply={d['max_apply']} "
              f"max_recall={d['max_recall']}")


if __name__ == "__main__":
    main()
