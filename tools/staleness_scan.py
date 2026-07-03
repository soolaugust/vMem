#!/usr/bin/env python3
"""
知识时效性巡检（真值信号增密 · 缺口5）

背景：
  memory-os 的过期机制 detect_conflict/supersede_chunk 只在「写新 chunk 且含否定词」时触发。
  不含否定词的代际更替（如"OS4 用 os-cpu"未提及旧的 metis）→ 旧知识永远 verified、永不过期。
  stability 衰减的是「记不记得」，没有任何东西衰减「还对不对」。

  本脚本主动巡检长期失效知识，用时间+应用硬信号（零 LLM），降其 confidence 让检索时降权。
  不删除、不硬覆盖——可恢复。复用 confidence 通道（retriever/scorer 已读）。

判据（确定性）：
  C1 长期未应用：apply_count=0 AND access_count>=N AND created_at 早于 D 天
  C2 同 topic 被超越：实体交集存在更晚 created_at 且 apply_count>=2 的同实体 chunk → 旧的存疑

默认 dry-run（只列候选）；--apply 才落库降 confidence。

用法：
  python tools/staleness_scan.py [--project P] [--min-access N] [--min-age-days D] [--apply] [--json]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

DEFAULT_MIN_ACCESS = 5
DEFAULT_MIN_AGE_DAYS = 30
STALE_CONF_DELTA = -0.15
WARMUP_MIN_MEASURED = 20   # 与 RTMC warmup 守卫同口径：apply 信号未预热则跳过 C1/C2


def _age_days(iso_str: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def scan(project: str, min_access: int, min_age_days: int, do_apply: bool):
    from memory_os.store.api import open_db, ensure_schema
    from memory_os.store.vfs_compat import _extract_key_entities, update_confidence

    conn = open_db()
    ensure_schema(conn)
    now = datetime.now(timezone.utc)

    # 预热守卫：apply_count 信号未积累足够则不下结论（避免冷启动误杀全库）
    try:
        measured = conn.execute(
            "SELECT COUNT(*) FROM recall_traces WHERE project=? AND applied_ids_json IS NOT NULL",
            (project,),
        ).fetchone()[0]
    except Exception:
        measured = 0
    if measured < WARMUP_MIN_MEASURED:
        conn.close()
        return {"project": project, "warmup": False,
                "measured_traces": measured, "warmup_min": WARMUP_MIN_MEASURED,
                "note": "apply 信号未预热，跳过时效巡检（避免冷启动误杀）", "candidates": []}

    try:
        rows = conn.execute(
            """SELECT id, summary, created_at,
                      COALESCE(access_count,0), COALESCE(apply_count,0),
                      COALESCE(confidence_score,0.7), COALESCE(verification_status,'pending')
               FROM memory_chunks
               WHERE project=? AND COALESCE(chunk_state,'ACTIVE')='ACTIVE'""",
            (project,),
        ).fetchall()
    except Exception as e:
        conn.close()
        return {"error": f"query failed: {e}", "candidates": []}

    # 预计算实体集合（C2 用）
    parsed = []
    for cid, summ, ca, ac, apc, cs, vs in rows:
        parsed.append({
            "id": cid, "summary": summ or "", "created_at": ca or "",
            "access_count": ac, "apply_count": apc, "confidence": cs,
            "vs": vs, "entities": _extract_key_entities(summ or ""),
            "age": _age_days(ca or "", now),
        })

    candidates = []
    for c in parsed:
        if c["vs"] == "disputed":
            continue  # 已被标，不重复
        reason = None
        # C1 长期未应用
        if c["apply_count"] == 0 and c["access_count"] >= min_access and c["age"] >= min_age_days:
            reason = f"C1长期未应用(ac={c['access_count']},apply=0,age={c['age']:.0f}d)"
        # C2 同 topic 被更新的高应用 chunk 超越
        if not reason and c["entities"]:
            for other in parsed:
                if other["id"] == c["id"] or not other["entities"]:
                    continue
                inter = c["entities"] & other["entities"]
                if (len(inter) >= 2 and other["apply_count"] >= 2
                        and other["created_at"] > c["created_at"]):
                    reason = f"C2被超越(by {other['id'][:8]},共享实体{len(inter)})"
                    break
        if reason:
            candidates.append({
                "id": c["id"], "summary": c["summary"][:120], "reason": reason,
                "access_count": c["access_count"], "apply_count": c["apply_count"],
                "confidence": round(c["confidence"], 3), "age_days": round(c["age"], 1),
            })

    applied = 0
    if do_apply and candidates:
        for cand in candidates:
            try:
                update_confidence(conn, cand["id"], STALE_CONF_DELTA, "stale_scan")
                applied += 1
            except Exception:
                pass
        conn.commit()
    conn.close()

    return {
        "project": project, "warmup": True,
        "thresholds": {"min_access": min_access, "min_age_days": min_age_days,
                       "conf_delta": STALE_CONF_DELTA},
        "mode": "apply" if do_apply else "dry-run",
        "candidate_count": len(candidates),
        "applied_count": applied,
        "candidates": candidates,
    }


def main():
    ap = argparse.ArgumentParser(description="知识时效性巡检（默认 dry-run）")
    ap.add_argument("--project", default=None)
    ap.add_argument("--min-access", type=int, default=DEFAULT_MIN_ACCESS)
    ap.add_argument("--min-age-days", type=int, default=DEFAULT_MIN_AGE_DAYS)
    ap.add_argument("--apply", action="store_true", help="落库降 confidence（默认仅 dry-run）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    project = args.project
    if not project:
        try:
            from memory_os.core.utils import resolve_project_id
            project = resolve_project_id(os.getcwd())
        except Exception:
            project = "unknown"

    result = scan(project, args.min_access, args.min_age_days, args.apply)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if result.get("error"):
        print(f"❌ {result['error']}")
        return
    if not result.get("warmup", True):
        print(f"⏳ {result['note']}（已测量 {result['measured_traces']}/{result['warmup_min']}）")
        return

    print(f"🕰️  知识时效性巡检 (project={result['project']}) [{result['mode']}]")
    print(f"   判据: C1(apply=0,access>={args.min_access},age>={args.min_age_days}d) | C2(同topic被高应用chunk超越)")
    print(f"   候选: {result['candidate_count']}"
          + (f"，已降权: {result['applied_count']}" if result['mode'] == 'apply' else "（dry-run，未落库）"))
    for c in result["candidates"]:
        print(f"   [{c['reason']}] conf={c['confidence']}")
        print(f"     {c['summary']}")
    if result["candidate_count"] and result["mode"] == "dry-run":
        print("\n   人工核对候选后，加 --apply 落库降 confidence（-0.15，可恢复）。")


if __name__ == "__main__":
    main()
