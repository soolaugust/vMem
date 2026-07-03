"""
privacy_filter.py — seccomp BPF 内容过滤器（VFS 写入边界自动脱敏）

OS 类比：Linux seccomp BPF content filter (Will Drewry, 2012)
在 VFS write() 边界对 payload 进行正则扫描，命中 secret pattern 时
替换为 [REDACTED:{type}]，防止敏感信息持久化到 store.db。

性能目标：<1ms（预编译正则，单 pass alternation）
"""

import re
import json
import os

# ── 模块级编译正则（import 时一次性编译） ──────────────────────────────────

_PATTERNS: list[tuple[re.Pattern, str]] = []


def _build_patterns() -> list[tuple[re.Pattern, str]]:
    """构造预编译 secret 检测正则。按特异性排序，长 pattern 优先。"""
    specs = [
        # AWS Access Key ID
        (r'AKIA[0-9A-Z]{16}', 'aws_access_key'),
        # AWS Secret Key (in assignment context)
        (r'(?:aws_secret_access_key|secret_access_key|AWS_SECRET(?:_ACCESS_KEY)?)\s*[=:]\s*[A-Za-z0-9/+=]{20,}', 'aws_secret_key'),
        # GitHub tokens (PAT, OAuth, App, Fine-grained)
        (r'(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{36,255}', 'github_token'),
        # Anthropic API key
        (r'sk-ant-[A-Za-z0-9\-]{20,}', 'anthropic_key'),
        # OpenAI API key (sk-proj- or sk- prefix)
        (r'sk-(?:proj-)?[A-Za-z0-9]{20,}', 'openai_key'),
        # Bearer token in header
        (r'Bearer\s+[A-Za-z0-9._~+/\-=]{20,}', 'bearer_token'),
        # PEM private key block
        (r'-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----', 'private_key'),
        # Basic auth in URL (://user:password@host)
        (r'://[^:@\s]{1,64}:[^@\s]{1,128}@', 'url_basic_auth'),
        # Generic API key/secret/token assignment
        (r'(?:api[_\-]?key|api[_\-]?secret|secret[_\-]?key|access[_\-]?token)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{20,}["\']?', 'generic_api_key'),
    ]
    return [(re.compile(pattern, re.IGNORECASE), label) for pattern, label in specs]


_PATTERNS = _build_patterns()

# 合并为单个 alternation regex 用于快速 pre-check
_FAST_CHECK = re.compile(
    r'AKIA[0-9A-Z]|aws_secret|secret_access_key|AWS_SECRET'
    r'|gh[pors]_|github_pat_|sk-ant-|sk-(?:proj-)?[A-Za-z0-9]{20}'
    r'|Bearer\s+[A-Za-z0-9._~+/\-=]{20}'
    r'|-----BEGIN\s+.*?PRIVATE\s+KEY'
    r'|://[^:@\s]+:[^@\s]+@'
    r'|(?:api[_\-]?key|api[_\-]?secret|secret[_\-]?key|access[_\-]?token)\s*[=:]',
    re.IGNORECASE
)


def scrub_secrets(text: str) -> tuple[str, list[dict]]:
    """
    扫描 text 中的 secret pattern，替换为 [REDACTED:{type}]。

    Returns:
        (scrubbed_text, redaction_log)
        redaction_log: [{"type": "aws_access_key", "offset": 42, "length": 20}, ...]

    性能：<1ms for typical chunk text (<2000 chars)。
    快速路径：先用 _FAST_CHECK 做 O(1) pre-screen，无命中直接返回。
    """
    if not text:
        return text, []

    # 快速路径：大部分 text 不含 secret
    if not _FAST_CHECK.search(text):
        return text, []

    redaction_log = []
    result = text

    for pattern, label in _PATTERNS:
        offset_shift = 0
        for match in pattern.finditer(text):
            start = match.start()
            end = match.end()
            original_len = end - start
            replacement = f'[REDACTED:{label}]'

            redaction_log.append({
                "type": label,
                "offset": start,
                "length": original_len,
            })

    # 用 re.sub 一次性替换（避免 offset 漂移问题）
    if redaction_log:
        for pattern, label in _PATTERNS:
            replacement = f'[REDACTED:{label}]'
            result = pattern.sub(replacement, result)

    return result, redaction_log


# ── PII / 机器标识符检测（借鉴 komi-learn safety_floor）────────────────────
# 与 scrub_secrets（脱敏 secret）不同：这里检测 PII/项目/机器标识符，
# 命中者**永不升级到跨项目语义层**（__semantic__），对应 komi 的"永不 global"。
# 不脱敏、不拒绝项目内存储，只决定"能否跨项目泛化"，故误杀代价低。

_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 邮箱
    (re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'), 'email'),
    # 中国手机号
    (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), 'cn_phone'),
    # 国际电话（+国家码，宽松）
    (re.compile(r'(?<![\w.])\+\d{1,3}[\s\-]?\d{6,12}(?![\w.])'), 'intl_phone'),
    # 绝对用户路径（机器特定）
    (re.compile(r'/(?:home|Users)/[A-Za-z0-9_.\-]+'), 'user_path'),
    # 内网 IP（RFC1918）
    (re.compile(r'\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b'), 'private_ip'),
    # localhost:port（本机服务端点，机器/项目特定）
    (re.compile(r'\blocalhost:\d{2,5}\b'), 'localhost_port'),
    # 主机名特征（user@host.internal 形式，常见于命令/配置）
    (re.compile(r'\b[A-Za-z0-9_.\-]+@[A-Za-z0-9\-]+\.(?:local|internal|lan|corp)\b'), 'internal_host'),
]

# 快速预筛：无任一关键字符则跳过逐 pattern 扫描，保 <1ms
_PII_FAST_CHECK = re.compile(
    r'@|(?<!\d)1[3-9]\d{9}|/home/|/Users/|192\.168|10\.\d|172\.(?:1[6-9]|2\d|3[01])'
    r'|localhost:\d|\+\d{1,3}[\s\-]?\d{6}'
)


def detect_identifiers(text: str) -> list[str]:
    """
    检测文本中的 PII/机器/项目标识符，返回命中的类型列表（去重，空=干净）。

    用途：判定一条知识能否升级到跨项目语义层。性能：快速预筛 + 预编译正则，<1ms。
    """
    if not text:
        return []
    if not _PII_FAST_CHECK.search(text):
        return []
    hits = []
    for pattern, label in _PII_PATTERNS:
        if pattern.search(text):
            hits.append(label)
    return hits


def can_promote_to_semantic(text: str) -> bool:
    """
    含 PII/项目/机器标识符的知识不能进入跨项目语义层（__semantic__）。
    对应 komi-learn 的"永不 global"。
    """
    return not detect_identifiers(text)


def generalize_for_semantic(text: str, llm_complete=None) -> tuple[str, bool]:
    """
    LLM 泛化重写：去除项目/机器/个人特定标识，保留通用可复用知识。

    Returns:
        (text_out, promotable)
        - 有 llm_complete 且重写后通过二次 PII 检测 → (改写文本, True)
        - 重写后仍含标识符（komi 关键点：二次检测）→ (原文, False)，放弃升级
        - 无 llm_complete → (原文, can_promote_to_semantic(原文))，不做重写

    llm_complete: 可注入的 (prompt, system, max_tokens) -> str|None 函数（默认用 llm_client）。
    """
    if not text:
        return text, True
    # 已经干净，无需重写
    if can_promote_to_semantic(text):
        return text, True
    # 取 LLM
    if llm_complete is None:
        try:
            from memory_os.core.llm_client import llm_complete as _lc
            llm_complete = _lc
        except Exception:
            llm_complete = None
    if llm_complete is None:
        return text, False  # 无 LLM 且含标识符 → 不升级
    system = (
        "你是知识泛化助手。将给定的记忆改写为通用、可跨项目复用的形式："
        "去除所有项目名/机器路径/主机名/IP/邮箱/电话/个人用户名等特定标识，"
        "保留通用的事实、决策、原理与约束。不得编造信息，不得保留任何具体标识符。"
        "只输出改写后的文本，不要解释。"
    )
    try:
        rewritten = llm_complete(text, system=system, max_tokens=512)
    except Exception:
        rewritten = None
    if not rewritten:
        return text, False
    rewritten = rewritten.strip()
    # komi 核心：重写后二次跑 PII 检测
    if can_promote_to_semantic(rewritten):
        return rewritten, True
    return text, False  # 重写仍泄露 → 放弃，保守不升级


def load_extra_patterns() -> list[tuple[re.Pattern, str]]:
    """从 sysctl 加载用户自定义额外 pattern。"""
    try:
        from memory_os.config.sysctl import get
        extra_json = get("privacy.extra_patterns_json")
        if not extra_json or extra_json == "[]":
            return []
        specs = json.loads(extra_json)
        return [(re.compile(s["pattern"]), s["label"]) for s in specs
                if "pattern" in s and "label" in s]
    except Exception:
        return []
