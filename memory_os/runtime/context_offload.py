"""
context_offload.py — Context Offload Protocol (demand paging)

OS 类比：Linux demand paging — 页表条目只记录页框号（8 bytes），
实际页内容（4KB）仅在缺页时加载。

当 context pressure 升高时，将完整注入切换为 compact reference 模式：
- none  → full injection（当前行为不变）
- some  → offload mode（compact ref + drill-down 提示）
- full  → minimal mode（仅 pinned chunk 的 ref）

典型压缩效果：full=44 tokens/chunk → offload=15 tokens/chunk (66% 节省)
"""

from typing import Optional


# ── 注入 icon 映射 ──────────────────────────────────────────────────────
_ICONS = {
    "decision": "💡",
    "design_constraint": "⚠️",
    "reasoning_chain": "🔗",
    "quantitative_evidence": "📊",
    "excluded_path": "🚫",
    "causal_chain": "⛓️",
    "procedure": "📋",
    "composite": "📦",
}


def format_full(chunks: list, max_chars: int = 800) -> str:
    """
    Full injection — 当前行为（pressure=none 时使用）。
    每条 chunk 完整注入 summary + raw_snippet。
    """
    lines = []
    total = 0
    for i, c in enumerate(chunks, 1):
        icon = _ICONS.get(c.get("chunk_type", ""), "💭")
        ct = c.get("chunk_type", "unknown")
        summary = (c.get("summary") or "")[:200]
        line = f"{icon} [{ct}] {summary}"

        rs = c.get("raw_snippet", "")
        if rs:
            line += f"（原文：{rs[:150]}）"

        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1

    return "\n".join(lines)


def format_offload(chunks: list, max_chars: int = 400) -> str:
    """
    Offload mode — compact reference（pressure=some 时使用）。
    每条 chunk 只注入 [ref:id] + chunk_type + summary[:30]。
    Agent 需要细节时调用 memory_lookup 查询。
    """
    lines = []
    total = 0
    for c in chunks:
        cid = c.get("id", "")[:8]
        ct = c.get("chunk_type", "")
        summary = (c.get("summary") or "")[:30]
        line = f"[ref:{cid}] {ct}: {summary}"

        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1

    if lines:
        lines.append("→ 需要详情请 memory_lookup 查询对应主题")
    return "\n".join(lines)


def format_minimal(chunks: list, max_chars: int = 200) -> str:
    """
    Minimal mode — 极度压缩（pressure=full 时使用）。
    仅保留 pinned 或 importance >= 0.8 的 chunk ref。
    """
    lines = []
    total = 0
    for c in chunks:
        imp = float(c.get("importance", 0.5))
        if imp < 0.8:
            continue
        cid = c.get("id", "")[:8]
        ct = c.get("chunk_type", "")
        summary = (c.get("summary") or "")[:20]
        line = f"[ref:{cid}] [{ct}] {summary}"

        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1

    return "\n".join(lines)


def format_chunks(chunks: list, pressure: str = "none",
                   max_chars: int = 800) -> str:
    """
    根据 context pressure 选择注入格式。

    Args:
        chunks: retrieval top-k chunks（含 id, summary, chunk_type, importance 等）
        pressure: "none" | "some" | "full"（来自 context_cgroup）
        max_chars: 最大注入字符数
    Returns:
        格式化的注入文本
    """
    if pressure == "full":
        return format_minimal(chunks, max_chars=min(max_chars, 200))
    elif pressure == "some":
        return format_offload(chunks, max_chars=min(max_chars, 400))
    else:
        return format_full(chunks, max_chars=max_chars)


def measure_compression(chunks: list) -> dict:
    """对比三种模式的 token 估算（用于 benchmark）。"""
    full_text = format_full(chunks, max_chars=99999)
    offload_text = format_offload(chunks, max_chars=99999)
    minimal_text = format_minimal(chunks, max_chars=99999)

    full_tokens = len(full_text) // 4
    offload_tokens = len(offload_text) // 4
    minimal_tokens = len(minimal_text) // 4

    return {
        "full_chars": len(full_text),
        "full_tokens_est": full_tokens,
        "offload_chars": len(offload_text),
        "offload_tokens_est": offload_tokens,
        "minimal_chars": len(minimal_text),
        "minimal_tokens_est": minimal_tokens,
        "offload_savings_pct": round((1 - offload_tokens / max(full_tokens, 1)) * 100, 1),
        "minimal_savings_pct": round((1 - minimal_tokens / max(full_tokens, 1)) * 100, 1),
    }
