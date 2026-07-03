#!/usr/bin/env python3
"""
remap_project_keys.py — 存量 project key 迁移（修复命名空间错位孤儿）

背景：resolve_project_id v2 起改用 gitroot 优先（见 utils.py），使 key 不再随
git remote 漂移。但这会让两类存量 chunk 失配,成为永不可命中的孤儿：
  1. 字面量 dirname 孤儿（如 "claude-workspace"）—— 早期目录无 git 时的兜底产物
  2. 旧 git: key —— v2 前用 git remote hash,现在同目录 resolve 成 gitroot: hash

本脚本把这些旧 key 迁移到 v2 resolver 对当前物理目录算出的稳定 key。

安全：
  - 默认 --dry-run 预览
  - 执行前自动备份库
  - 只迁移 --map 显式指定的映射，或字面量 dirname 孤儿（可自动反解）
  - global / 当前已是正确 gitroot: 的 key 不动

用法：
  # 预览(用内置已知映射 + 自动反解)
  python3 tools/remap_project_keys.py --dry-run
  # 显式指定映射(推荐,最可控)
  python3 tools/remap_project_keys.py --map 'claude-workspace=gitroot:7e3095aef7a6' \\
                                      --map 'git:a0ab16e8cafc=gitroot:ea65ed9a9fae'
  # 执行
  python3 tools/remap_project_keys.py --map '...'
"""
import sqlite3
import os
import sys
import shutil
import argparse
from pathlib import Path

# 永不迁移的 key（业务语义明确）
_PROTECTED = {"global", "__semantic__", ""}

# 已知的 dirname → 物理路径候选（用于自动反解 dirname 孤儿到当前稳定 key）
# 仅在能确定该 dirname 唯一对应某物理目录时填入。
_KNOWN_DIR_PATHS = {
    "claude-workspace": "/home/mi/ssd/codes/claude-workspace",
}


def _default_db() -> str:
    env = os.environ.get("MEMORY_OS_DB")
    if env and Path(env).exists():
        return env
    for cand in (Path.home() / ".claude" / "memory-os" / "store.db",
                 Path.home() / ".memory-os" / "store.db"):
        if cand.exists():
            return str(cand)
    raise SystemExit("找不到 store.db，请用 --db 指定")


def _resolve_for_dir(path: str):
    """对物理目录跑 v2 resolver 得到当前稳定 key。失败返回 None。"""
    if not os.path.isdir(path):
        return None
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from memory_os.core.utils import resolve_project_id
        return resolve_project_id(path)
    except Exception:
        return None


def build_remap(conn, explicit_map: dict) -> dict:
    """
    构造 old_key → new_key 迁移映射。
    优先级：explicit_map（--map 显式） > 自动反解（dirname 孤儿）。
    只返回 old != new 且 old 非 protected 的项。
    """
    remap = {}
    existing = {r[0] for r in conn.execute("SELECT DISTINCT project FROM memory_chunks")}

    for old in existing:
        if old in _PROTECTED:
            continue
        new = None
        # 1. 显式映射优先
        if old in explicit_map:
            new = explicit_map[old]
        # 2. dirname 孤儿自动反解（非 git:/gitroot:/abspath: 前缀 = 字面量 dirname）
        elif not str(old).startswith(("git:", "gitroot:", "abspath:")):
            path = _KNOWN_DIR_PATHS.get(old)
            if path:
                new = _resolve_for_dir(path)
        if new and new != old:
            remap[old] = new
    return remap


def apply_remap(db_path: str, explicit_map: dict, dry_run=True) -> dict:
    conn = sqlite3.connect(db_path)
    remap = build_remap(conn, explicit_map)
    stats = {"mappings": {}, "total_rows": 0}

    for old, new in remap.items():
        n = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE project=?",
                         (old,)).fetchone()[0]
        if n == 0:
            continue
        stats["mappings"][old] = {"new": new, "rows": n}
        stats["total_rows"] += n
        if not dry_run:
            conn.execute("UPDATE memory_chunks SET project=? WHERE project=?",
                        (new, old))

    if not dry_run and stats["total_rows"] > 0:
        conn.commit()
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser(description="存量 project key 迁移")
    ap.add_argument("--db", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只预览不改库")
    ap.add_argument("--map", action="append", default=[],
                    help="显式映射 old=new，可多次。如 'claude-workspace=gitroot:abc'")
    args = ap.parse_args()

    db_path = args.db or _default_db()
    explicit = {}
    for m in args.map:
        if "=" not in m:
            raise SystemExit(f"--map 格式错误（需 old=new）: {m}")
        k, v = m.split("=", 1)
        explicit[k.strip()] = v.strip()

    # 执行前备份
    if not args.dry_run:
        import time
        bak = f"{db_path}.bak_before_remap"
        shutil.copy2(db_path, bak)
        print(f"已备份: {bak}")

    stats = apply_remap(db_path, explicit, dry_run=args.dry_run)

    mode = "[DRY-RUN] " if args.dry_run else ""
    if not stats["mappings"]:
        print(f"{mode}无可迁移 key（无孤儿或映射未匹配）")
        return
    print(f"{mode}迁移 {len(stats['mappings'])} 个 key，共 {stats['total_rows']} 行：")
    for old, info in sorted(stats["mappings"].items(), key=lambda x: -x[1]["rows"]):
        print(f"  {old:28} → {info['new']:24} ({info['rows']} 行)")


if __name__ == "__main__":
    main()
