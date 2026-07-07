#!/usr/bin/env python3
"""
assertion_guard.py — SessionStart hook：自动跑生产断言（闭环的「强制执行」层）

根因（2026-06-05）：production_assertions.py 能抓死链，但没有任何调度器触发它
（不在 cron / hooks / metabot）。监控退化成「人想起来才体检」——apply_signal
死链因此隐藏了数周。

修复：把断言挂到「必然发生」的事件——SessionStart。每次会话开始自动跑，
  - 红了：把摘要 + 复发告警注入会话上下文（additionalContext），让 agent
    一开局就看到，不依赖主动检索；
  - 健康：静默（不污染上下文，符合「数据优先 / 动效克制」）。

「接线≠在跑」防复发自检：本脚本被 hooks.json 的 SessionStart 注册后，
  必须验证它真的执行——见文件末尾 __main__ 的 self-check 提示。
"""

import json
import os
import sys
from pathlib import Path

# 让脚本能 import 同目录上层的 production_assertions / assertion_history
_HERE = Path(__file__).resolve().parent
_MEMOS = _HERE.parent
for p in (str(_MEMOS), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _build_context() -> str:
    """跑断言，返回需注入上下文的文本（健康则返回 ""）。"""
    try:
        import memory_os.observability.production_assertions as pa
        import memory_os.observability.assertion_history_compat as ah
    except Exception as e:
        return ""  # 模块缺失绝不阻塞会话启动

    try:
        report = pa.run_all(fix=False)
    except Exception:
        return ""

    status = report.get("status", "UNKNOWN")
    rec = report.get("recurrence") or {}

    # 健康且无复发 → 静默
    if status == "HEALTHY" and not rec.get("escalate") and not rec.get("recurred"):
        return ""

    # 复发断言名集合：复发的 critical 不截断 message（保留诊断线索）
    recurred_names = {x["name"] for x in rec.get("recurred", [])} | \
                     {x["name"] for x in rec.get("escalate", [])}

    lines = []
    s = report.get("summary", {})
    if status != "HEALTHY":
        failed = [r for r in report.get("results", []) if not r.get("passed")]
        crit = [r for r in failed if r.get("severity") == "critical"]
        icon = "❌" if crit else "⚠️"
        tag = f"{len(crit)} 项 critical" if crit else f"{len(failed)} 项 fail(非critical)"
        lines.append(
            f"{icon} 生产断言 {status}：{s.get('passed')}/{s.get('total')} 通过，{tag}")
        # critical 优先展示；无 critical 时展示所有 fail
        for r in (crit or failed)[:4]:
            msg = r.get("message", "")
            # 复发的 critical：完整展示（诊断线索往往在尾部，如"检查 suppress_unused..."）
            # 非复发：截断 90 字符控制上下文预算
            if r["name"] in recurred_names and r.get("severity") == "critical":
                lines.append(f"  ✗🔁 [{r['name']}] {msg}")
            else:
                lines.append(f"  ✗ [{r['name']}] {msg[:90]}")

    # 复发告警最重要——单独突出
    try:
        alert = ah.format_recurrence_alert(rec)
        if alert:
            lines.append(alert)
    except Exception:
        pass

    if not lines:
        return ""
    return ("【生产断言自检 · SessionStart】\n" + "\n".join(lines) +
            "\n（运行 `python3 aios/memory-os/production_assertions.py` 查看完整报告）")


def main():
    # 读 hook stdin（SessionStart 会传，但本 hook 不依赖其内容）
    try:
        sys.stdin.read()
    except Exception:
        pass

    context = _build_context()
    if not context:
        # 健康：不输出 additionalContext，静默退出
        sys.exit(0)

    try:
        from context_governor import enforce_additional_context
        output = enforce_additional_context(
            None,
            context,
            producer="assertion_guard",
            hook_event_name="SessionStart",
            mandatory=True,
            max_chars=1200,
        )
    except Exception:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context[:1200],
            }
        }
    if output:
        print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    # self-check 模式：python3 assertion_guard.py --selfcheck
    # 验证「接线≠在跑」——直接调用看是否产出，而非假设 hook 会跑。
    if "--selfcheck" in sys.argv:
        ctx = _build_context()
        if ctx:
            print(ctx)
        else:
            print("✓ 断言全绿，SessionStart 将静默（无 context 注入）")
        sys.exit(0)
    main()
