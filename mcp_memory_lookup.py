#!/usr/bin/env python3
"""
memory_lookup MCP Server — 推理中断模型（Inference Interrupt Model）

迭代 99：AI 主动缺页中断（Active Page Fault）

OS 类比：
  传统 batch I/O（polling）：进程定期检查设备状态（busy wait）
  中断模型（interrupt）：设备就绪时主动通知 CPU（interrupt → ISR → resume）

  memory-os 演进路径：
    v1（轮询）：UserPromptSubmit hook → batch inject top-K chunks → 推理开始
      问题：固定 top-K 无法按推理中途发现的新需求动态调整
      类比：进程开始前读取所有可能需要的数据（预读 + 批处理）

    v2（中断）：AI 推理中途感知到知识缺口 → 调用 memory_lookup() → 按需注入
      优势：
        1. 查询粒度更细（"SCX_ENQ_IMMED 的约束" vs 宽泛的初始 prompt）
        2. 多轮检索（发现 A → A 引导发现 B）
        3. 零冗余（只注入真正需要的知识）
      类比：demand paging（需要时才加载页面，而不是启动时全部加载）

使用方式：
  AI 在推理时遇到知识缺口时，直接调用此 MCP 工具：
    memory_lookup("SCX_ENQ_IMMED 约束")
    memory_lookup("checkpoint_restore 返回格式")
    memory_lookup("BM25 scorer 参数", top_k=5)

  工具返回格式化的检索结果，AI 继续推理。

架构：
  FastMCP stdio server → 通过 MCP 协议与 Claude Code 通信
  检索管道：fts_search → retrieval_score → format → return
  DB 连接：只读连接（避免与 writer 锁竞争）
"""

import sys
import asyncio
import os
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone, timedelta

import anyio

# FastMCP — Model Context Protocol Python SDK
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

# ── AIOS memory-os 路径 ───────────────────────────────────────────────────────
_MOS_ROOT = Path(__file__).resolve().parent
if str(_MOS_ROOT) not in sys.path:
    sys.path.insert(0, str(_MOS_ROOT))

from store_vfs import fts_search, open_db, pin_chunk, unpin_chunk, is_pinned, get_pinned_chunks, ensure_schema
from scorer import retrieval_score, recency_score
from utils import resolve_project_id
from config import get as _sysctl

# ── MCP Server 初始化 ────────────────────────────────────────────────────────
mcp = FastMCP(
    name="memory-os",
    instructions=(
        "AIOS Memory OS — 主动知识检索工具。\n"
        "当你在推理过程中发现需要查找特定知识时（不确定某个决策/设计/约束），\n"
        "调用 memory_lookup 按需检索。这是推理中断模型（demand paging）：\n"
        "不需要在推理前预载所有知识，在需要时精确查询即可。\n\n"
        "适用场景：\n"
        "  - '我不确定这个函数的参数格式' → memory_lookup('函数名 参数')\n"
        "  - '上次关于这个模块的决策是什么' → memory_lookup('模块名 决策')\n"
        "  - '这里有什么设计约束' → memory_lookup('约束 限制', chunk_types=['design_constraint'])\n"
    )
)

# ── DB 连接（只读，避免锁竞争）──────────────────────────────────────────────
def _open_readonly() -> sqlite3.Connection:
    """打开只读 DB 连接（类比 O_RDONLY，零写锁竞争）"""
    db_path = Path.home() / ".claude" / "memory-os" / "store.db"
    env_path = os.environ.get("MEMORY_OS_DB")
    if env_path:
        db_path = Path(env_path)
    if not db_path.exists():
        raise FileNotFoundError(f"store.db not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _format_chunk(chunk: dict, rank: int) -> str:
    """格式化单个 chunk 为可读文本（注入格式与 retriever 保持一致）"""
    chunk_type = chunk.get("chunk_type", "")
    summary = chunk.get("summary", "").strip()
    content = chunk.get("content", "").strip()
    importance = chunk.get("importance", 0.5)

    # chunk_type 符号
    type_icons = {
        "decision": "💡",
        "reasoning_chain": "🔗",
        "design_constraint": "⚠️",
        "prompt_context": "📋",
        "code_snippet": "💻",
        "task_state": "📌",
        "quantitative_evidence": "📊",
        "causal_chain": "🔗",
    }
    icon = type_icons.get(chunk_type, "📎")
    if chunk_type == "semantic_memory":
        icon = "🌐"  # 语义记忆：跨项目通用知识

    # 语义层来源标记
    semantic_tag = " [跨项目语义记忆]" if chunk.get("_from_semantic_layer") else ""
    chunk_id = chunk.get("id", "")
    id_tag = f" id={chunk_id}" if chunk_id else ""
    lines = [f"{icon} [{rank}] [{chunk_type}]{semantic_tag} (importance={importance:.2f}){id_tag}"]
    lines.append(f"  {summary}")
    if content and len(content) < 500:
        lines.append(f"  ---")
        lines.append(f"  {content[:400]}{'...' if len(content) > 400 else ''}")

    return "\n".join(lines)


# ── MCP 工具定义 ─────────────────────────────────────────────────────────────

@mcp.tool()
def memory_lookup(
    query: str,
    top_k: int = 5,
    chunk_types: list[str] | None = None,
    project: str | None = None,
) -> str:
    """
    在 AIOS 知识库中主动检索相关记忆（推理中断 / demand paging）。

    当你推理时发现需要某类知识但不确定时，调用此工具：
    - 上次关于某模块的决策
    - 某功能的已知设计约束
    - 某段代码的参数格式
    - 之前总结的性能数据

    Args:
        query: 查询字符串（自然语言或关键词均可）
        top_k: 返回结果数量（默认 5）
        chunk_types: 可选，过滤 chunk 类型（如 ["design_constraint", "decision"]）
        project: 可选，指定项目 ID（默认自动推断）

    Returns:
        格式化的检索结果，包含 summary 和 content 摘要
    """
    if not query or not query.strip():
        return "❌ 查询为空，请提供检索关键词。"

    max_top_k = int(_sysctl("mcp.memory_lookup_top_k_max"))
    try:
        top_k = max(1, min(int(top_k), max_top_k))
    except (TypeError, ValueError):
        return "❌ top_k 必须是整数。"
    response_budget = int(_sysctl("mcp.memory_lookup_max_response_chars"))

    # 推断项目 ID
    if not project:
        try:
            project = resolve_project_id()
        except Exception:
            project = "default"

    try:
        conn = _open_readonly()
    except FileNotFoundError as e:
        return f"❌ 知识库未初始化：{e}"

    try:
        # ── FTS5 检索 ─────────────────────────────────────────────────────────
        ct_tuple = tuple(chunk_types) if chunk_types else None
        candidates = fts_search(conn, query, project, top_k=top_k * 3, chunk_types=ct_tuple)

        if not candidates:
            # fallback：也尝试 global 层
            global_candidates = fts_search(conn, query, "global", top_k=top_k, chunk_types=ct_tuple)
            candidates = global_candidates

        # ── 语义记忆层激活（跨项目通用知识，__semantic__ project）──────────────
        # OS 类比：TLB 命中后补充 shared memory page — 语义记忆跨 project 共享，
        # 任何查询都自动激活相关的通用知识，不受 project 边界限制。
        _SEMANTIC_PROJECT = "__semantic__"
        try:
            semantic_ct = tuple(["semantic_memory"]) if not chunk_types else ct_tuple
            semantic_candidates = fts_search(
                conn, query, _SEMANTIC_PROJECT,
                top_k=max(2, top_k // 2),
                chunk_types=semantic_ct,
            )
            if semantic_candidates:
                # 标记来源，避免与 project 内 chunk 混淆
                for c in semantic_candidates:
                    c["_from_semantic_layer"] = True
                candidates = candidates + semantic_candidates
        except Exception:
            pass  # 语义层激活失败不影响主检索

        if not candidates:
            return f"💭 未找到与 '{query}' 相关的记忆。\n  提示：知识库可能还没有这方面的内容，或查询词可以换个角度。"

        # ── 评分排序 ────────────────────────────────────────────────────────
        scored = []
        for c in candidates:
            fts_rank = c.get("fts_rank", 0.0) or 0.0
            score = retrieval_score(
                relevance=min(fts_rank / 10.0, 1.0),  # normalize fts_rank
                importance=c.get("importance", 0.5),
                last_accessed=c.get("last_accessed", ""),
                created_at=c.get("created_at", ""),
                access_count=c.get("access_count", 0),
            )
            scored.append((score, c))

        # design_constraint 优先（类比 mlock 保护页优先服务缺页中断）
        scored.sort(key=lambda x: (
            -(1 if x[1].get("chunk_type") == "design_constraint" else 0),
            -x[0]
        ))

        top_results = scored[:top_k]

        # ── 格式化输出（RSS budget aware）────────────────────────────────────────
        query_label = query[:160] + ("…" if len(query) > 160 else "")
        lines = []
        used_chars = 0
        budget_exhausted = False

        def _append_budgeted(text: str) -> bool:
            nonlocal used_chars, budget_exhausted
            projected = used_chars + len(text) + 1
            if projected > response_budget:
                budget_exhausted = True
                return False
            lines.append(text)
            used_chars = projected
            return True

        _append_budgeted(f"🔍 memory_lookup: '{query_label}' → {len(top_results)} 条结果\n")

        # 分离约束和普通知识（类比 retriever 的强制注入逻辑）
        constraints = [(s, c) for s, c in top_results if c.get("chunk_type") == "design_constraint"]
        others = [(s, c) for s, c in top_results if c.get("chunk_type") != "design_constraint"]

        if constraints:
            _append_budgeted("【已知约束（系统级设计限制）】")
            for i, (score, c) in enumerate(constraints, 1):
                if not _append_budgeted(_format_chunk(c, i)):
                    break
                if not _append_budgeted(f"  (score={score:.3f})"):
                    break
            _append_budgeted("")

        if others and not budget_exhausted:
            _append_budgeted("【相关知识】")
            offset = len(constraints)
            for i, (score, c) in enumerate(others, 1):
                if not _append_budgeted(_format_chunk(c, offset + i)):
                    break
                if not _append_budgeted(f"  (score={score:.3f})"):
                    break
                if not _append_budgeted(""):
                    break

        if budget_exhausted:
            _append_budgeted("… context budget reached; refine query or request a specific ref/chunk for more details.")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ 检索失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


@mcp.tool()
def memory_stats(project: str | None = None) -> str:
    """
    查询 AIOS 知识库的统计信息（chunk 数量、类型分布、近期活跃度）。

    Returns:
        知识库统计摘要
    """
    if not project:
        try:
            project = resolve_project_id()
        except Exception:
            project = "default"

    try:
        conn = _open_readonly()
    except FileNotFoundError as e:
        return f"❌ 知识库未初始化：{e}"

    try:
        # 总量
        total = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE project=?", (project,)
        ).fetchone()[0]

        # 类型分布
        type_rows = conn.execute(
            "SELECT chunk_type, COUNT(*) FROM memory_chunks WHERE project=? GROUP BY chunk_type",
            (project,)
        ).fetchall()

        # 近期活跃 (access_count > 0)
        active = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE project=? AND access_count > 0",
            (project,)
        ).fetchone()[0]

        # ROI 信号：真实应用 (apply_count > 0) — 区别于「被召回」，这是被模型实际用上的 chunk。
        # COALESCE 兼容老库（apply_count 列惰性添加）。active 与 applied 的差距 = 召回浪费量。
        try:
            applied = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE project=? AND COALESCE(apply_count,0) > 0",
                (project,)
            ).fetchone()[0]
        except Exception:
            applied = 0

        # 最近写入
        recent = conn.execute(
            """SELECT id, chunk_type, summary FROM memory_chunks
               WHERE project=? ORDER BY created_at DESC LIMIT 3""",
            (project,)
        ).fetchall()

        lines = [f"📊 Memory OS 知识库统计 (project={project})\n"]
        lines.append(f"  总量: {total} chunks，活跃: {active} ({active/total*100:.1f}% 被引用)" if total > 0 else "  总量: 0 chunks")
        if total > 0:
            lines.append(f"  真实应用: {applied} ({applied/total*100:.1f}% 被实际用上)  ← 召回≠应用，差距={active-applied}")
        lines.append("")
        lines.append("  类型分布:")
        for row in sorted(type_rows, key=lambda r: -r[1]):
            lines.append(f"    {row[0]:25s}: {row[1]}")
        lines.append("")
        lines.append("  最近写入:")
        for row in recent:
            lines.append(f"    [{row[1]}] {row[2][:80]}")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ 统计失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


# ── 迭代104：pin/unpin MCP 工具（OS 类比：mlock/munlock per-VMA）──────────────

def _open_readwrite() -> sqlite3.Connection:
    """打开读写 DB 连接（pin/unpin 需要写权限）"""
    db_path = Path.home() / ".claude" / "memory-os" / "store.db"
    env_path = os.environ.get("MEMORY_OS_DB")
    if env_path:
        db_path = Path(env_path)
    if not db_path.exists():
        raise FileNotFoundError(f"知识库不存在：{db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@mcp.tool()
def pin_memory(
    chunk_id: str,
    pin_type: str = "soft",
    project: str | None = None,
) -> str:
    """
    将指定 chunk 锁定到当前项目，阻止被自动淘汰。
    OS 类比：mlock(addr, len) — 将页面锁定在进程地址空间，阻止 swap out。

    pin_type:
      'hard' — 所有淘汰路径跳过该 chunk（stale reclaim、DAMON DEAD、kswapd ZONE_MIN）
               适用：设计约束、不可变决策、关键架构知识
      'soft' — 保护 stale reclaim 和 DAMON DEAD，但 kswapd 内存极度紧张时仍可淘汰
               适用：重要但非关键的量化证据、近期决策

    Args:
        chunk_id: 要锁定的 chunk ID（可从 memory_lookup 结果中获取）
        pin_type: 'hard' 或 'soft'（默认 'soft'）
        project: 项目 ID（默认自动解析当前目录）

    Returns:
        操作结果描述
    """
    if not project:
        try:
            project = resolve_project_id()
        except Exception:
            project = "default"

    if pin_type not in ("hard", "soft"):
        return f"❌ pin_type 必须是 'hard' 或 'soft'，得到：{pin_type!r}"

    try:
        conn = _open_readwrite()
    except FileNotFoundError as e:
        return f"❌ 知识库未初始化：{e}"

    try:
        ensure_schema(conn)
        success = pin_chunk(conn, chunk_id, project, pin_type)
        conn.commit()
        if success:
            return (
                f"✅ chunk {chunk_id[:12]}... 已 {pin_type} pin 到 project={project}\n"
                f"  {'🔒 hard pin: 所有淘汰路径均跳过' if pin_type == 'hard' else '🔐 soft pin: 保护 stale/DAMON，不挡 kswapd 硬淘汰'}"
            )
        else:
            return f"❌ chunk {chunk_id[:12]}... 不存在（pin 失败）"
    except Exception as e:
        return f"❌ pin 失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


@mcp.tool()
def unpin_memory(
    chunk_id: str,
    project: str | None = None,
) -> str:
    """
    解除 chunk 在当前项目中的 pin，允许被自动淘汰。
    OS 类比：munlock(addr, len) — 解除内存锁定，页面重新可被 swap out。

    Args:
        chunk_id: 要解锁的 chunk ID
        project: 项目 ID（默认自动解析当前目录）

    Returns:
        操作结果描述
    """
    if not project:
        try:
            project = resolve_project_id()
        except Exception:
            project = "default"

    try:
        conn = _open_readwrite()
    except FileNotFoundError as e:
        return f"❌ 知识库未初始化：{e}"

    try:
        ensure_schema(conn)
        success = unpin_chunk(conn, chunk_id, project)
        conn.commit()
        if success:
            return f"✅ chunk {chunk_id[:12]}... pin 已解除 (project={project})"
        else:
            return f"⚠️ chunk {chunk_id[:12]}... 在 project={project} 中未被 pin"
    except Exception as e:
        return f"❌ unpin 失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


@mcp.tool()
def list_pinned(
    pin_type: str | None = None,
    project: str | None = None,
) -> str:
    """
    列出当前项目中所有 pinned chunks。
    OS 类比：/proc/[pid]/smaps 中 Locked: 字段 — 查看进程的 mlock 区域。

    Args:
        pin_type: 过滤类型 'hard'/'soft'（默认显示全部）
        project: 项目 ID（默认自动解析当前目录）

    Returns:
        格式化的 pinned chunk 列表
    """
    if not project:
        try:
            project = resolve_project_id()
        except Exception:
            project = "default"

    try:
        conn = _open_readonly()
    except FileNotFoundError as e:
        return f"❌ 知识库未初始化：{e}"

    try:
        pinned = get_pinned_chunks(conn, project, pin_type=pin_type)
        if not pinned:
            label = f" ({pin_type} pin)" if pin_type else ""
            return f"📌 project={project} 没有 pinned chunks{label}"

        lines = [f"📌 Pinned chunks (project={project}" + (f", type={pin_type}" if pin_type else "") + f") — {len(pinned)} 条\n"]
        for i, c in enumerate(pinned, 1):
            icon = "🔒" if c["pin_type"] == "hard" else "🔐"
            lines.append(
                f"  {i}. {icon}[{c['pin_type']}] {c['chunk_type']:20s} imp={c['importance']:.2f}\n"
                f"     ID: {c['chunk_id'][:16]}...\n"
                f"     {c['summary'][:100]}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 查询失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


@mcp.tool()
def memory_write(
    summary: str,
    content: str,
    chunk_type: str = "design_constraint",
    project: str | None = None,
    tags: list[str] | None = None,
    importance: float = 0.9,
) -> str:
    """
    将新知识直接写入 memory-os（知识真相源），避免只落到文件 memory。

    Args:
        summary: 一句话摘要，用于检索结果标题
        content: 详细内容，写入前应已去重并确认不是代码/仓库已记录事实
        chunk_type: 知识类型，如 design_constraint / decision / reference / quantitative_evidence
        project: 项目 ID（默认自动解析当前目录）
        tags: 可选标签
        importance: 重要性 0.0-1.0，默认 0.9

    Returns:
        写入结果和 chunk_id
    """
    if not summary or not summary.strip():
        return "❌ summary 为空。"
    if not content or not content.strip():
        return "❌ content 为空。"
    if not project:
        try:
            project = resolve_project_id()
        except Exception:
            project = "default"
    try:
        importance_f = max(0.0, min(float(importance), 1.0))
    except (TypeError, ValueError):
        return "❌ importance 必须是数字。"

    allowed_types = {
        "design_constraint", "decision", "reference", "quantitative_evidence",
        "reasoning_chain", "semantic_memory", "project", "feedback", "user",
    }
    if chunk_type not in allowed_types:
        return f"❌ chunk_type 不支持：{chunk_type}"

    try:
        conn = _open_readwrite()
    except FileNotFoundError as e:
        return f"❌ 知识库未初始化：{e}"

    try:
        import hashlib
        import json as _json
        cid_src = f"{project}\n{chunk_type}\n{summary.strip()}\n{content.strip()}"
        cid = "manual:" + hashlib.sha256(cid_src.encode("utf-8")).hexdigest()[:24]
        now_iso = datetime.now(timezone.utc).isoformat()
        tags_json = _json.dumps(tags or ["manual", "memory_write"], ensure_ascii=False)
        conn.execute(
            """INSERT OR REPLACE INTO memory_chunks
               (id, created_at, updated_at, project, source_session, chunk_type,
                content, summary, tags, importance, retrievability, last_accessed,
                source_type, source_reliability, chunk_state, access_count, apply_count)
               VALUES (?, COALESCE((SELECT created_at FROM memory_chunks WHERE id=?), ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE',
                       COALESCE((SELECT access_count FROM memory_chunks WHERE id=?), 0),
                       COALESCE((SELECT apply_count FROM memory_chunks WHERE id=?), 0))""",
            (
                cid, cid, now_iso, now_iso, project, "memory_write", chunk_type,
                content.strip(), summary.strip(), tags_json, importance_f, 0.9,
                now_iso, "manual", 0.95, cid, cid,
            ),
        )
        rowid = conn.execute("SELECT rowid FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
        conn.execute("DELETE FROM memory_chunks_fts WHERE rowid_ref=?", (str(rowid),))
        conn.execute(
            "INSERT INTO memory_chunks_fts(rowid_ref, summary, content) VALUES (?, ?, ?)",
            (str(rowid), summary.strip(), content.strip()),
        )
        conn.commit()
        return f"✅ memory_write: 写入 {chunk_type} chunk\n  id={cid}\n  project={project}"
    except Exception as e:
        return f"❌ memory_write 失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


@mcp.tool()
def memory_applied(
    chunk_ids: list[str],
    project: str | None = None,
) -> str:
    """
    回写「真实应用」信号：标记某些召回的 chunk 在本次推理中被实际用上了。
    OS 类比：MMU Dirty bit — Accessed bit 只说明页被读过（召回），Dirty bit 才说明
    页内容真正参与了计算并产生了影响（应用）。区分二者是回收决策的核心信号。

    何时调用：当 memory_lookup 返回的某条知识**确实影响了你的输出/决策**时回写其
    chunk_id。仅仅看到、但没用上的 chunk 不要回写——那正是「召回浪费」，apply_count
    保持 0 才能让系统识别并衰减它。

    与 access_count 的区别：
      - access_count：被召回/注入即自增（无差别，量大）
      - apply_count：被真正用上才自增（有判断，量少）→ apply_count/access_count = ROI

    下游消费（已存在，此前因写入侧缺失而恒为 0）：
      - store_vfs RTMC：apply_ratio 修正 stability 巩固量（零应用→floor 0.3 惩罚）
      - write_feedback.decay_stale_pins：apply_count=0 的 pin 按冷度自动降级/解锁

    Args:
        chunk_ids: 本次推理中被实际应用的 chunk ID 列表（取自 memory_lookup 结果）
        project: 项目 ID（默认自动解析当前目录）

    Returns:
        操作结果描述
    """
    if not chunk_ids:
        return "⚠️ chunk_ids 为空，无回写。"

    if not project:
        try:
            project = resolve_project_id()
        except Exception:
            project = "default"

    try:
        conn = _open_readwrite()
    except FileNotFoundError as e:
        return f"❌ 知识库未初始化：{e}"

    try:
        ensure_schema(conn)  # 保证 apply_count 列存在（惰性 ALTER）
        now_iso = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" * len(chunk_ids))
        cur = conn.execute(
            f"UPDATE memory_chunks "
            f"SET apply_count = COALESCE(apply_count, 0) + 1, "
            f"    last_applied = ? "
            f"WHERE id IN ({placeholders})",
            [now_iso, *chunk_ids],
        )
        conn.commit()
        n = cur.rowcount
        if n <= 0:
            return f"❌ 未匹配任何 chunk（id 是否正确？project={project}）"
        return (
            f"✅ 已标记 {n}/{len(chunk_ids)} 条 chunk 为「真实应用」(apply_count +1)\n"
            f"  这些知识的 stability 巩固与 pin 保护将不再吃零应用惩罚。"
        )
    except Exception as e:
        return f"❌ apply 回写失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


@mcp.tool()
def memory_hook_health(
    hours: int = 24,
    project: str | None = None,
) -> str:
    """
    Hook 系统健康检查 — 查看最近 N 小时内哪些 hook 在触发、哪些静默失败。
    OS 类比：dmesg + journalctl — 查看内核子系统日志。

    返回：
      1. hook_txn_log：各 hook 的成功/失败次数、平均耗时
      2. dmesg：最近的系统日志（WARN/ERROR 级别）
      3. assertion_history：存活断言通过率

    Args:
        hours: 查看最近 N 小时的日志（默认 24）
        project: 筛选项目（空=全部）
    """
    if not project:
        try:
            project = resolve_project_id()
        except Exception:
            project = None

    try:
        conn = _open_readonly()
    except FileNotFoundError as e:
        return f"❌ 知识库未初始化：{e}"

    try:
        lines = []

        # ── 1. hook_txn_log ──
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        where = "WHERE committed_at >= ?"
        params = [cutoff]
        if project:
            where += " AND project = ?"
            params.append(project)

        rows = conn.execute(
            f"SELECT hook, status, COUNT(*), AVG(chunk_count) "
            f"FROM hook_txn_log {where} GROUP BY hook, status ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()

        lines.append(f"🪝 Hook 执行统计（最近 {hours}h）")
        if rows:
            for hook, status, cnt, avg_chunks in rows:
                icon = "✅" if status in ("ok", "committed") else "❌"
                chunks_str = f", avg {avg_chunks:.0f} chunks" if avg_chunks else ""
                lines.append(f"  {icon} {hook}: {status} × {cnt}{chunks_str}")
        else:
            lines.append("  (无记录)")

        # ── 2. dmesg WARN/ERROR ──
        dmesg_rows = conn.execute(
            "SELECT timestamp, level, subsystem, message FROM dmesg "
            "WHERE timestamp >= ? AND level IN ('WARN', 'ERROR', 'CRIT') "
            "ORDER BY timestamp DESC LIMIT 10",
            [cutoff],
        ).fetchall()

        lines.append(f"\n📋 最近日志（WARN/ERROR，最近 {hours}h）")
        if dmesg_rows:
            for ts, level, subsystem, msg in dmesg_rows:
                icon = "⚠️" if level == "WARN" else "🔴"
                lines.append(f"  {icon} [{ts[:16]}] [{subsystem}] {msg[:100]}")
        else:
            lines.append("  (无告警)")

        # ── 3. assertion 存活率 ──
        assertion_rows = conn.execute(
            "SELECT assertion_name, "
            "SUM(CASE WHEN passed THEN 1 ELSE 0 END) as pass_cnt, "
            "COUNT(*) as total "
            "FROM assertion_history WHERE ts >= ? "
            "GROUP BY assertion_name ORDER BY total DESC LIMIT 10",
            [cutoff],
        ).fetchall()

        lines.append(f"\n🛡️ 存活断言通过率（最近 {hours}h）")
        if assertion_rows:
            for name, pass_cnt, total in assertion_rows:
                rate = pass_cnt / total * 100 if total else 0
                icon = "✅" if rate >= 90 else "⚠️" if rate >= 50 else "❌"
                lines.append(f"  {icon} {name}: {pass_cnt}/{total} ({rate:.0f}%)")
        else:
            lines.append("  (无记录)")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ hook 健康检查失败：{type(e).__name__}: {e}"
    finally:
        conn.close()


# ── 入口 ────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _stdio_server_compat():
    """stdio transport compatible with Codex and long-lived open stdin pipes.

    The installed MCP SDK's stdio helper wraps ``sys.stdin`` with AnyIO's async
    file iterator. In this environment that iterator does not yield a line until
    stdin closes, so Codex-style clients hang at ``initialize`` because they keep
    the pipe open. This local transport keeps the normal MCP newline-JSON format
    but reads stdin via asyncio fd readiness and writes flushed newline JSON.
    """
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async def stdin_reader():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        fd = sys.stdin.fileno()
        buffer = bytearray()

        def on_stdin_ready() -> None:
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if data:
                queue.put_nowait(data)
            else:
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass
                queue.put_nowait(None)

        loop.add_reader(fd, on_stdin_ready)
        try:
            async with read_stream_writer:
                while True:
                    data = await queue.get()
                    if data is None:
                        break
                    buffer.extend(data)
                    while True:
                        newline = buffer.find(b"\n")
                        if newline < 0:
                            break
                        raw_line = bytes(buffer[: newline + 1])
                        del buffer[: newline + 1]
                        line = raw_line.decode("utf-8", errors="replace")
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            await read_stream_writer.send(exc)
                            continue
                        await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    def _write_stdout(payload: str) -> None:
        sys.stdout.write(payload)
        sys.stdout.flush()

    async def stdout_writer():
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    )
                    _write_stdout(payload + "\n")
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_reader)
        tg.start_soon(stdout_writer)
        yield read_stream, write_stream


async def _run_stdio_compat() -> None:
    async with _stdio_server_compat() as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"doctor", "install", "repair"}:
        from vmem_doctor import main as doctor_main
        raise SystemExit(doctor_main(sys.argv[1:]))
    anyio.run(_run_stdio_compat)


if __name__ == "__main__":
    main()
