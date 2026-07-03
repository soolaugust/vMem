#!/usr/bin/env python3
"""
loop_breaker — PreToolUse 同步 hook，检测重复命令并 block。

OS 类比: Linux hung_task_timeout_secs — 内核检测到进程在不可中断状态
  (D state) 超过阈值时打印告警并可选 panic，防止单个进程无限期占用资源。

AIOS 类比: 在每次工具调用前检查当前 session 内同一命令的执行次数，
  超过阈值时 block 执行并注入诊断提示，防止 agent 陷入死循环。

根因: mimo-flash 和 opus 均出现过死循环 — 相同命令重复执行数百次，
  浪费大量 tokens。

v2 升级 (2026-06-09):
  新增 pattern-level 去重 — 不只看精确命令，还提取命令族（command family）。
  实证: mm get 67e52e58c vs mm get 6e52e58c 只差一个字符，精确匹配各 300+ 次
  都不触发，但模式匹配 `mm get *` 立即识别为同一族。

阈值设计:
  - Bash exact: 5 次（死循环实证: 477 次相同 ls|grep|wc）
  - Bash pattern: 15 次（同一命令族，如 `mm get *`，容忍少量变体但不过分）
  - Read: 8 次（正常编辑-验证循环可能重读同一文件，留余量）
  - 其他: 10 次

持久化: 复用 tool_profile.db（只读查询，不写入）
"""

import sys
import json
import sqlite3
import re
import hashlib
import shlex
from pathlib import Path

MEMORY_OS_DIR = Path.home() / ".claude" / "memory-os"
DB_PATH = MEMORY_OS_DIR / "tool_profile.db"

# 精确 key 阈值: 同一 session 内同一 tool_key 的最大允许执行次数
EXACT_THRESHOLDS = {
    "Bash": 5,
    "Read": 8,
    "Grep": 10,
    "Edit": 10,
    "Write": 10,
}

# 模式 key 阈值: 同一 session 内同一 command family 的最大允许执行次数
PATTERN_THRESHOLDS = {
    "Bash": 15,
}

# 接近阈值时的警告余量（差 N 次时 stderr 警告）
WARN_MARGIN = 2


def _stable_digest(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:length]


def _python_family(cmd: str, first_token: str, parts: list[str]) -> str | None:
    if first_token not in ("python3", "python"):
        return None
    if len(parts) >= 2 and parts[1] in ("-c", "--command"):
        code = cmd.split(parts[1], 1)[1].strip()
        return f"{first_token} {parts[1]} code:{_stable_digest(code)}"
    if len(parts) >= 2 and parts[1] == "-":
        code = cmd.split("\n", 1)[1] if "\n" in cmd else cmd
        return f"{first_token} stdin code:{_stable_digest(code)}"
    if len(parts) >= 2 and parts[1].endswith(".py"):
        script = Path(parts[1]).name
        return f"{first_token} script:{script}"
    return f"{first_token} <args>"


def _git_family(cmd: str, parts: list[str]) -> str | None:
    if not parts or parts[0] != "git":
        return None
    idx = 1
    if len(parts) >= 3 and parts[1] == "-C":
        idx = 3
    if idx >= len(parts):
        return "git <args>"
    subcmd = parts[idx]
    if subcmd in {"show", "grep", "rev-list", "diff-tree", "branch", "for-each-ref"}:
        semantic_args = []
        for arg in parts[idx + 1:]:
            if arg == "--":
                break
            if arg.startswith("-"):
                continue
            semantic_args.append(arg)
            if len(semantic_args) >= 2:
                break
        suffix = ":".join(semantic_args) if semantic_args else "<args>"
        return f"git {subcmd} {suffix}"
    return f"git {subcmd} <args>"


def _option_value(parts: list[str], option: str) -> str | None:
    for idx, part in enumerate(parts):
        if part == option and idx + 1 < len(parts):
            return parts[idx + 1]
        if part.startswith(option + "="):
            return part.split("=", 1)[1]
    return None


def _vng_family(cmd: str, parts: list[str]) -> str | None:
    if not parts or parts[0] != "vng":
        return None
    mode = "run" if "--run" in parts else "build" if "--build" in parts else "kconfig" if "--kconfig" in parts else "other"
    kernel = _option_value(parts, "--run") if mode == "run" else None
    exec_cmd = _option_value(parts, "--exec") or ""
    exec_name = ""
    exec_digest = ""
    if exec_cmd:
        try:
            exec_parts = shlex.split(exec_cmd, posix=True)
        except ValueError:
            exec_parts = exec_cmd.split()
        for item in exec_parts:
            if item.endswith((".sh", ".py", ".bt")):
                exec_name = Path(item).name
                break
        exec_digest = _stable_digest(exec_cmd)
    kernel_name = Path(kernel).name if kernel else "default"
    if exec_cmd:
        return f"vng {mode} kernel:{kernel_name} exec:{exec_name or '<cmd>'}:{exec_digest}"
    return f"vng {mode} kernel:{kernel_name}"


def extract_command_family(cmd: str) -> str:
    """
    从 bash 命令中提取「命令族」— 将变化的参数归一化为通配符，
    但保留足够语义避免把自动推进中的不同确定性脚本误判为同一循环。

    mm get 67e52e58c-... → mm get <id>
    git show origin/for-next:file → git show origin/for-next:file
    python3 - <<'PY' ... → python3 stdin code:<digest>
    python3 script.py → python3 script:script.py
    """
    cmd = cmd.strip()
    if not cmd:
        return ""

    # mm get/search/delete/pin/unpin → mm <subcmd> <arg> 归一化
    m = re.match(r'^(mm\s+(?:get|search|delete|pin|unpin)\s+)(\S+)(.*)', cmd)
    if m:
        prefix, arg, rest = m.group(1), m.group(2), m.group(3)
        # UUID-like or long hex hash → <id>
        if re.match(r'^[0-9a-f]{8}-?[0-9a-f]{4}-?', arg) or len(arg) > 12:
            return f"{prefix}<id>{rest}".strip()
        # 带引号的搜索词 → <query>
        if arg.startswith('"') or arg.startswith("'"):
            return f"{prefix}<query>{rest}".strip()
        # 其他参数也归一化
        return f"{prefix}<arg>{rest}".strip()

    try:
        parts = shlex.split(cmd, posix=True)
    except ValueError:
        parts = cmd.split()
    first_token = parts[0] if parts else ""

    python_family = _python_family(cmd, first_token, parts)
    if python_family:
        return python_family

    # node -e: 代码片段归一化但保留代码摘要
    if first_token == "node" and len(parts) >= 2 and parts[1] in ("-e", "--eval"):
        code = cmd.split(parts[1], 1)[1].strip()
        return f"{first_token} {parts[1]} code:{_stable_digest(code)}"

    git_family = _git_family(cmd, parts)
    if git_family:
        return git_family

    vng_family = _vng_family(cmd, parts)
    if vng_family:
        return vng_family

    # 常见工具: 保留子命令，参数归一化
    subcommand_tools = {'docker', 'kubectl', 'npm', 'pip', 'systemctl'}
    if first_token in subcommand_tools and len(parts) >= 2:
        return f"{first_token} {parts[1]} <args>"

    # 其他: 保留命令名 + <args>
    return f"{first_token} <args>" if first_token else cmd[:50]


def normalize_key(tool_name: str, tool_input: dict) -> tuple:
    """
    返回 (exact_key, pattern_key)。
    exact_key: 精确匹配（现有逻辑）
    pattern_key: 命令族匹配（新增）
    """
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        return f"read:{path}", f"read:{path}"
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        cmd_normalized = re.sub(r'\s+', ' ', cmd.strip())[:200]
        exact = f"bash:{cmd_normalized}"
        family = extract_command_family(cmd)
        pattern = f"bash:family:{family}"
        return exact, pattern
    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"grep:{path}:{pattern[:50]}", f"grep:{path}:{pattern[:50]}"
    elif tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path", "")
        return f"write:{path}", f"write:{path}"
    else:
        return f"{tool_name.lower()}:_", f"{tool_name.lower()}:_"


def query_count(conn, session_id: str, tool_name: str, key_prefix: str, exact: bool = True) -> int:
    """查询 session 内某 key 的执行次数。exact=True 精确匹配，False 前缀匹配。"""
    try:
        if exact:
            row = conn.execute(
                "SELECT COUNT(*) FROM tool_calls "
                "WHERE session_id = ? AND tool_name = ? AND tool_key = ? "
                "AND ts >= datetime('now', '-1 hour')",
                (session_id, tool_name, key_prefix)
            ).fetchone()
        else:
            # pattern key 存为 exact key，但我们需要匹配同一 family
            # 用 tool_key LIKE 'bash:family:%' 的方式不行，因为 tool_key 存的是精确命令
            # 所以改用：提取所有 bash 调用，重新计算 family 并聚合
            rows = conn.execute(
                "SELECT tool_key FROM tool_calls "
                "WHERE session_id = ? AND tool_name = ? "
                "AND ts >= datetime('now', '-1 hour')",
                (session_id, tool_name)
            ).fetchall()
            count = 0
            target_family = key_prefix.replace("bash:family:", "")
            for (stored_key,) in rows:
                if stored_key.startswith("bash:"):
                    stored_cmd = stored_key[5:]  # strip "bash:"
                    if extract_command_family(stored_cmd) == target_family:
                        count += 1
            return count
        return row[0] if row else 0
    except Exception:
        return 0


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    session_id = data.get("session_id", "")

    if tool_name not in EXACT_THRESHOLDS or not session_id:
        sys.exit(0)

    exact_key, pattern_key = normalize_key(tool_name, tool_input)

    try:
        db_uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True, timeout=2)

        # 1. 精确匹配检查
        exact_count = query_count(conn, session_id, tool_name, exact_key, exact=True)

        # 2. 模式匹配检查（仅 Bash）
        pattern_count = 0
        if tool_name in PATTERN_THRESHOLDS:
            pattern_count = query_count(conn, session_id, tool_name, pattern_key, exact=False)

        conn.close()
    except Exception:
        sys.exit(0)

    entity = exact_key.split(":", 1)[-1][:80]
    exact_limit = EXACT_THRESHOLDS.get(tool_name, 10)
    pattern_limit = PATTERN_THRESHOLDS.get(tool_name, 999)

    # block: 精确 key 超过阈值
    if exact_count >= exact_limit:
        result = {
            "decision": "block",
            "reason": (
                f"[loop_breaker] {tool_name} × {exact_count + 1}次: '{entity}' — "
                f"同一命令已执行 {exact_count} 次（阈值 {exact_limit}），疑似死循环。"
                f"请停下来分析为什么结果不满足预期，而非继续重试。"
            ),
            "suggestion": (
                "1) 检查命令逻辑是否正确；"
                "2) 如需不同结果，请修改命令参数；"
                "3) 如任务已完成，请切换到下一个任务。"
            ),
        }
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        sys.exit(2)

    # block: 命令族超过阈值（v2 新增）
    if pattern_count >= pattern_limit:
        family = extract_command_family(tool_input.get("command", ""))
        result = {
            "decision": "block",
            "reason": (
                f"[loop_breaker] {tool_name} 命令族 '{family}' 已执行 {pattern_count} 次"
                f"（阈值 {pattern_limit}）— 虽然每次参数略有不同，但属于同一操作的反复重试。"
                f"请停下来重新审视方案。"
            ),
            "suggestion": (
                "1) 如果在尝试不同 ID/参数，说明搜索策略有误，换个方法；"
                "2) 如果在等待某个结果，说明轮询逻辑有问题，加退出条件；"
                "3) 如果已经拿到答案，请继续推进。"
            ),
        }
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        sys.exit(2)

    # warn: 接近阈值
    if exact_count >= exact_limit - WARN_MARGIN:
        sys.stderr.write(
            f"[loop_breaker] ⚠ {tool_name} × {exact_count + 1}次: '{entity}' — "
            f"接近精确阈值 {exact_limit}，再执行 {exact_limit - exact_count} 次将被阻止。\n"
        )
    elif pattern_count >= pattern_limit - WARN_MARGIN:
        family = extract_command_family(tool_input.get("command", ""))
        sys.stderr.write(
            f"[loop_breaker] ⚠ {tool_name} 命令族 '{family}' × {pattern_count}次 — "
            f"接近模式阈值 {pattern_limit}，再执行 {pattern_limit - pattern_count} 次将被阻止。\n"
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
