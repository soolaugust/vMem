"""
hooks/defang.py — 检索注入防御（借鉴 komi-learn _sanitize / prompt injection defense）

OS 类比：input sanitization at the trust boundary（类比 copy_from_user 校验）。
检索出的 chunk 在注入 SessionStart additionalContext 之前必须清洗：store 里的内容
（尤其外部导入 / 跨项目 / 历史会话来源）可能携带 prompt injection payload，
若原样注入，可能突破 data fence、伪造 turn 边界、冒充 system/assistant 指令。

清洗步骤（对齐 komi _sanitize）：
  1. 剥离 XML/HTML-ish 标签（含自定义 fence，如 <komi-recall>、<system> 等）
  2. defang 角色标记（System:/Assistant:/Human:/<|im_start|> 等）→ 用全角冒号破坏 turn 边界但保留可读
  3. 丢弃控制字符
  4. collapse 连续空白

性能：预编译正则，单条 <0.1ms。注入时对每条 summary + raw_snippet 调用。
"""
import re

# ── 预编译正则（import 时一次性编译）──────────────────────────────────────

# 1. XML/HTML-ish 标签：<tag>、</tag>、<tag attr="...">，限标签本身（不删内容）
#    长度上限 80 防止吞掉正常文本中的 < ... >（如代码片段中的比较表达式偏长）
_TAG_RE = re.compile(r'</?[A-Za-z!|][^>]{0,80}>')

# 2. 角色标记：行首或独立出现的 turn 边界标志
#    - 文本型：System:/Assistant:/Human:/User: （行首，可前导空白）
#    - ChatML 型：<|im_start|> <|im_end|> <|system|> 等（已被 _TAG_RE 部分覆盖，
#      但 <| |> 形式需单独处理，因为含 |）
_ROLE_LINE_RE = re.compile(
    r'(?im)^[ \t]*(System|Assistant|Human|User|系统|助手)[ \t]*:',
)
_CHATML_RE = re.compile(r'<\|[A-Za-z_]+\|>')

# 3. 控制字符（保留 \t \n \r）
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

# 4. 连续空白（含换行）collapse 为单空格
_WS_RE = re.compile(r'\s+')

# 全角冒号，用于 defang 角色标记（破坏 turn 边界但保留可读语义）
_FULLWIDTH_COLON = '：'


def defang(text: str) -> str:
    """
    清洗一段将要注入 LLM context 的文本，中和潜在的 prompt injection。

    返回清洗后的单行文本（空白已 collapse）。空输入返回空串。
    """
    if not text:
        return ""
    # 1. ChatML 标记先处理（含 | 不被 _TAG_RE 完全覆盖）
    t = _CHATML_RE.sub(' ', text)
    # 2. 剥离 XML/HTML-ish 标签
    t = _TAG_RE.sub('', t)
    # 3. defang 角色标记：冒号替换为全角，使其不再是 turn 边界
    t = _ROLE_LINE_RE.sub(lambda m: m.group(0)[:-1] + _FULLWIDTH_COLON, t)
    # 4. 丢弃控制字符
    t = _CTRL_RE.sub('', t)
    # 5. collapse 空白
    t = _WS_RE.sub(' ', t).strip()
    return t
