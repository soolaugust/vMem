"""
dashboard_data.py — Memory OS Dashboard 数据导出

生成 JSON 数据供 dashboard.html 可视化使用。
包含：chunk 分布、graph 关系、session replay、compression metrics。

用法：python3 tools/dashboard_data.py [--project PROJECT] > dashboard_data.json
"""

import json
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_os.store.vfs_compat import open_db
from memory_os.store.graph import ensure_graph_schema
from memory_os.store.episodes import ensure_replay_schema
from memory_os.runtime.context.offload_compat import measure_compression

_DEFAULT_DB = os.path.expanduser("~/.claude/memory-os/store.db")


def get_chunk_distribution(conn, project=None):
    """chunk_type 分布统计。"""
    if project:
        rows = conn.execute(
            "SELECT chunk_type, COUNT(*), AVG(importance), AVG(access_count) "
            "FROM memory_chunks WHERE project=? GROUP BY chunk_type",
            (project,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT chunk_type, COUNT(*), AVG(importance), AVG(access_count) "
            "FROM memory_chunks GROUP BY chunk_type"
        ).fetchall()
    return [
        {"type": r[0], "count": r[1],
         "avg_importance": round(r[2] or 0, 3),
         "avg_access_count": round(r[3] or 0, 1)}
        for r in rows
    ]


def get_graph_edges(conn, project=None, limit=100):
    """图边数据（供 force-directed graph 可视化）。"""
    ensure_graph_schema(conn)
    if project:
        rows = conn.execute(
            """SELECT e.from_id, e.to_id, e.relation_type, e.weight,
                      m1.summary, m2.summary
               FROM chunk_edges e
               JOIN memory_chunks m1 ON m1.id = e.from_id AND m1.project = ?
               JOIN memory_chunks m2 ON m2.id = e.to_id
               ORDER BY e.weight DESC LIMIT ?""",
            (project, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.from_id, e.to_id, e.relation_type, e.weight,
                      m1.summary, m2.summary
               FROM chunk_edges e
               JOIN memory_chunks m1 ON m1.id = e.from_id
               JOIN memory_chunks m2 ON m2.id = e.to_id
               ORDER BY e.weight DESC LIMIT ?""",
            (limit,)
        ).fetchall()

    nodes = {}
    edges = []
    for r in rows:
        from_id, to_id = r[0][:8], r[1][:8]
        nodes[from_id] = {"id": from_id, "label": (r[4] or "")[:20]}
        nodes[to_id] = {"id": to_id, "label": (r[5] or "")[:20]}
        edges.append({
            "source": from_id, "target": to_id,
            "type": r[2], "weight": round(r[3], 2)
        })

    return {"nodes": list(nodes.values()), "edges": edges}


def get_timeline_stats(conn, days=30):
    """最近 N 天的 chunk 写入/访问时间线。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT DATE(created_at) as day, COUNT(*) as created
           FROM memory_chunks WHERE created_at > ?
           GROUP BY day ORDER BY day""",
        (cutoff,)
    ).fetchall()
    return [{"date": r[0], "created": r[1]} for r in rows if r[0]]


def get_replay_summary(conn, limit=20):
    """最近的 replay 事件摘要。"""
    ensure_replay_schema(conn)
    rows = conn.execute(
        """SELECT session_id, event_type, timestamp, duration_ms
           FROM replay_events
           ORDER BY timestamp DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    return [
        {"session": r[0][:8], "event": r[1], "time": r[2], "ms": r[3]}
        for r in rows
    ]


def get_health_metrics(conn):
    """系统健康指标。"""
    total = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
    zero_ac = conn.execute(
        "SELECT COUNT(*) FROM memory_chunks WHERE access_count = 0"
    ).fetchone()[0]

    try:
        edge_count = conn.execute("SELECT COUNT(*) FROM chunk_edges").fetchone()[0]
    except Exception:
        edge_count = 0

    try:
        pinned = conn.execute("SELECT COUNT(*) FROM chunk_pins").fetchone()[0]
    except Exception:
        pinned = 0

    try:
        swapped = conn.execute("SELECT COUNT(*) FROM swap_chunks").fetchone()[0]
    except Exception:
        swapped = 0

    return {
        "total_chunks": total,
        "zero_access_count": zero_ac,
        "zero_access_pct": round(zero_ac / max(total, 1) * 100, 1),
        "graph_edges": edge_count,
        "pinned_chunks": pinned,
        "swapped_chunks": swapped,
    }


def get_compression_demo():
    """Context offload 压缩效果演示。"""
    sample_chunks = [
        {"id": f"demo_{i}", "chunk_type": "decision",
         "summary": f"架构决策{i}：基于性能基准测试选择 SQLite WAL 作为持久化方案",
         "importance": 0.7, "raw_snippet": "实测数据显示..." * 5}
        for i in range(5)
    ]
    return measure_compression(sample_chunks)


def export_all(project=None):
    """导出全部 dashboard 数据。"""
    db_path = os.environ.get("MEMORY_OS_DB", _DEFAULT_DB)
    if not os.path.exists(db_path):
        return {"error": f"DB not found: {db_path}"}

    conn = open_db(db_path)
    try:
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project": project or "all",
            "distribution": get_chunk_distribution(conn, project),
            "graph": get_graph_edges(conn, project),
            "timeline": get_timeline_stats(conn),
            "replay": get_replay_summary(conn),
            "health": get_health_metrics(conn),
            "compression": get_compression_demo(),
        }
    finally:
        conn.close()
    return data


def get_sessions(project=None, limit=50):
    """最近 session 列表，UNION session_episodes + replay_events。

    背景：session_episodes 和 replay_events 的 session_id 体系不同（episodes 用完整 UUID，
    replay 用 16 字符截断 ID），两者无交集。次方法返回两个数据源的所有 session，
    并附 replay_event_count 字段，前端据此判断是否有重放数据。
    """
    from memory_os.store.episodes import ensure_episodes_schema, ensure_replay_schema, get_recent_episodes
    db_path = os.environ.get("MEMORY_OS_DB", _DEFAULT_DB)
    if not os.path.exists(db_path):
        return []
    conn = open_db(db_path)
    try:
        ensure_episodes_schema(conn)
        ensure_replay_schema(conn)

        # 1) session_episodes 来源
        if project:
            episodes = get_recent_episodes(conn, project, limit=limit)
        else:
            rows = conn.execute(
                "SELECT session_id, ended_at, summary, project FROM session_episodes "
                "ORDER BY ended_at DESC LIMIT ?", (limit,)
            ).fetchall()
            episodes = [
                {"session_id": r[0], "ended_at": r[1], "summary": r[2], "project": r[3]}
                for r in rows
            ]

        # 2) replay_events 来源（按 session_id 聚合，取最大 timestamp 作 ended_at）
        replay_rows = conn.execute(
            "SELECT session_id, MAX(timestamp) as last_ts, COUNT(*) as cnt "
            "FROM replay_events GROUP BY session_id ORDER BY last_ts DESC LIMIT ?",
            (limit,)
        ).fetchall()
        replay_sessions = {
            r[0]: {"session_id": r[0], "ended_at": r[1], "summary": "", "project": "",
                   "replay_event_count": r[2]}
            for r in replay_rows
        }

        # 3) 给 episodes 也补 replay_event_count（按 session_id 完整匹配 + 16 字符前缀匹配）
        seen_ids = set()
        merged = []
        for ep in episodes:
            sid = ep["session_id"]
            cnt = 0
            r = conn.execute(
                "SELECT COUNT(*) FROM replay_events WHERE session_id = ? OR session_id = ?",
                (sid, sid[:16])
            ).fetchone()
            if r:
                cnt = r[0]
            ep["replay_event_count"] = cnt
            merged.append(ep)
            seen_ids.add(sid)

        # 4) 把没在 episodes 里出现的 replay-only sessions 也加上
        for sid, item in replay_sessions.items():
            # 排除已通过前缀匹配的
            if any(s[:16] == sid[:16] for s in seen_ids):
                continue
            merged.append(item)

        # 按 ended_at 倒序
        merged.sort(key=lambda x: x.get("ended_at") or "", reverse=True)
        return merged[:limit]
    finally:
        conn.close()


def get_session_replay(session_id):
    """单 session 的完整 replay 事件流（追溯/重放数据）。

    解析策略（按优先级尝试）：
      1) 完整精确匹配
      2) 前缀匹配（处理传入完整 UUID 但 replay_events 存的是 16 字符截断 ID 的情况）
      3) 短 ID 前缀匹配
    """
    from memory_os.store.episodes import ensure_replay_schema, replay_session
    db_path = os.environ.get("MEMORY_OS_DB", _DEFAULT_DB)
    if not os.path.exists(db_path):
        return {"error": f"DB not found: {db_path}"}
    conn = open_db(db_path)
    try:
        ensure_replay_schema(conn)
        sid = (session_id or "").strip()
        if not sid:
            return []

        # 1) 完整匹配
        row = conn.execute(
            "SELECT DISTINCT session_id FROM replay_events WHERE session_id = ? LIMIT 1",
            (sid,)
        ).fetchone()
        if not row:
            # 2) 前缀匹配（双向）：传入 ID 是已存 ID 的前缀，或已存 ID 是传入 ID 的前缀
            for prefix_len in (16, 8):
                test = sid[:prefix_len]
                row = conn.execute(
                    "SELECT DISTINCT session_id FROM replay_events "
                    "WHERE session_id = ? OR session_id LIKE ? LIMIT 1",
                    (test, test + "%")
                ).fetchone()
                if row:
                    break
        if row:
            sid = row[0]
        events = replay_session(conn, sid)
    finally:
        conn.close()
    return events


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=None)
    parser.add_argument("--cmd", default="export",
                        choices=["export", "sessions", "replay"],
                        help="export=完整 dashboard 数据 (默认), sessions=session 列表, replay=单 session 事件")
    parser.add_argument("--session", default=None, help="replay 命令需要的 session_id")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    if args.cmd == "sessions":
        out = get_sessions(args.project, args.limit)
    elif args.cmd == "replay":
        if not args.session:
            out = {"error": "--session required for replay"}
        else:
            out = get_session_replay(args.session)
    else:
        out = export_all(args.project)
    print(json.dumps(out, ensure_ascii=False, indent=2))
