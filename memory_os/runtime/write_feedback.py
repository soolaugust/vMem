"""
write_feedback.py — 写入-检索闭环反馈机制

OS 类比：Linux writeback + I/O accounting — 写入后立即验证可达性，
不可达的 page 标记为 unreclaimable 而非正常的 active/inactive。

核心理念：写入的知识如果未来检索不到，等于浪费存储+污染搜索空间。
在 write 时进行 retrievability 自检，闭环保证写入质量。

机制 1: Write-time retrievability check
  - 写入前用 chunk summary 做 FTS5 自查询
  - 如果 summary 的关键词在 FTS5 中无法产生有效 match，降级 importance

机制 2: Evidence attachment（证据附着）
  - quantitative_evidence 写入时自动找最相关的 decision chunk
  - 建立 SUPPORTS edge，检索 decision 时连带注入其证据

机制 3: Pin decay（pin 自动衰退）
  - pinned chunk 连续 N 天 ac=0，hard→soft→unpin

机制 4: Write throttle（写入节流）
  - project 近 7 天 recall=0 但仍在写入 → 提高写入 importance 阈值
"""

import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional


def check_retrievability(conn: sqlite3.Connection, summary: str,
                          project: str) -> float:
    """
    机制 1: 用 summary 的关键词做 FTS5 自查询，返回 retrievability score [0,1]。

    score=1.0: summary 中的词在 FTS5 索引中有大量候选（未来容易被检索到）
    score=0.0: summary 中的词在索引中完全无匹配（写入后等于死数据）

    不是查 chunk 本身（还没写入），而是查 summary 的 token 在现有索引中的
    覆盖度——如果用户提问包含类似词汇，能否触发 FTS5 匹配。
    """
    if not summary or len(summary) < 10:
        return 0.0

    # 提取 summary 的关键 token（跟 bm25.py 的 hybrid_tokenize 逻辑一致）
    tokens = set()
    # English words
    for m in re.finditer(r'[a-zA-Z][a-zA-Z0-9_]{2,}', summary):
        tokens.add(m.group().lower())
    # Chinese bigrams
    cn = re.sub(r'[^一-鿿]', '', summary)
    for i in range(len(cn) - 1):
        tokens.add(cn[i:i + 2])

    if not tokens:
        return 0.0

    # 取前 5 个最长的 token 作为探测词
    probe_tokens = sorted(tokens, key=len, reverse=True)[:5]

    # 对每个 token 检查 FTS5 中是否有匹配
    hit_count = 0
    for token in probe_tokens:
        try:
            escaped = token.replace('"', '""')
            row = conn.execute(
                f'SELECT COUNT(*) FROM memory_chunks_fts WHERE memory_chunks_fts MATCH \'"{escaped}"\'',
            ).fetchone()
            if row and row[0] > 0:
                hit_count += 1
        except Exception:
            pass

    return hit_count / len(probe_tokens)


def attach_evidence_to_decision(conn: sqlite3.Connection, evidence_chunk: dict,
                                 project: str) -> Optional[str]:
    """
    机制 2: 量化证据自动附着到最相关的 decision chunk。

    逻辑：用 evidence 的 summary 搜索同 project 的 decision chunk，
    找到最匹配的一条，建立 SUPPORTS edge。

    Returns: 被附着的 decision chunk_id，或 None
    """
    from memory_os.store.graph import ensure_graph_schema, add_edge, EdgeType

    summary = evidence_chunk.get("summary", "")
    if not summary:
        return None

    # 搜索同 project 的 decision chunks
    rows = conn.execute(
        """SELECT id, summary FROM memory_chunks
           WHERE project = ? AND chunk_type = 'decision'
           ORDER BY last_accessed DESC LIMIT 20""",
        (project,)
    ).fetchall()

    if not rows:
        return None

    # 简单 token overlap 找最匹配的 decision
    ev_tokens = set()
    for m in re.finditer(r'[a-zA-Z][a-zA-Z0-9_]{2,}', summary):
        ev_tokens.add(m.group().lower())
    cn = re.sub(r'[^一-鿿]', '', summary)
    for i in range(len(cn) - 1):
        ev_tokens.add(cn[i:i + 2])

    if not ev_tokens:
        return None

    best_id, best_score = None, 0.0
    for dec_id, dec_summary in rows:
        dec_tokens = set()
        for m in re.finditer(r'[a-zA-Z][a-zA-Z0-9_]{2,}', dec_summary or ""):
            dec_tokens.add(m.group().lower())
        cn_d = re.sub(r'[^一-鿿]', '', dec_summary or "")
        for i in range(len(cn_d) - 1):
            dec_tokens.add(cn_d[i:i + 2])
        if not dec_tokens:
            continue
        overlap = len(ev_tokens & dec_tokens) / max(len(ev_tokens | dec_tokens), 1)
        if overlap > best_score:
            best_score = overlap
            best_id = dec_id

    if best_id and best_score > 0.05:
        ensure_graph_schema(conn)
        try:
            add_edge(conn, evidence_chunk.get("id", ""), best_id,
                     EdgeType.RELATED, round(best_score, 2), "evidence_attach")
        except Exception:
            pass
        return best_id
    return None


def decay_stale_pins(conn: sqlite3.Connection, project: str,
                      hard_to_soft_days: int = 14,
                      soft_to_unpin_days: int = 28) -> dict:
    """
    机制 3: Pin 自动衰退。

    依据 apply_count（被「真正用上」）而非 access_count（仅「被召回」）。
    根因（2026-06-05）：access_count 会因被召回注入而增长，但召回≠应用——
    一条被反复注入却从不被引用的 pin 仍会"假装活跃"逃过衰退。改用 apply_count
    后，只有真正影响过输出的 pin 才保命，死知识的 pin 才会被如实回收。

    hard pin + apply_count=0 超过 14 天 → 降为 soft pin
    soft pin + apply_count=0 超过 28 天 → unpin

    Returns: {"hard_to_soft": N, "unpinned": N}
    """
    now = datetime.now(timezone.utc)
    results = {"hard_to_soft": 0, "unpinned": 0}

    try:
        rows = conn.execute(
            """SELECT cp.chunk_id, cp.pin_type, cp.pinned_at,
                      COALESCE(mc.apply_count, 0), mc.last_applied
               FROM chunk_pins cp
               JOIN memory_chunks mc ON mc.id = cp.chunk_id
               WHERE cp.project = ?""",
            (project,)
        ).fetchall()
    except Exception:
        return results

    for chunk_id, pin_type, pinned_at, apc, last_applied in rows:
        if apc > 0:
            continue  # 被真正应用过的 pin 不衰退（apply_count > 0）

        try:
            pin_age = (now - datetime.fromisoformat(pinned_at.replace("Z", "+00:00"))).days
        except Exception:
            continue

        if pin_type == "hard" and pin_age >= hard_to_soft_days:
            conn.execute(
                "UPDATE chunk_pins SET pin_type = 'soft' WHERE chunk_id = ? AND project = ?",
                (chunk_id, project)
            )
            results["hard_to_soft"] += 1
        elif pin_type == "soft" and pin_age >= soft_to_unpin_days:
            conn.execute(
                "DELETE FROM chunk_pins WHERE chunk_id = ? AND project = ?",
                (chunk_id, project)
            )
            results["unpinned"] += 1

    if results["hard_to_soft"] + results["unpinned"] > 0:
        conn.commit()
    return results


def get_write_throttle_factor(conn: sqlite3.Connection, project: str) -> float:
    """
    机制 4: 写入节流因子。

    如果 project 近 7 天 recall_hit=0 但有新 chunk 写入，
    返回 throttle factor < 1.0（提高写入 importance 阈值）。
    如果检索健康，返回 1.0（不节流）。

    Returns: float in [0.3, 1.0]
      1.0 = 检索健康，正常写入
      0.5 = 检索低效，提高写入门槛 2×
      0.3 = 检索完全无效，只写入 importance >= 0.9 的高价值 chunk
    """
    try:
        # 近 7 天检索命中数
        recall_hit = conn.execute(
            """SELECT COUNT(*) FROM recall_traces
               WHERE project = ? AND injected = 1
               AND timestamp > datetime('now', '-7 days')""",
            (project,)
        ).fetchone()[0]

        # 近 7 天写入数
        writes_7d = conn.execute(
            """SELECT COUNT(*) FROM memory_chunks
               WHERE project = ? AND created_at > datetime('now', '-7 days')""",
            (project,)
        ).fetchone()[0]
    except Exception:
        return 1.0

    if writes_7d == 0:
        return 1.0  # 没写入就不需要节流

    hit_rate = recall_hit / max(writes_7d, 1)

    if hit_rate >= 0.3:
        return 1.0  # 健康：30%+ 的写入最终被检索到
    elif hit_rate >= 0.1:
        return 0.7  # 轻度节流
    elif recall_hit > 0:
        return 0.5  # 中度节流
    else:
        return 0.3  # 重度节流：近乎无效的项目
