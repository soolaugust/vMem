"""
store_aggregate.py — Chunk Aggregation (hugepages)

OS 类比：Linux hugepages — 将多个连续 4KB 页合并为 2MB 大页，
减少 TLB 条目数，提升 hit rate。

同理：5 条相关 decision 分别注入 = 5 × 44 = 220 tokens。
聚合为 1 条 composite = ~80 tokens。信息密度提升 2.75×。

触发时机：SessionStart 子系统（与 DAMON/MGLRU 同批次）。
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional


def find_clusters(
    conn: sqlite3.Connection,
    project: str,
    min_cluster_size: int = 3,
    max_clusters: int = 5,
) -> list:
    """
    利用 chunk_edges 找到互连的 chunk 簇（connected components）。

    算法：Union-Find on chunk_edges within same project。
    只考虑 RELATED/COOCCURS/CAUSES/REQUIRES 边（排除 CONTRADICTS）。

    Returns:
        [{"chunk_ids": [...], "chunk_type": str, "summaries": [...]}, ...]
    """
    from memory_os.store.graph import ensure_graph_schema
    ensure_graph_schema(conn)

    rows = conn.execute(
        """SELECT e.from_id, e.to_id
           FROM chunk_edges e
           JOIN memory_chunks m1 ON m1.id = e.from_id AND m1.project = ?
           JOIN memory_chunks m2 ON m2.id = e.to_id AND m2.project = ?
           WHERE e.relation_type NOT IN ('contradicts')""",
        (project, project),
    ).fetchall()

    if not rows:
        return []

    # Union-Find
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for from_id, to_id in rows:
        parent.setdefault(from_id, from_id)
        parent.setdefault(to_id, to_id)
        union(from_id, to_id)

    # Group by root
    groups = {}
    for node in parent:
        root = find(node)
        groups.setdefault(root, []).append(node)

    # Filter by min size and fetch metadata
    clusters = []
    for root, chunk_ids in sorted(groups.items(), key=lambda x: -len(x[1])):
        if len(chunk_ids) < min_cluster_size:
            continue
        if len(clusters) >= max_clusters:
            break

        placeholders = ",".join("?" * len(chunk_ids))
        meta_rows = conn.execute(
            f"""SELECT id, summary, chunk_type, importance
                FROM memory_chunks
                WHERE id IN ({placeholders})
                ORDER BY importance DESC""",
            chunk_ids,
        ).fetchall()

        if not meta_rows:
            continue

        clusters.append({
            "chunk_ids": [r[0] for r in meta_rows],
            "chunk_type": meta_rows[0][2],
            "summaries": [(r[1] or "")[:80] for r in meta_rows],
            "max_importance": max(r[3] or 0.5 for r in meta_rows),
        })

    return clusters


def aggregate_cluster(
    conn: sqlite3.Connection,
    cluster: dict,
    project: str,
    max_bullets: int = 7,
) -> Optional[str]:
    """
    将一个 cluster 聚合为 composite chunk 并写入 DB。

    格式：
      "[聚合×N] {title}
       • point1
       • point2
       ..."

    原始 chunk 的 oom_adj += 100（降优先级但保留 drill-down 路径）。

    Returns:
        composite chunk 的 id，或 None（如果 cluster 无效）
    """
    from memory_os.store.vfs import insert_chunk, bump_chunk_version
    from memory_os.core.schema import MemoryChunk
    import json

    chunk_ids = cluster["chunk_ids"]
    summaries = cluster["summaries"][:max_bullets]
    chunk_type = cluster.get("chunk_type", "decision")
    max_imp = cluster.get("max_importance", 0.7)

    if not summaries:
        return None

    title = summaries[0]
    bullets = "\n".join(f"• {s}" for s in summaries[1:] if s)
    composite_summary = f"[聚合×{len(chunk_ids)}] {title}"
    composite_content = f"{composite_summary}\n{bullets}" if bullets else composite_summary

    now = datetime.now(timezone.utc).isoformat()
    c = MemoryChunk(
        chunk_type="composite",
        summary=composite_summary[:200],
        content=composite_content,
        importance=max_imp,
        project=project,
        created_at=now,
        updated_at=now,
        last_accessed=now,
        tags=chunk_ids[:max_bullets],
    )

    try:
        insert_chunk(conn, c.to_dict())
    except Exception:
        return None

    # 降级原始 chunk（不删除，保留 drill-down）
    for cid in chunk_ids:
        try:
            conn.execute(
                "UPDATE memory_chunks SET oom_adj = COALESCE(oom_adj, 0) + 100 WHERE id = ?",
                (cid,),
            )
        except Exception:
            pass

    conn.commit()
    return c.id


def aggregate_related_chunks(
    conn: sqlite3.Connection,
    project: str,
    min_cluster_size: int = 3,
    max_bullets: int = 7,
) -> list:
    """
    SessionStart 入口：扫描项目 chunk graph，聚合互连簇为 composite。

    Returns:
        [composite_chunk_id, ...]
    """
    clusters = find_clusters(conn, project, min_cluster_size)
    results = []
    for cluster in clusters:
        cid = aggregate_cluster(conn, cluster, project, max_bullets)
        if cid:
            results.append(cid)
    return results
