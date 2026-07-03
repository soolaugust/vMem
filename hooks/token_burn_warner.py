#!/usr/bin/env python3
"""
token_burn_warner — PostToolUse hook，检测 token 消耗异常并强烈提示。

设计原则：
  - 绝不阻断用户任务（不用 block decision）
  - 强烈提示，持续标记，直到用户响应或问题消除
  - 分两级：WARN（黄色）和 CRITICAL（红色）

检测维度：
  1. 单会话累计 cache_read 超阈值 → 提示 /compact
  2. 单轮工具调用次数过多 → 提示并行化或拆分
  3. 连续 N 轮无产出（只读不写）→ 提示是否卡住了

数据源：tool_profile.db（只读）
"""

import sys
import json
import sqlite3
import os
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".claude" / "memory-os" / "tool_profile.db"

# 阈值配置
WARN_TOOL_CALLS_PER_SESSION = 80       # 单会话工具调用 WARN
CRIT_TOOL_CALLS_PER_SESSION = 150      # 单会话工具调用 CRITICAL
WARN_READONLY_STREAK = 15              # 连续只读调用 WARN
CRIT_READONLY_STREAK = 30              # 连续只读调用 CRITICAL

# 输出调用计数器（避免每次都查 DB，用文件缓存近似值）
COUNTER_FILE = Path("/tmp") / "token_warner" / "counters.json"


def load_counters(session_id: str) -> dict:
    """从文件缓存加载计数器（避免每次查 DB）。"""
    try:
        if COUNTER_FILE.exists():
            data = json.loads(COUNTER_FILE.read_text())
            return data.get(session_id, {})
    except Exception:
        pass
    return {}


def save_counters(session_id: str, counters: dict):
    """保存计数器到文件。"""
    try:
        COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if COUNTER_FILE.exists():
            data = json.loads(COUNTER_FILE.read_text())
        data[session_id] = counters
        # 清理超过 24h 的 session
        now = datetime.now().timestamp()
        cleaned = {}
        for sid, c in data.items():
            if now - c.get("last_ts", 0) < 86400:
                cleaned[sid] = c
        COUNTER_FILE.write_text(json.dumps(cleaned))
    except Exception:
        pass


def query_session_stats(session_id: str) -> dict:
    """查询 session 的工具调用统计。"""
    try:
        db_uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True, timeout=2)

        # 总调用次数
        row = conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        total = row[0] if row else 0

        # 最近 20 次调用的 tool_name（用于检测只读连续）
        rows = conn.execute(
            "SELECT tool_name FROM tool_calls "
            "WHERE session_id = ? ORDER BY ts DESC LIMIT 20",
            (session_id,)
        ).fetchall()
        recent = [r[0] for r in rows]

        # 最近一次写操作的位置
        write_tools = {"Edit", "Write", "MultiEdit"}
        readonly_streak = 0
        for name in recent:
            if name in write_tools:
                break
            readonly_streak += 1

        conn.close()
        return {
            "total_calls": total,
            "recent_tools": recent,
            "readonly_streak": readonly_streak,
        }
    except Exception:
        return {"total_calls": 0, "recent_tools": [], "readonly_streak": 0}


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    # 用文件计数器快速判断是否需要查 DB
    counters = load_counters(session_id)
    call_count = counters.get("call_count", 0) + 1
    counters["call_count"] = call_count
    counters["last_ts"] = datetime.now().timestamp()

    # 每 10 次调用查一次 DB 校准（避免每次都查 DB 的开销）
    if call_count % 10 == 1 or call_count < 5:
        stats = query_session_stats(session_id)
        actual = stats["total_calls"]
        if actual > call_count:
            call_count = actual
            counters["call_count"] = call_count
        readonly_streak = stats["readonly_streak"]
    else:
        readonly_streak = counters.get("readonly_streak", 0)

    warnings = []

    # 检测 1: 总调用次数
    if call_count >= CRIT_TOOL_CALLS_PER_SESSION:
        warnings.append(
            f"🔴 CRITICAL: 本会话已执行 {call_count} 次工具调用。"
            f"强烈建议 /compact 开新会话，或审视当前任务是否陷入循环。"
        )
    elif call_count >= WARN_TOOL_CALLS_PER_SESSION:
        warnings.append(
            f"🟡 WARN: 本会话已执行 {call_count} 次工具调用（接近上限 {CRIT_TOOL_CALLS_PER_SESSION}）。"
            f"考虑 /compact 或拆分任务。"
        )

    # 检测 2: 连续只读
    if readonly_streak >= CRIT_READONLY_STREAK:
        warnings.append(
            f"🔴 CRITICAL: 连续 {readonly_streak} 次只读操作（Read/Grep/Bash 查询），无任何写入。"
            f"可能在无效探索——审视是否已经找到答案但没推进。"
        )
    elif readonly_streak >= WARN_READONLY_STREAK:
        warnings.append(
            f"🟡 WARN: 连续 {readonly_streak} 次只读操作。如果在搜索信息，已经够多了——考虑行动。"
        )

    save_counters(session_id, counters)

    if warnings:
        # 通过 stderr 输出（hook 的 stderr 会作为 feedback 注入到模型）
        for w in warnings:
            sys.stderr.write(f"\n{'='*60}\n{w}\n{'='*60}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
