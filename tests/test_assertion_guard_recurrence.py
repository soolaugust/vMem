#!/usr/bin/env python3
"""
test_assertion_guard_recurrence.py — 验证断言复发告警链端到端正确。

固化的验证套路（2026-06-05）：用临时 store.db 副本 + MEMORY_OS_DIR 重定向，
在隔离环境制造 apply_signal 复发，跑完整 assertion_guard，断言输出包含：
  1. 复发的 critical 用 ✗🔁 标记且 message 不截断（保留诊断线索）
  2. 【断言复发检测】段含"复发（第 N 次转红"

零污染真实库（临时副本跑完即删）。这是"模拟复发验证告警链"的可复用 harness，
替代手动 Bash 模拟（Code is Harness：确定性验证写成代码）。

用法：python3 tests/test_assertion_guard_recurrence.py
"""
import json
import os
import shutil
import subprocess
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEMOS = HERE.parent
REAL_DB = Path(os.path.expanduser("~/.claude/memory-os/store.db"))


def run() -> bool:
    if not REAL_DB.exists():
        print("SKIP: 真实 store.db 不存在")
        return True

    tmp = Path(tempfile.mkdtemp(prefix="assert-recur-"))
    try:
        shutil.copy(REAL_DB, tmp / "store.db")
        # 复制 swap_state.json 消除无关断言的假复发噪声
        swp = REAL_DB.parent / "swap_state.json"
        if swp.exists():
            shutil.copy(swp, tmp / "swap_state.json")

        # 注入"上次 apply_signal pass"历史 + 清零 apply_count 制造复发
        sys.path.insert(0, str(MEMOS))
        import memory_os.observability.assertion_history_compat as ah
        conn = sqlite3.connect(str(tmp / "store.db"))
        ah.ensure_history_schema(conn)
        yest = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO assertion_history(assertion_name,ts,passed,severity,recurrence_count) "
            "VALUES(?,?,?,?,?)", ("apply_signal_alive", yest, 1, "critical", 0))
        conn.execute("UPDATE memory_chunks SET apply_count=0, last_applied=NULL")
        conn.commit()
        conn.close()

        # 跑完整 assertion_guard（隔离库）
        env = dict(os.environ, MEMORY_OS_DIR=str(tmp))
        proc = subprocess.run(
            [sys.executable, str(MEMOS / "hooks" / "assertion_guard.py")],
            input=json.dumps({"hook_event_name": "SessionStart", "session_id": "test"}),
            capture_output=True, text=True, env=env, timeout=30)

        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]

        checks = {
            "复发标记 ✗🔁": "✗🔁" in ctx and "apply_signal_alive" in ctx,
            "完整诊断线索(不截断)": "suppress_unused" in ctx,
            "复发检测段": "复发（第" in ctx and "次转红" in ctx,
        }
        ok = all(checks.values())
        for name, passed in checks.items():
            print(f"  [{'✓' if passed else '✗'}] {name}")
        print(f"\n{'🎯 告警链端到端通过' if ok else '❌ 告警链断裂'}")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
