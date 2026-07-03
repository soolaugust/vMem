"""
llm_client.py — 轻量 LLM 调用封装（共享给 consolidation umbrella + PII 泛化重写）

复用 benchmarks/longmemeval/evaluate.py 的 anthropic SDK 同步范式：
  anthropic.Anthropic()（读 ANTHROPIC_API_KEY env）+ client.messages.create()

设计原则：优雅回退 — 无 API key / 无 anthropic 包 / 调用异常 → 返回 None，
调用方据此回退到确定性逻辑（consolidation 退字符串拼接；PII 泛化退不升级）。
lazy import，不在模块顶层 import anthropic，避免无依赖环境 import 失败。
"""
import os

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_CLIENT = None
_CLIENT_TRIED = False


def _get_client():
    """lazy 单例 client。无 key 或无包 → None。"""
    global _CLIENT, _CLIENT_TRIED
    if _CLIENT_TRIED:
        return _CLIENT
    _CLIENT_TRIED = True
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _CLIENT = None
        return None
    try:
        import anthropic
        _CLIENT = anthropic.Anthropic()
    except Exception:
        _CLIENT = None
    return _CLIENT


def llm_available() -> bool:
    """是否可调用 LLM（有 key + 有包）。"""
    return _get_client() is not None


def llm_complete(prompt: str, system: str = None,
                 model: str = DEFAULT_MODEL, max_tokens: int = 1024,
                 temperature: float = 0.0) -> str | None:
    """
    同步调用 LLM，返回文本；任何失败（无 key/无包/异常）→ None。

    Args:
        prompt: 用户消息内容
        system: 可选 system prompt
        model: 模型 ID（默认 haiku-4-5，廉价）
        max_tokens / temperature: 生成参数

    Returns:
        生成文本（已 strip）或 None
    """
    client = _get_client()
    if client is None:
        return None
    try:
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
        return response.content[0].text.strip()
    except Exception:
        return None
