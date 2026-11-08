"""判断哪些文本单元不应该翻译（数字、公式、URL、代码等）。"""

from __future__ import annotations

import re
from typing import Iterable

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
URL_ONLY_RE = re.compile(
    r"^(?:https?://|ftp://|www\.)\S+$"
    r"|^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$"
    r"|^doi:\s*\S+$",
    re.IGNORECASE,
)
LATEX_RE = re.compile(r"\\[A-Za-z]{2,}|\^\{|\}_\{")
CODE_FONT_RE = re.compile(
    r"(courier|consol|mono|code|menlo|monaco|sfmono|lucidaconsole)", re.IGNORECASE
)
MATH_FONT_RE = re.compile(
    r"(symbol|math|cmmi|cmsy|cmex|stix|mathjax)", re.IGNORECASE
)
CODE_START_RE = re.compile(
    r"^(SELECT\s|INSERT\s|UPDATE\s|DELETE\s|CREATE\s|DROP\s|ALTER\s"
    r"|def\s|class\s|import\s|from\s+\S+\s+import\s|#include\s"
    r"|public\s|private\s|protected\s|static\s|void\s"
    r"|function\s|const\s|let\s|var\s|return\s"
    r"|print\(|console\.log|if\s*\(|for\s*\(|while\s*\()",
    re.IGNORECASE,
)
CODE_PUNCT_RE = re.compile(r"[{};=<>\[\]]")
NUMBER_RE = re.compile(r"^[\s\d.,:;%()+\-–—/]+$")

# 参考文献条目、作者/单位、DOI/arXiv、图内小标注
REFERENCE_ENTRY_RE = re.compile(r"^\[\d+\]\s*")
DOI_RE = re.compile(r"\b10\.\d{4,9}/\S+", re.IGNORECASE)
ARXIV_RE = re.compile(r"arxiv:\s*\d{4}\.\d{4,5}", re.IGNORECASE)
CAPTION_RE = re.compile(r"^(figure|table|fig\.|algorithm)\s*\d+", re.IGNORECASE)
ALL_CAPS_RE = re.compile(r"^[A-Z][A-Z0-9&\-]{1,7}$")
INSTITUTION_RE = re.compile(
    r"\b(University|Institute|Department|Laborator(?:y|ies)|School|College|"
    r"Academy|Research Center|Inc\.?|Corporation|Company)\b",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")

# 中文行首禁则：这些标点不应出现在一行的开头
NO_LINE_START = set("，。、；：！？）》」』】”’%％,.;:!?)]}")


def _latin_count(text: str) -> int:
    return len(LATIN_RE.findall(text))


def _cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text))


def classify(text: str, fonts: Iterable[str] = (), size: float = 0.0) -> tuple[bool, str]:
    """返回 (是否需要翻译, 跳过原因)。"""
    raw = text or ""
    text = " ".join(raw.split())
    if not text:
        return False, "空文本"

    latin = _latin_count(text)
    cjk = _cjk_count(text)
    total_letters = latin + cjk
    if latin == 0:
        return False, "无英文（数字/符号）"
    if cjk and cjk / max(total_letters, 1) > 0.4:
        return False, "已是中文"

    if URL_ONLY_RE.match(text):
        return False, "URL/邮箱"
    if NUMBER_RE.match(text):
        return False, "纯数字"
    if re.fullmatch(r"(?:[\d.,]+\s*(?:x|×|%|s|ms|J|MB|GB)?\s*)+", text):
        return False, "数字/倍率"
    # 只挡「M3 [3]」「FastV [4]」这种就是方法名加编号的短标签；带 ≥4 个词的
    # 句子（「M3[3] applies model fine-tuning」）该翻，不能因为一句引用编号就丢掉。
    if re.search(r"\[\d+\]", text) and len(text.split()) <= 3:
        return False, "方法名/引用编号"
    if REFERENCE_ENTRY_RE.match(text):
        return False, "参考文献条目"
    if (DOI_RE.search(text) or ARXIV_RE.search(text)) and len(text.split()) <= 10:
        return False, "DOI/arXiv 编号"
    if ALL_CAPS_RE.match(text):
        return False, "缩写/符号"

    fonts = [f or "" for f in fonts]
    if fonts:
        math_fonts = sum(1 for f in fonts if MATH_FONT_RE.search(f))
        if math_fonts / len(fonts) >= 0.5:
            return False, "公式字体"
        mono_fonts = sum(1 for f in fonts if CODE_FONT_RE.search(f))
        if mono_fonts / len(fonts) >= 0.6:
            return False, "等宽字体代码"

    if LATEX_RE.search(text):
        return False, "公式"

    if CODE_START_RE.match(text):
        return False, "代码"
    code_marks = len(CODE_PUNCT_RE.findall(text))
    if code_marks >= 3 and code_marks / len(text) > 0.18 and text.count(";") >= 1:
        return False, "代码"

    if re.search(r"[ˆ∑∏√∂∇∫∥≤≥−∈≈≠±]", text) and len(text.split()) <= 8:
        return False, "公式"
    if (
        " " not in text.strip()
        and len(text) <= 14
        and re.search(r"[A-Z]{2,}", text)
        and re.search(r"[,^_=]", text)
    ):
        return False, "公式/符号"
    if re.fullmatch(r"(?:min|max|s\.t\.)\b.*", text.strip(), re.IGNORECASE) and len(text.split()) <= 6:
        return False, "公式"
    # 单个字母/下标组成的公式碎片（"w D r x d"）：没有可译的自然语言
    if 1 < len(text.split()) <= 6 and all(
        len(word.strip(".,;:()")) == 1 for word in text.split()
    ):
        return False, "公式/符号"
    # 图例/表头的单位或模型缩写（GFLOPs、DeiTs、ViTs）：保留原文
    if re.fullmatch(r"[A-Z][A-Za-z]*[A-Z]s", text):
        return False, "单位/缩写"
    operators = len(re.findall(r"[=+\-*/^_<>|\\]", text))
    if operators >= 3 and operators > latin * 0.7:
        return False, "公式"
    if "=" in text and len(text.split()) <= 14:
        words = re.findall(r"[a-z]{4,}", text)
        math_marks = len(re.findall(r"[=×÷±ˆ∑√^_<>]", text))
        if not words or text.count("=") >= 2 or math_marks >= 3:
            return False, "公式"
    if "=" in text and len(text.split()) <= 6 and (
        operators >= 2 or re.search(r"[A-Za-z0-9]\s*[\^_]\s*\d", text)
    ):
        return False, "公式"

    # 图内坐标/图例/流程标签通常是 4~6.5pt 的短文本，不参与翻译，避免极窄块反复截断
    if size and size < 6.9 and not CAPTION_RE.match(text):
        if len(text) <= 120 and len(text.split()) <= 14:
            return False, "图形内标注"

    # 单字母/单符号没有翻译价值
    if latin == 1 and len(text) <= 3:
        return False, "字母缩写/符号"

    return True, ""


# 行内数学、DOI/arXiv 等需要在翻译前后原样保留的片段
_MATH_PATTERNS = [
    re.compile(r"\$[^$]{1,120}\$"),
    re.compile(r"\\\([^()]{1,100}\\\)"),
    re.compile(r"\\\[[^\[\]]{1,140}\\\]"),
    re.compile(r"arxiv:\s*\d{4}\.\d{4,5}", re.IGNORECASE),
    re.compile(r"\b10\.\d{4,9}/[^\s,;]+"),
    re.compile(
        r"\b[A-Za-z][A-Za-z0-9_]{0,7}\s*=\s*[A-Za-z0-9_.]{1,12}"
        r"(?:\s*[+\-*/^×÷]\s*[A-Za-z0-9_.]{1,12})*"
    ),
    re.compile(r"\b[A-Za-z0-9)]\s*[\^_]\s*[A-Za-z0-9{}]{1,8}"),
]
_PLACEHOLDER_RE = re.compile(r"⟦\s*M(\d+)\s*⟧")


def protect_inline_math(text: str) -> tuple[str, list[str]]:
    """把行内公式替换为占位符，返回（带占位符文本，原文片段列表）。"""
    spans: list[str] = []

    def replace(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"⟦M{len(spans)}⟧"

    protected = text or ""
    for pattern in _MATH_PATTERNS:
        protected = pattern.sub(replace, protected)
    return protected, spans


def restore_inline_math(text: str, spans: list[str]) -> str:
    """把占位符换回原文公式；译文漏掉的公式补在末尾，避免符号丢失。"""
    if not spans:
        return text

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 1 <= index <= len(spans):
            return spans[index - 1]
        return match.group(0)

    present = {int(item) for item in _PLACEHOLDER_RE.findall(text)}
    restored = _PLACEHOLDER_RE.sub(replace, text)
    missing = [span for i, span in enumerate(spans, start=1) if i not in present]
    if missing:
        restored = restored.rstrip() + " " + " ".join(missing)
    return restored

