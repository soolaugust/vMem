#!/usr/bin/env python3
"""
effort_router — UserPromptSubmit hook（省钱路由）

背景（事实，2026-06-03 查证 Claude Code 官方文档）：
  - /fast = Opus 完整推理 + 输出加速 → 每 token 成本更高，不省钱。
  - /effort low = 减少推理深度 → 真正省 token（适合简单/机械任务）。
  - Haiku ≈ Opus 的 ~1/10 价 → 简单活儿可换小模型。
  Claude Code 没有"按任务类型自动切模型"的官方机制：hook 不能改
  model/effort，只能注入 additionalContext 提示。本 hook 即唯一合法的
  半自动手段——检测到纯简单操作时，提示 Claude 自觉降低 effort。

触发策略（宁缺毋滥，避免噪音）：
  命中"简单操作信号" 且 不含"复杂任务信号" → 注入一条降档建议。
  其余情况一律静默退出。

约束（照搬 parallel_hint.py 范式）：
  - 不修改用户 prompt（hooks 不支持）。
  - additionalContext ≤ 150 字。
  - 全程 try/except，失败绝不阻塞 UserPromptSubmit。
"""

import sys
import json
import re
from pathlib import Path

_ROOT = Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context_governor import prompt_text  # noqa: E402
from lib.context_pressure import should_shed_optional_context  # noqa: E402

MAX_NOTICE_LEN = 150

# ── 简单操作信号：机械、确定性、低推理需求 ──────────────────────
# 文件浏览 / git 只读 / 搜索 / 格式化 / 简单重命名 / 列目录
SIMPLE_SIGNALS = re.compile(
    r"(?:"
    r"\bls\b|列目录|看一下目录|目录结构|"
    r"git\s+(?:status|log|diff|show|branch)|查看?(?:git)?状态|"
    r"\bgrep\b|搜索|查找(?:一下)?|找一下|"
    r"格式化|format\b|缩进|对齐代码|"
    r"重命名|改个?名|rename|"
    r"加(?:个)?注释|补注释|写注释|"
    r"\bcat\b|读(?:一下)?文件内容|看看这个文件|"
    r"删(?:除|掉)?(?:这)?(?:行|个空行|多余)|去掉空行"
    r")",
    re.IGNORECASE,
)

# ── 复杂任务信号：需要 Opus 完整推理，命中则不建议降档 ──────────
COMPLEX_SIGNALS = re.compile(
    r"(?:"
    r"分析|根因|为什么|why|设计|架构|重构|refactor|"
    r"调试|debug|排查|定位(?:问题|bug)|"
    r"优化(?:算法|性能|逻辑)|"
    r"实现|开发|写(?:一个|个)?(?:功能|模块|脚本|程序|算法)|"
    r"评估|权衡|对比方案|推导|证明|"
    r"修(?:复)?(?:这个)?(?:bug|缺陷|race|死锁|崩溃)|"
    r"复杂|并发|race|lock|死锁"
    r")",
    re.IGNORECASE,
)


def _should_hint(text: str) -> bool:
    """命中简单信号且无复杂信号 → 建议降档。"""
    sample = text[:400]
    if COMPLEX_SIGNALS.search(sample):
        return False
    return bool(SIMPLE_SIGNALS.search(sample))


def main():
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    if should_shed_optional_context(hook_input):
        sys.exit(0)

    prompt = prompt_text(hook_input)
    # 太短（如"继续""ls"）或太长（多半是复杂任务）都跳过
    if not prompt or not (8 <= len(prompt) <= 200):
        sys.exit(0)

    try:
        if not _should_hint(prompt):
            sys.exit(0)

        notice = (
            "[省钱路由] 此任务偏简单/机械。建议本轮用 /effort low 减少推理深度，"
            "或纯机械活儿分派 Task(model=haiku，约 1/10 价)。注意 /fast 更贵不省钱。"
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": notice[:MAX_NOTICE_LEN],
            }
        }, ensure_ascii=False))
    except Exception:
        pass  # 永远不阻塞用户输入

    sys.exit(0)


if __name__ == "__main__":
    main()
