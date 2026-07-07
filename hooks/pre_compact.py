"""
pre_compact.py — PreCompact hook（suspend_prepare notifier chain）

OS 类比：Linux suspend_prepare notifier chain (2005) — 内核子系统在进入
suspend 状态前收到通知，刷出关键状态到持久化存储。

当 Claude Code 压缩上下文时，之前注入的记忆会被压缩/丢失。
此 hook 在压缩前重新注入 hard-pinned chunks + top-K recent decisions，
确保核心知识不被压缩掉。

性能目标：<50ms（两条 SQL 查询 + 字符串格式化）
"""

import json
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_project() -> str:
    """解析当前项目 ID（与 retriever.py 相同逻辑）。"""
    proj = os.environ.get("CLAUDE_PROJECT", "")
    if proj:
        return proj
    try:
        from memory_os.core.utils import resolve_project_id
        return resolve_project_id()
    except Exception:
        return "unknown"


def _open_db_readonly() -> sqlite3.Connection:
    """只读打开 store.db。"""
    from memory_os.store.vfs_compat import open_db, DB_PATH
    db_path = os.environ.get("MEMORY_OS_DB", DB_PATH)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _collect_critical_chunks(conn: sqlite3.Connection, project: str,
                              max_chars: int, decision_top_k: int,
                              decision_min_importance: float) -> str:
    """
    收集 hard-pinned + top-K recent decisions，格式化为紧凑文本。

    优先级：hard-pinned > recent decisions。去重：pinned 中已有的 decision 不重复。
    """
    from memory_os.store.vfs_compat import get_pinned_chunks

    parts = []
    seen_ids = set()
    char_count = 0

    # 1. Hard-pinned chunks
    pinned = get_pinned_chunks(conn, project, pin_type="hard")
    for p in pinned:
        line = f"[{p['chunk_type']}] {p['summary']}"
        if char_count + len(line) + 1 > max_chars:
            break
        parts.append(line)
        seen_ids.add(p["chunk_id"])
        char_count += len(line) + 1

    # 2. Top-K recent decisions (importance >= threshold)
    rows = conn.execute(
        """SELECT id, summary, chunk_type FROM memory_chunks
           WHERE project = ? AND chunk_type = 'decision'
             AND importance >= ? AND chunk_state = 'ACTIVE'
           ORDER BY last_accessed DESC
           LIMIT ?""",
        (project, decision_min_importance, decision_top_k * 2),
    ).fetchall()

    added = 0
    for row_id, summary, chunk_type in rows:
        if row_id in seen_ids:
            continue
        if added >= decision_top_k:
            break
        line = f"[decision] {summary}"
        if char_count + len(line) + 1 > max_chars:
            break
        parts.append(line)
        char_count += len(line) + 1
        added += 1

    return "\n".join(parts)


def main():
    """PreCompact hook 入口。"""
    try:
        from memory_os.config.sysctl import get
        if not get("precompact.enabled"):
            print(json.dumps({}))
            return

        max_chars = get("precompact.max_chars") or 2000
        decision_top_k = get("precompact.decision_top_k") or 3
        decision_min_imp = get("precompact.decision_min_importance") or 0.6
    except Exception:
        max_chars, decision_top_k, decision_min_imp = 2000, 3, 0.6

    project = _resolve_project()

    try:
        conn = _open_db_readonly()
    except Exception:
        print(json.dumps({}))
        return

    try:
        context_text = _collect_critical_chunks(
            conn, project, max_chars, decision_top_k, decision_min_imp
        )
    finally:
        conn.close()

    if not context_text:
        print(json.dumps({}))
        return

    # TaC compression: if collected text exceeds budget, compress via thinking
    tac_meta = None
    if len(context_text) > max_chars:
        try:
            from hooks.tac_compressor import tac_compress
            context_text, tac_meta = tac_compress(context_text, budget_chars=max_chars)
        except Exception:
            context_text = context_text[:max_chars]

    try:
        from hooks.context_governor import enforce_additional_context
        output = enforce_additional_context(
            None,
            context_text,
            producer="pre_compact",
            hook_event_name="PreCompact",
            mandatory=True,
            max_chars=max_chars,
        )
    except Exception:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreCompact",
                "additionalContext": context_text[:max_chars]
            }
        }
    if output and tac_meta:
        output["hookSpecificOutput"]["_tac_compression"] = tac_meta
    if output:
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
