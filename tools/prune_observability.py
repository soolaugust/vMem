#!/usr/bin/env python3
"""
prune_observability.py — 观测表 TTL/上限清理（修复 DB 膨胀）

背景：cache_hit_harness 发现库 5.5MB 但知识本体(memory_chunks)仅 0.09MB，
膨胀 60x+。根因是观测/影子追踪表（shadow_traces/session_focus/checkpoints 等）
无 TTL 无上限，随 session 无限堆积。db_hygiene.py 只清 memory_chunks，不覆盖这些。

OS 类比：logrotate + systemd journal vacuum — 运行时遥测数据按时间/大小滚动回收，
不影响业务数据（知识本体）。

策略（保守，只删观测数据，永不碰 memory_chunks / chunk_pins / goals）：
  - 按时间列 TTL 删除超期行
  - 对无可靠时间列或需限量的表，保留最近 N 行（按 rowid）
  - VACUUM 回收空间（可选，--vacuum）

用法：
  python3 tools/prune_observability.py --dry-run      # 只看会删多少
  python3 tools/prune_observability.py                # 执行清理
  python3 tools/prune_observability.py --vacuum       # 清理后回收空间
  python3 tools/prune_observability.py --ttl-days 14  # 自定义 TTL
"""
import sqlite3
import os
import sys
import argparse
from pathlib import Path

# 表 → (时间列, TTL 天数, 行数上限)。时间列为 None 则只按 rowid 限量。
# TTL 与上限取「先到先删」：超期 OR 超量都清。
_RULES = {
    "shadow_traces":   ("updated_at", 30, 2000),
    "session_focus":   ("updated_at", 30, 2000),
    "checkpoints":     ("created_at", 30, 500),
    "replay_events":   ("timestamp", 30, 3000),
    "priming_state":   ("primed_at", 14, 1000),
    "tool_patterns":   ("last_seen", 60, 1000),
    "hook_txn_log":    (None, None, 1000),
    "dmesg":           (None, None, 1000),
    "chunk_coactivation": (None, None, 2000),
}

# 绝对禁止清理的业务表（知识本体 + 用户意图）
_PROTECTED = {"memory_chunks", "memory_chunks_fts", "chunk_pins", "goals",
              "workspaces", "workspace_todos", "knowledge_versions",
              "schema_anchors", "recall_traces"}  # recall_traces 保留作命中率分析


def _default_db() -> str:
    env = os.environ.get("MEMORY_OS_DB")
    if env and Path(env).exists():
        return env
    for cand in (Path.home() / ".claude" / "memory-os" / "store.db",
                 Path.home() / ".memory-os" / "store.db"):
        if cand.exists():
            return str(cand)
    raise SystemExit("找不到 store.db，请用 --db 指定")


def _exists(c, t) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (t,)).fetchone() is not None


def prune(db_path: str, ttl_days_override=None, dry_run=False, vacuum=False) -> dict:
    c = sqlite3.connect(db_path)
    stats = {}
    for table, (time_col, ttl, cap) in _RULES.items():
        if table in _PROTECTED or not _exists(c, table):
            continue
        ttl = ttl_days_override if ttl_days_override is not None else ttl
        before = c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        deleted = 0

        # 1. TTL 删除超期
        if time_col and ttl:
            q = (f"SELECT COUNT(*) FROM \"{table}\" "
                 f"WHERE {time_col} < datetime('now', ?)")
            n = c.execute(q, (f"-{ttl} days",)).fetchone()[0]
            if n and not dry_run:
                c.execute(f"DELETE FROM \"{table}\" WHERE {time_col} < datetime('now', ?)",
                          (f"-{ttl} days",))
            deleted += n

        # 2. 行数上限：保留最近 cap 行（按 rowid，删最旧）
        remain = before - deleted
        if cap and remain > cap:
            over = remain - cap
            if not dry_run:
                c.execute(
                    f"DELETE FROM \"{table}\" WHERE rowid IN "
                    f"(SELECT rowid FROM \"{table}\" ORDER BY rowid ASC LIMIT ?)",
                    (over,))
            deleted += over

        if deleted:
            stats[table] = {"before": before, "deleted": deleted, "after": before - deleted}

    if not dry_run:
        c.commit()
        if vacuum:
            c.execute("VACUUM")
    c.close()
    return stats


def main():
    ap = argparse.ArgumentParser(description="观测表 TTL/上限清理")
    ap.add_argument("--db", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vacuum", action="store_true", help="清理后 VACUUM 回收空间")
    ap.add_argument("--ttl-days", type=int, default=None, help="覆盖所有表的 TTL 天数")
    args = ap.parse_args()

    db_path = args.db or _default_db()
    stats = prune(db_path, args.ttl_days, args.dry_run, args.vacuum)

    mode = "[DRY-RUN] " if args.dry_run else ""
    if not stats:
        print(f"{mode}无可清理行（观测表均在阈值内）")
        return
    total = sum(s["deleted"] for s in stats.values())
    print(f"{mode}清理观测表 {len(stats)} 张，共 {total} 行：")
    for t, s in sorted(stats.items(), key=lambda x: -x[1]["deleted"]):
        print(f"  {t:22} {s['before']:6} → {s['after']:6}  (-{s['deleted']})")
    if args.vacuum and not args.dry_run:
        print("已 VACUUM 回收空间")


if __name__ == "__main__":
    main()
