"""
assertion_history.py — 断言运行历史 + 复发检测（闭环的「记忆」层）

根因（2026-06-05）：production_assertions.py 能抓死链，但
  (1) 没自动跑——只在人想起时执行，监控退化成偶尔体检；
  (2) 没复发检测——无法回答「这个坑是第几次踩」，导致同一个 bug 用同样的
      轻量修法反复修反复坏（apply_signal 死链就复发了一次）。

本模块提供逐条断言的转红/转绿历史，并计算复发次数。它是确定性的
（纯 SQL + 状态机比较，零 LLM），符合「Code is Harness」原则。

数据结构（store.db 中一张表）：
    assertion_history(assertion_name, ts, passed, severity, recurrence_count)
  recurrence_count 语义：该断言「从 pass 跌回 fail」的累计次数。
  一个断言第一次失败 recurrence=0；修好后再坏 recurrence=1；如此累加。
  recurrence >= 2 → 该教训不该只是记忆，应固化为永久断言/fix 逻辑。

OS 类比：edac（EDAC）内存错误计数器 — 不只报「现在有错」，更累计
  「同一地址纠错几次」，CE 累计超阈值才升级为需要换条的 UE。
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

MEMORY_OS_DIR = Path(os.environ.get("MEMORY_OS_DIR", os.path.expanduser("~/.claude/memory-os")))
STORE_DB = MEMORY_OS_DIR / "store.db"

# 复发升级阈值：累计跌回失败 >= 此值 → 提示固化为永久约束
RECURRENCE_ESCALATE = 2
# 复发窗口：仅在此天数内的「上一次状态」参与复发判定（防止远古历史误判）
RECURRENCE_WINDOW_DAYS = 30


def ensure_history_schema(conn: sqlite3.Connection) -> None:
    """惰性建表（幂等）。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS assertion_history (
               id               INTEGER PRIMARY KEY AUTOINCREMENT,
               assertion_name   TEXT    NOT NULL,
               ts               TEXT    NOT NULL,
               passed           INTEGER NOT NULL,
               severity         TEXT,
               recurrence_count INTEGER DEFAULT 0
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_assert_hist_name_ts "
        "ON assertion_history(assertion_name, ts DESC)"
    )
    conn.commit()


def _last_state(conn: sqlite3.Connection, name: str):
    """返回该断言最近一条历史 (passed:bool, recurrence:int, ts:str) 或 None。"""
    row = conn.execute(
        "SELECT passed, recurrence_count, ts FROM assertion_history "
        "WHERE assertion_name=? ORDER BY ts DESC LIMIT 1",
        (name,),
    ).fetchone()
    if not row:
        return None
    return bool(row[0]), int(row[1] or 0), row[2]


def record_run(conn: sqlite3.Connection, results: list) -> dict:
    """记录本次断言运行，计算每条断言的复发次数。

    Args:
        conn: store.db 连接
        results: [{"name","passed","severity"}, ...]（AssertionResult.to_dict 子集）

    Returns:
        {
          "recurred":   [{"name","recurrence","since"}...],  # 本次新转红 / 再次转红
          "escalate":   [{"name","recurrence"}...],          # 复发 >= 阈值，需固化
          "recovered":  ["name"...],                          # 本次从 fail 转回 pass
        }
    """
    ensure_history_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECURRENCE_WINDOW_DAYS)).isoformat()

    recurred, escalate, recovered = [], [], []

    for r in results:
        name = r.get("name")
        if not name:
            continue
        passed = bool(r.get("passed"))
        severity = r.get("severity", "info")

        prev = _last_state(conn, name)
        recurrence = 0

        if prev is not None:
            prev_passed, prev_recur, prev_ts = prev
            recurrence = prev_recur
            in_window = prev_ts >= cutoff
            if not passed and prev_passed and in_window:
                # pass → fail 跌落：复发计数 +1
                recurrence = prev_recur + 1
                recurred.append({"name": name, "recurrence": recurrence, "since": prev_ts})
                if recurrence >= RECURRENCE_ESCALATE:
                    escalate.append({"name": name, "recurrence": recurrence})
            elif not passed and not prev_passed:
                # 持续失败：保持原 recurrence，不重复计数
                pass
            elif passed and not prev_passed:
                recovered.append(name)
        else:
            # 首次记录该断言：失败也算 recurrence=0（第一次踩坑不是复发）
            if not passed:
                recurred.append({"name": name, "recurrence": 0, "since": None})

        conn.execute(
            "INSERT INTO assertion_history (assertion_name, ts, passed, severity, recurrence_count) "
            "VALUES (?,?,?,?,?)",
            (name, now, 1 if passed else 0, severity, recurrence),
        )

    conn.commit()
    return {"recurred": recurred, "escalate": escalate, "recovered": recovered}


def format_recurrence_alert(analysis: dict) -> str:
    """把复发分析格式化成给 agent 看的告警文本（注入会话上下文用）。空则返回 ""。"""
    lines = []
    for e in analysis.get("escalate", []):
        lines.append(
            f"  🔁 [{e['name']}] 已第 {e['recurrence']} 次从修复后跌回失败——"
            f"轻量修法反复失败，应固化为永久断言/补 fix 逻辑（缺口2：教训升级）"
        )
    for r in analysis.get("recurred", []):
        if r["recurrence"] >= RECURRENCE_ESCALATE:
            continue  # 已在 escalate 里报过
        if r["recurrence"] >= 1:
            lines.append(
                f"  ⚠️ [{r['name']}] 复发（第 {r['recurrence']} 次转红，上次正常于 {r['since'][:10]}）"
            )
    if not lines:
        return ""
    return "【断言复发检测】\n" + "\n".join(lines)


def history_summary(conn: sqlite3.Connection, name: str = None, limit: int = 20) -> list:
    """查询历史（调试/报告用）。name=None 返回全部最近 limit 条。"""
    ensure_history_schema(conn)
    if name:
        rows = conn.execute(
            "SELECT assertion_name, ts, passed, severity, recurrence_count "
            "FROM assertion_history WHERE assertion_name=? ORDER BY ts DESC LIMIT ?",
            (name, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT assertion_name, ts, passed, severity, recurrence_count "
            "FROM assertion_history ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"name": r[0], "ts": r[1], "passed": bool(r[2]),
         "severity": r[3], "recurrence": r[4]}
        for r in rows
    ]


if __name__ == "__main__":
    import sys
    conn = sqlite3.connect(str(STORE_DB), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    name = sys.argv[1] if len(sys.argv) > 1 else None
    for h in history_summary(conn, name):
        mark = "✓" if h["passed"] else "✗"
        rec = f" recur={h['recurrence']}" if h["recurrence"] else ""
        print(f"[{mark}] {h['ts'][:19]} {h['name']}{rec}")
    conn.close()
