"""中文译文后处理：全角标点、空格清理、数字/单位保护、重复退化修复。

只做保守的机械修复，不改写句意，避免把本来正确的译文改坏。
"""

from __future__ import annotations

import re

CJK = r"\u3400-\u4dbf\u4e00-\u9fff"
CJK_RE = re.compile(f"[{CJK}]")
LATIN_RE = re.compile(r"[A-Za-z]")

# 这些片段在标点转换前先整体藏起来，避免把小数点、版本号、时间、URL 改坏。
_MASK_PATTERNS = (
    # 行内/行间公式整体藏起来：全角化不能碰 \( \) $ $，否则会变成 \（ \）。
    re.compile(r"\\\(.+?\\\)|\\\[.+?\\\]|\$\$?.+?\$\$?", re.S),
    re.compile(r"(?:https?://|ftp://|www\.)\S+|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\[\d{1,3}(?:\s*[,–—\-]\s*\d{1,3})*\]"),  # 引用编号 [8, 11–13]
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)+\b"),  # Node.js / v1.2 / U.S.
    re.compile(r"\b\d{1,4}(?:[-/.]\d{1,2}){1,2}\b"),  # 2025-01-01 / 05/06/2025
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),  # 10:30
    re.compile(r"\b\d+(?:[.,]\d+)*\s*%?"),  # 1,000.50 / 18%
    re.compile(
        r"\b\d+(?:\.\d+)?\s*"
        r"(?:kg|g|mg|t|km|m|cm|mm|nm|MB|GB|TB|KB|kWh|Wh|W|kW|V|A|mA|Hz|kHz|MHz|GHz|ms|s|°C|°F)\b"
    ),
)

# 占位符带字母前缀，避免后续「纯数字」规则把占位符里的序号再次mask。
_PLACEHOLDER_RE = re.compile(r"\x00P(\d+)\x00")

_PUNCT_MAP = {
    ",": "，",
    ";": "；",
    "?": "？",
    "!": "！",
    "(": "（",
    ")": "）",
}

_DUP_PUNCT_RE = re.compile(r"([，。！？；：、])\1+")
_DUP_PHRASE_RE = re.compile(r"([\u4e00-\u9fff]{3,12})\1+")


def _mask(text: str) -> tuple[str, dict[str, str]]:
    masked: dict[str, str] = {}

    def keep_match(match: re.Match[str]) -> str:
        token = match.group(0)
        key = f"\x00P{len(masked)}\x00"
        masked[key] = token
        return key

    for pattern in _MASK_PATTERNS:
        text = pattern.sub(keep_match, text)
    return text, masked


def _unmask(text: str, masked: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: masked.get(m.group(0), m.group(0)), text)


def _cjk_ratio(text: str) -> float:
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    return cjk / max(cjk + latin, 1)


def fix_punctuation(text: str) -> str:
    """半角标点转全角；数字、单位、日期、URL、代码片段不受影响。"""
    if not text:
        return text
    text, masked = _mask(text)
    text = text.replace("...", "……")

    if _cjk_ratio(text) >= 0.3:
        for half, full in _PUNCT_MAP.items():
            text = text.replace(half, full)
        # 数字之间的冒号（如 3:1）保持半角
        text = re.sub(r"(?<!\d):", "：", text)
        # 只把“英文句点 + 空格 + 中文”改成句号；保留 4. / Fig. 3 / 3.5 这类编号和小数
        text = re.sub(r"(?<!\d)\.(?=\s*[\u4e00-\u9fff])", "。", text)
        text = _unmask(text, masked)
        # 章节编号（“2。相关工作”“3.1。预备知识”）里的句号恢复为半角编号点。
        text = re.sub(
            r"(?m)(^|\n)(\d{1,2}(?:\.\d{1,2})*)\s*。",
            r"\1\2. ",
            text,
        )
        text = re.sub(r"(?m)(^|\n)(\d{1,2})。", r"\1\2. ", text)
        # 列表项被并进同一段时（“…如下：1。首先…”），编号后的句号也恢复成半角点
        return re.sub(r"(?<![\d.])(\d{1,2})。(?=[\u4e00-\u9fff])", r"\1. ", text)

    return _unmask(text, masked)


def fix_numbering_dots(text: str) -> str:
    """把“1。步骤”这类编号后的全角句号恢复成半角点（只动 1–2 位编号 + 汉字）。"""
    if not text:
        return text
    text = re.sub(
        r"(?m)(^|\n)(\d{1,2}(?:\.\d{1,2})*)\s*。", r"\1\2. ", text
    )
    return re.sub(r"(?<![\d.])(\d{1,2})。(?=[\u4e00-\u9fff])", r"\1. ", text)


def clean_spaces(text: str) -> str:
    """清理中英文之间、中文标点两侧的多余空格。"""
    if not text:
        return text
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +([，。、；：！？）】」』”’])", r"\1", text)
    text = re.sub(r"([（【「『“‘]) +", r"\1", text)
    text = re.sub(r"(?<=[，。、；：！？）】」』”’]) +", "", text)
    # 模型偶尔会在中文后面残留英文冠词：和 a RTX 4090 -> 和RTX 4090
    text = re.sub(r"(?<=[\u4e00-\u9fff]) +(?:\ba|\ban|\bA|\bAn) +(?=[A-Z0-9])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[A-Za-z0-9])", "", text)
    text = re.sub(r"(?<=[A-Za-z0-9]) +(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return text.strip()


def fix_repetition(text: str) -> str:
    """抑制模型常见的重复退化：的的、叠词病句、重复标点。"""
    if not text:
        return text
    text = text.replace("的的", "的")
    text = text.replace("了了", "了")
    text = _DUP_PHRASE_RE.sub(r"\1", text)
    text = _DUP_PUNCT_RE.sub(r"\1", text)

    # 相邻重复短句：只合并完全相同的句子，避免误伤正常排比。
    parts = re.split(r"([。！？；])", text)
    if len(parts) >= 3:
        rebuilt: list[str] = []
        previous = ""
        for index in range(0, len(parts) - 1, 2):
            body = parts[index].strip()
            mark = parts[index + 1]
            if body and body == previous:
                continue
            rebuilt.append(body + mark)
            previous = body
        if parts[-1].strip():
            rebuilt.append(parts[-1].strip())
        text = "".join(rebuilt)
    return text


_ECHO_SPLIT_RE = re.compile(r"\s*[、,，/／]\s*")
# 清单项：以拉丁字母开头、短、不含中文和句末标点（MMDP / LLaVA 1.5-7B / Max-Min）
_ECHO_ITEM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 .+\-]{0,23}$")
_ECHO_MARKS = ("：", ":", "，", ",")
# 这些词收尾的短句本身就是“清单引导语”，清单一并砍掉后它们也不该留。
_ECHO_LEAD_TAIL = ("获取", "下载", "访问", "参见", "详见", "见", "如下", "链接")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def strip_term_echo(text: str, terms=()) -> str:
    """砍掉模型在句末回吐的术语清单。

    LLM 有时把提示词里的「必须保持英文原样」清单当成正文，接在句子后面：
    `…GPU 内存使用量。代码可在此获取：MMDP、LLM、GPU、LMMs、DivPrune`。
    判定必须保守——只有同时满足下面几条才动手，避免误删正常的并列短句：

    * 清单前有冒号/逗号分隔，且清单至少 4 项；
    * 每一项都是短的拉丁词串（含中文的并列、带句号的句子一律不动）；
    * 已知术语时，至少 6 成项命中术语表；无术语表时要求至少 6 项。
    """
    if not text:
        return text
    candidate = text.rstrip()
    cut = -1
    for mark in _ECHO_MARKS:
        cut = max(cut, candidate.rfind(mark))
    if cut <= 0:
        return text
    items = [item.strip() for item in _ECHO_SPLIT_RE.split(candidate[cut + 1 :]) if item.strip()]
    if len(items) < 4 or not all(_ECHO_ITEM_RE.match(item) for item in items):
        return text
    known = {_squash(term) for term in terms if term}
    hits = sum(1 for item in items if _squash(item) in known)
    if known:
        if hits < max(3, int(len(items) * 0.6)):
            return text
    elif len(items) < 6:
        return text
    head = candidate[:cut].rstrip(" ：:，,、")
    if not head:
        return text
    # “代码可在此获取：” 这类引导语在清单被砍掉后就是半句话，整句丢弃。
    clause_start = max(head.rfind(mark) for mark in "。！？；")
    clause = head[clause_start + 1 :].strip()
    if clause and any(clause.endswith(tail) for tail in _ECHO_LEAD_TAIL):
        head = head[: clause_start + 1].rstrip() if clause_start >= 0 else ""
        if not head:
            return text
    if head[-1] not in "。！？…”’）)!?":
        head += "。"
    return head


# 句末只剩「代码可在此获取：」这类引导语（清单已被砍掉或模型根本没写出来）。
_TAIL_LEAD_RE = re.compile(
    r"(?:^|[。！？；])[^。！？；]{0,24}?(?:获取|下载|访问|参见|详见|如下|链接)"
    r"[^。！？；]{0,6}[：:，,、]?\s*[⋄†‡§¶*]?\s*[。．.]?$"
)


def drop_lead_in_tail(text: str) -> str:
    """丢掉句末悬空的引导语。

    模型把“代码可在此获取：<链接>”里的链接吞掉后，句子就剩半截
    「代码可在此获取：⋄。」。整句都是引导语、没有实际信息，直接删。
    """
    if not text:
        return text
    candidate = text.rstrip()
    match = _TAIL_LEAD_RE.search(candidate)
    if not match:
        return text
    keep = candidate[: match.start()].rstrip()
    if match.start() > 0 and candidate[match.start()] in "。！？；":
        keep = candidate[: match.start() + 1].rstrip()
    if not keep or not CJK_RE.search(keep[-12:]):
        return text
    return keep


def collapse_tail_repeat(text: str) -> str:
    """删掉模型在句末重复抄一遍的公式/句子。

    典型输出（Ahmed 训练损失段）：
    `…来最小化总体损失函数： Ltotal = Lmse + α · LCE + β · LKD`，
    前半句已经写过同一个式子，末尾又抄一遍。判定条件：重复片段
    ≥24 字符、出现在文本后半段并且一直延续到结尾，才把尾巴切掉。
    """
    if not text or len(text) < 60:
        return text
    candidate = text.rstrip()
    n = len(candidate)
    limit = min(n // 2, 160)
    for size in range(limit, 23, -1):
        for head in range(0, max(0, min(n // 2, 90))):
            chunk = candidate[head : head + size]
            if not chunk.strip():
                continue
            tail = candidate.rfind(chunk)
            if tail <= head or tail + size < n - 2:
                continue
            keep = candidate[:tail].rstrip(" \t\n，,、；;：:-")
            if len(keep) < 24:
                return text
            if not CJK_RE.search(keep):
                return text
            if keep[-1] not in "。！？.!?":
                keep += "。"
            return keep
    return text


_INSTRUCTION_ECHO_RE = re.compile(
    r"(?:请|麻烦|需要)(?:你)?(?:直接)?(?:给出|提供|输出|翻译|写出|列出|补充)"
    r"[^。！？\n]{0,24}(?:公式|译文|结果|内容|关系式|答案|版本|原文)"
    r"|<tool_call>|【术语表】"
)
_ECHO_PREFIX_RE = re.compile(
    r"^(?:以下是|下面是|这是)(?:该|这段|本段|原文的|其)?(?:译文|翻译)[:：]?\s*"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；])")


def strip_instruction_echo(text: str) -> str:
    """删掉模型写进译文里的指令句。

    典型泄漏（Zhang 第 8 页）：`其中 请直接给出公式或关系式。` ——
    模型把"该输出什么"的指令当正文写了出来。整句删掉，其余句子保持原样。
    """
    if not text or not (_INSTRUCTION_ECHO_RE.search(text) or _ECHO_PREFIX_RE.match(text)):
        return text
    text = _ECHO_PREFIX_RE.sub("", text)
    if not text:
        return ""
    kept = [
        sentence
        for sentence in _SENTENCE_SPLIT_RE.split(text)
        if sentence.strip() and not _INSTRUCTION_ECHO_RE.search(sentence)
    ]
    if not kept:
        return ""
    cleaned = "".join(kept).strip()
    if cleaned and cleaned[-1] not in "。！？.!?":
        cleaned += "。"
    return cleaned


def postprocess(text: str) -> str:
    """完整后处理流水线。"""
    if not text:
        return text
    text = strip_instruction_echo(text)
    text = strip_term_echo(text)
    text = drop_lead_in_tail(text)
    text = collapse_tail_repeat(text)
    text = fix_punctuation(text)
    text = fix_repetition(text)
    text = clean_spaces(text)
    return text
