#!/usr/bin/env python3
"""
memory-os parallel_hint — UserPromptSubmit hook
迭代110 P4: Multi-Agent Parallel Scheduling Hint

OS 类比: Linux CFS Work-Stealing Scheduler (2007) + fork-exec 并行
  当 CPU 空闲时，从其他 CPU 的运行队列"偷取"就绪任务并行执行。
  目标：消除顺序等待，最大化 CPU 利用率。

AIOS 类比: 检测用户 prompt 中的独立并行子任务，
  注入 additionalContext 提示 Claude 使用 Agent tool 并行执行，
  而非默认的顺序串行处理。

检测信号（3 类）：
  P0 显式并行请求："分别"/"各自"/"同时"/"并行"/"一起" + 多对象
  P1 列表型任务：编号列表（1. 2. 3.）或破折号列表中的 3+ 独立项
  P2 比较型任务："A 和 B 分别"/"对比 X 和 Y"/"分析 X、Y、Z"

注入策略：
  - 只在检测到 2+ 独立可并行任务时才注入（避免噪音）
  - 注入一条简短提示："[CFS] 检测到 N 个独立子任务，可用 Agent tool 并行执行"
  - 如果任务明显串行（含"然后"/"之后"/"先...再"），跳过注入

约束：
  - 不修改用户 prompt（hooks 不支持）
  - additionalContext 最多 200 字
  - 全程 try/except，失败不影响 UserPromptSubmit
"""

import sys
import json
import re
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.context_pressure import prompt_text, should_shed_optional_context
from lib.prompt_io import emit_user_prompt_context, read_hook_input

MAX_NOTICE_LEN = 200

CONTINUE_PATTERNS = [
    re.compile(r"^\s*continue\s+from\s+where\s+you\s+left\s+off\s*[\.!。！]*\s*$", re.I),
    re.compile(r"^\s*继续(之前|上次|刚才)?(的)?(任务|工作|loop|成长 loop).*$", re.I),
]

# ── 串行依赖信号（存在则不建议并行）──────────────────────────
SERIAL_SIGNALS = re.compile(
    r'(?:然后|之后|接着|再(?:去|做|看|分析|检查)|先.*?(?:再|然后)|完成.*?(?:再|然后)|等.*?(?:再|完成))',
    re.IGNORECASE
)

# ── P0: 显式并行词 + 多对象 ────────────────────────────────
PARALLEL_EXPLICIT = re.compile(
    r'(?:分别|各自|同时|并行|一并|同步)(?:.{0,30}(?:和|与|、|，|,))',
    re.IGNORECASE
)

# ── P1: 编号列表（3+ 项）──────────────────────────────────
NUMBERED_LIST = re.compile(
    r'(?:^|\n)\s*(?:[①②③④⑤]|\d+[.。、）)]\s+.{5,})',
    re.MULTILINE
)

# ── P2: 对比型任务 ────────────────────────────────────────
COMPARISON = re.compile(
    r'(?:对比|比较|分析)\s*.{2,20}(?:和|与|、)\s*.{2,20}'
    r'|.{2,20}(?:和|与|、).{2,20}(?:分别|各自|哪个|哪种)',
    re.IGNORECASE
)

# ── P3: 枚举对象（3+ 个用顿号/逗号连接的项）────────────────
ENUM_OBJECTS = re.compile(
    r'(?:[^\n，,、]{2,15}(?:[，,、])){2,}[^\n，,、]{2,15}',
    re.IGNORECASE
)


def is_continue_prompt(prompt: str) -> bool:
    return any(pattern.search(prompt) for pattern in CONTINUE_PATTERNS)


def _count_parallel_signals(text: str) -> tuple[int, list[str]]:
    sample = text[:500]
    signals = []

    if SERIAL_SIGNALS.search(sample):
        serial_count = len(SERIAL_SIGNALS.findall(sample))
        if serial_count >= 2:
            return 0, []

    if PARALLEL_EXPLICIT.search(sample):
        signals.append("显式并行")

    numbered_items = NUMBERED_LIST.findall(text[:1000])
    if len(numbered_items) >= 3:
        signals.append(f"列表{len(numbered_items)}项")

    if COMPARISON.search(sample):
        signals.append("对比分析")

    enum_matches = ENUM_OBJECTS.findall(sample)
    if enum_matches:
        for match in enum_matches:
            parts = re.split(r'[，,、]', match)
            if len(parts) >= 3 and all(2 <= len(part.strip()) <= 15 for part in parts):
                signals.append(f"枚举{len(parts)}个对象")
                break

    return len(signals), signals


def _extract_task_count(text: str) -> int:
    """粗略估计独立子任务数量。"""
    # 编号列表最准确
    numbered = NUMBERED_LIST.findall(text[:1000])
    if len(numbered) >= 2:
        return len(numbered)
    # 顿号枚举
    enum_matches = ENUM_OBJECTS.findall(text[:500])
    for m in enum_matches:
        parts = re.split(r'[，,、]', m)
        if len(parts) >= 2:
            return len(parts)
    return 2  # 默认 2


def main():
    hook_input = read_hook_input()
    prompt = prompt_text(hook_input)
    if is_continue_prompt(prompt):
        sys.exit(0)
    if should_shed_optional_context(hook_input):
        sys.exit(0)
    if not prompt or len(prompt) < 10:
        sys.exit(0)

    try:
        signal_count, reasons = _count_parallel_signals(prompt)
        if signal_count < 2:
            sys.exit(0)

        task_count = _extract_task_count(prompt)
        reason_str = "、".join(reasons[:2])

        # iter1052: 升级提示——明确指导子 Agent 把结果写入 memory-os
        # 子 Agent 完成任务后调用 mcp__memory-os__memory_lookup 可读取其他子任务结果
        # 主 session 通过 memory-os 检索汇总，无需等待所有子 Agent 完成
        notice = (
            f"[CFS] 检测到 {task_count} 个独立子任务（{reason_str}）。"
            f"建议：用 Agent tool 并行执行各子任务；"
            f"每个子 Agent 完成后将结论写入 memory-os（mcp__memory-os__write_memory），"
            f"主 session 通过 memory_lookup 汇总结果。"
        )

        emit_user_prompt_context(notice[:MAX_NOTICE_LEN])

    except Exception:
        pass  # 永远不阻塞用户输入

    sys.exit(0)


if __name__ == "__main__":
    main()
