"""结构解析：把 PDF 拆成有序的文档元素树（阶段 ①）。

输出按阅读顺序排列的元素树：页面与栏信息、章节标题层级、段落、图+图注、
表+表题、公式块、脚注、参考文献、页眉页脚。每个元素带来源页码、原文与
confidence，标出不确定项（无编号图注、仅凭字号判定的伪标题等）。

阅读顺序对双栏成立：先左栏到底、再右栏；通栏元素（标题、跨栏图表、
页脚注释）把页面切成横带，跨栏续接不会越过它们。
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from dataclasses import dataclass, field, replace

import fitz

from ..textfilter import CAPTION_RE, NO_LINE_START, classify
from .mathtext import sanitize_glyphs

HEADER_BAND = 62.0  # 距页顶多少 pt 内视为页眉
FOOTER_BAND = 58.0  # 距页底多少 pt 内视为页脚
TITLE_MIN_RATIO = 1.35  # 首页标题相对正文字号的下限
HEADING_MIN_RATIO = 1.12  # 标题相对正文字号的下限
COLUMN_GAP_MIN = 14.0  # 栏间最小空白宽度
WIDE_RATIO = 0.55  # 超过版心该宽度的块视为通栏
# 栏检测只认"确实待在单栏里"的块：算法标题、跨栏引言行常达页宽 50%~55%，
# 会把栏缝盖掉，导致整页退化成单栏、双栏续段被按 y 坐标交错排序。
COLUMN_BLOCK_RATIO = 0.45

REFERENCE_TITLES = {"references", "bibliography", "reference", "references and notes"}
ABSTRACT_TITLES = {"abstract", "summary"}
SECTION_WORDS = {
    "introduction",
    "related works",
    "related work",
    "method",
    "methods",
    "proposed method",
    "experiments",
    "experiment",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "acknowledgements",
    "acknowledgment",
    "appendix",
}

_LABEL_RE = re.compile(
    r"^(figure|fig\.?|table|algorithm)\s*([0-9]+[A-Za-z]?)\s*[.:：]?\s*", re.IGNORECASE
)
_LABELS = {"figure": "图", "fig": "图", "fig.": "图", "table": "表", "algorithm": "算法"}
_NUMBERED_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+\S")
_REF_ITEM_RE = re.compile(r"^\[\d{1,3}\]")
# 条目分隔：空格 + "[n]" + 空白 + 大写字母（作者名），避免把标题里的 [n] 当编号。
_REF_SPLIT_RE = re.compile(r"(?<=\s)(?=\[\d{1,3}\]\s+[A-Z])")


@dataclass
class Element:
    """文档中的一个有序元素。"""

    kind: str  # title/subtitle/heading/body/caption/figure/table/equation/reference/footnote/header/footer
    text: str = ""
    page: int = 0
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    size: float = 10.5
    bold: bool = False
    level: int = 0
    label: str = ""  # 图 1 / 表 2
    rows: list[list[str]] = field(default_factory=list)
    image: bytes = b""
    image_size: tuple[int, int] = (0, 0)
    skip: bool = False
    note: str = ""
    translation: str = ""
    translatable: bool = True
    # 阶段 ① 元数据
    id: str = ""
    order: int = 0
    column: int = 0
    confidence: float = 1.0
    confidence_notes: list[str] = field(default_factory=list)
    children: list["Element"] = field(default_factory=list)

    @property
    def rect(self) -> fitz.Rect:
        return fitz.Rect(self.bbox)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "kind": self.kind,
            "order": self.order,
            "page": self.page + 1,
            "column": self.column,
            "bbox": [round(value, 1) for value in self.bbox],
            "size": round(self.size, 1),
            "bold": self.bold,
            "level": self.level,
            "label": self.label,
            "text": self.text,
            "translate": bool(self.translatable and not self.skip),
            "skip": self.skip,
            "confidence": round(self.confidence, 2),
            "confidence_notes": self.confidence_notes,
        }
        if self.rows:
            data["rows"] = self.rows
        if self.kind in ("figure", "equation", "table") and self.image_size:
            data["image_size"] = list(self.image_size)
        if self.children:
            data["children"] = [child.to_dict() for child in self.children]
        return data


@dataclass
class ColumnLayout:
    count: int = 1
    gutters: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Document:
    source: str
    page_size: tuple[float, float]
    elements: list[Element]
    body_size: float = 10.5
    dropped_headers: int = 0
    page_columns: list[int] = field(default_factory=list)
    page_gutters: list[list[tuple[float, float]]] = field(default_factory=list)
    degraded: str = ""

    @property
    def columns(self) -> int:
        """全篇主导栏数：取各页最多出现的栏数。"""
        if not self.page_columns:
            return 1
        return max(set(self.page_columns), key=self.page_columns.count)

    @property
    def title(self) -> str:
        return next((e.text for e in self.elements if e.kind == "title"), "")

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for element in self.elements:
            for node in element.walk():
                result[node.kind] = result.get(node.kind, 0) + 1
        return result

    def low_confidence(self, threshold: float = 0.7) -> list[Element]:
        return [
            node
            for element in self.elements
            for node in element.walk()
            if node.confidence < threshold and node.kind not in ("header", "footer")
        ]

    def to_dict(self) -> dict:
        return {
            "schema": 1,
            "source": self.source,
            "page_count": len(self.page_columns),
            "page_size": [round(value, 1) for value in self.page_size],
            "document": {
                "columns": self.columns,
                "body_size": self.body_size,
                "title": self.title,
                "reading_order": (
                    "column-major：先左栏到底，再右栏；通栏块切分横带"
                    if self.columns > 1
                    else "single-column：自上而下"
                ),
                "degraded": self.degraded,
                "dropped_headers": self.dropped_headers,
            },
            "pages": [
                {
                    "page": index + 1,
                    "columns": columns,
                    "gutters": [[round(a, 1), round(b, 1)] for a, b in gutters],
                }
                for index, (columns, gutters) in enumerate(
                    zip(self.page_columns, self.page_gutters)
                )
            ],
            "counts": self.counts(),
            "elements": [element.to_dict() for element in self.elements],
        }


@dataclass
class _Block:
    text: str
    page: int
    bbox: fitz.Rect
    size: float
    bold_ratio: float
    kind: str = "body"
    level: int = 0
    label: str = ""
    column: int = 0
    confidence: float = 1.0
    notes: list[str] = field(default_factory=list)
    skip: bool = False
    translatable: bool = True
    # 数学/算法碎片无法还原成可排版文本时，按原版区域截图保留（坐标为页面坐标）。
    raster: fitz.Rect | None = None
    image: bytes = b""
    image_size: tuple[int, int] = (0, 0)

    @property
    def rect(self) -> fitz.Rect:
        return self.bbox

    @property
    def bold(self) -> bool:
        return self.bold_ratio >= 0.6


def _join_lines(lines: list[str]) -> str:
    out = ""
    for raw in lines:
        piece = " ".join((raw or "").split())
        if not piece:
            continue
        if not out:
            out = piece
            continue
        if out.endswith("-") and piece[:1].islower():
            out = out[:-1] + piece
        else:
            out = out + " " + piece
    return " ".join(out.split())


_LIST_MARKERS = set("•◦▪‣·●○■□◆◇")
_DANGLING_WORDS = {
    "and", "or", "of", "the", "for", "to", "with", "in", "on", "at", "by",
    "as", "that", "which", "is", "are", "was", "were", "we", "this", "these",
    "from", "not", "a", "an", "be", "has", "have",
}
_NUMBERED_LINE_RE = re.compile(r"^\s*\d{1,2}\s*[:.]\s+\S")


def _is_list_item(text: str) -> bool:
    return bool(text) and (text[0] in _LIST_MARKERS or bool(_NUMBERED_LINE_RE.match(text)))


def _starts_continuation(previous: str, current: str) -> bool:
    """当前块是否承接上一块未写完的句子。

    除了"小写开头"，还要覆盖双栏断句后接专有名词的情况：
    "... For “Standard” and" / "“Solution 1” models, ..."，上一块停在
    连接词/冠词上，下一块虽以大写字母开头，仍是同一句。
    """
    if current.lstrip("“\"'‘「『([（")[:1].islower():
        return True
    words = re.findall(r"[A-Za-z]+", previous)
    return bool(words) and words[-1].lower() in _DANGLING_WORDS


def _continues(previous: str, current: str) -> bool:
    """上一行句子没写完、下一行以小写字母开头 → 同一段（标题/正文续行）。"""
    if not current or not current[0].islower():
        return False
    return previous.rstrip()[-1:] not in ".!?:。！？：；"


def _block_from_dict(block: dict, page_index: int) -> list[_Block]:
    """把一个 PyMuPDF block 按行切成段落。

    CVPR 这类论文常把整栏正文合成一个 block，若整块当一段，元素树就失去
    段落粒度。这里用行距、首行缩进、字号/字重变化识别段落边界。
    """
    lines: list[tuple[str, fitz.Rect, float, bool]] = []
    for line in block.get("lines", []):
        pieces: list[str] = []
        weights: dict[float, int] = {}
        bold_chars = 0
        total_chars = 0
        for span in line.get("spans", []):
            text = span.get("text") or ""
            if not text.strip():
                continue
            pieces.append(text)
            size = round(float(span.get("size") or 0.0), 1)
            weights[size] = weights.get(size, 0) + len(text)
            total_chars += len(text)
            if "bold" in str(span.get("font") or "").lower():
                bold_chars += len(text)
        text = " ".join(("".join(pieces)).split())
        text = sanitize_glyphs(text)
        if not text or not weights:
            continue
        size = max(weights.items(), key=lambda item: item[1])[0] or 10.0
        lines.append(
            (
                text,
                fitz.Rect(line["bbox"]),
                size,
                bold_chars / max(1, total_chars) >= 0.6,
            )
        )
    if not lines:
        return []

    block_x0 = min(item[1].x0 for item in lines)
    row_gaps = [
        lines[index + 1][1].y0 - lines[index][1].y0 for index in range(len(lines) - 1)
    ]
    typical = sorted(row_gaps)[len(row_gaps) // 2] if row_gaps else 0.0
    groups: list[list[tuple[str, fitz.Rect, float, bool]]] = [[lines[0]]]
    for previous, current in zip(lines, lines[1:]):
        if _is_list_item(current[0]):
            # 项目符号行各自成段，避免整张列表被并成一个正文块。
            groups.append([current])
            continue
        prev_rect, cur_rect = previous[1], current[1]
        size = max(previous[2], current[2])
        gap = cur_rect.y0 - prev_rect.y0
        indent = cur_rect.x0 - block_x0
        # 段内行距随文档变化（CVPR 单倍、报告 1.5 倍），阈值取本块行距中位数。
        limit = typical * 1.35 if typical > 0 else size * 1.45
        new_para = (
            gap > limit
            or gap < -2.0
            or abs(current[2] - previous[2]) > 1.0
            or current[3] != previous[3]
        )
        if not new_para and indent > size * 0.9 and cur_rect.x0 - prev_rect.x0 > size * 0.9:
            # 首行缩进一般 1~2.5 字符；更大的横向偏移多来自居中标题换行，不算新段。
            # 但上一行句子没写完、下一行以小写字母开头时，这是续行而不是新段
            # （双栏标题 "3.1. ... in" / "learned image compression" 会被误拆）。
            if indent <= size * 2.5 and not _continues(previous[0], current[0]):
                new_para = True
        if new_para:
            groups.append([current])
        else:
            groups[-1].append(current)

    result: list[_Block] = []
    for group in groups:
        text = _join_lines([item[0] for item in group])
        if not text:
            continue
        rect = fitz.Rect(group[0][1])
        for item in group[1:]:
            rect |= item[1]
        weights: dict[float, int] = {}
        bold_chars = 0
        total_chars = 0
        for item in group:
            weights[item[2]] = weights.get(item[2], 0) + len(item[0])
            total_chars += len(item[0])
            if item[3]:
                bold_chars += len(item[0])
        size = max(weights.items(), key=lambda item: item[1])[0] or 10.0
        result.append(
            _Block(
                text=text,
                page=page_index,
                bbox=rect,
                size=size,
                bold_ratio=bold_chars / max(1, total_chars),
            )
        )
    return result


# ---------- 栏数识别 ----------


def _textual(block: _Block) -> bool:
    """像自然语言正文的块；公式/算法碎片会横跨栏缝，不能用于判栏。"""
    text = block.text
    if not text or _has_noise(text):
        return False
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return letters + cjk >= 20 and _math_density(text) < 0.2


def detect_columns(blocks: list[_Block], page_rect: fitz.Rect) -> ColumnLayout:
    """用"文字块横向覆盖"找栏间空白，支持单栏/双栏/三栏。

    只统计窄块（宽度 <= 页宽 45%）。通栏图表、跨栏标题不参与，避免把
    整页宽的元素误当成覆盖；页面上半/下半各有一批窄块时才承认分栏。
    """
    narrow = [b for b in blocks if b.rect.width <= page_rect.width * COLUMN_BLOCK_RATIO]
    if len(narrow) < 6:
        return ColumnLayout(1, [])

    # 只看正文字号的块：标题、图表内小字可能横跨栏间空白，会掩盖真正的栏缝。
    body_size = _document_body_size(narrow)
    core = [
        b
        for b in narrow
        if body_size * 0.92 <= b.size <= body_size * 1.12
    ]
    prose = [b for b in core if _textual(b)]
    # 图占大半页时，真正的正文块可能只剩 4~5 个；门槛太高会让整页退化成
    # 单栏，图注/子图标签横跨栏缝也会被当成正文覆盖。
    if len(prose) >= 4:
        core = prose
    if len(core) >= 4:
        narrow = core

    x_start = int(min(b.rect.x0 for b in narrow))
    x_end = int(max(b.rect.x1 for b in narrow))
    if x_end - x_start < 120:
        return ColumnLayout(1, [])

    counts = [0] * (x_end - x_start + 1)
    for block in narrow:
        left = max(x_start, int(block.rect.x0) - x_start)
        right = min(x_end, int(block.rect.x1) - x_start)
        for index in range(left, right + 1):
            counts[index] += 1

    # 栏缝允许被少量跨栏行（算法伪代码、宽公式）压过；只有多数块都覆盖的
    # 横向位置才算正文区。恰好达到容忍数量的位置仍算"被覆盖"，否则栏缝边界
    # 会被错误外扩到左栏右缘（左栏块 x1=294.8 被排除后左右分栏双双失效）。
    limit = max(1, int(len(narrow) * 0.2))
    covered = [count >= limit for count in counts]

    gaps: list[tuple[float, float]] = []
    index = 0
    while index < len(covered):
        if covered[index]:
            index += 1
            continue
        start = index
        while index < len(covered) and not covered[index]:
            index += 1
        if index - start >= COLUMN_GAP_MIN:
            gaps.append((x_start + start, x_start + index))

    if not gaps:
        return ColumnLayout(1, [])

    content_height = page_rect.height
    gutters: list[tuple[float, float]] = []
    for gap in gaps:
        # 少数跨栏行（算法伪代码、宽公式）会压过栏缝；只有占据页面较大纵向
        # 范围时才算真的没有栏缝，否则按双栏处理。
        blockers = [
            b
            for b in narrow
            if b.rect.x0 < gap[0] + 1.0 and b.rect.x1 > gap[1] - 1.0
        ]
        if blockers:
            ranges = sorted((b.rect.y0, b.rect.y1) for b in blockers)
            covered_height = 0.0
            current = list(ranges[0])
            for y0, y1 in ranges[1:]:
                if y0 <= current[1] + 2.0:
                    current[1] = max(current[1], y1)
                else:
                    covered_height += current[1] - current[0]
                    current = [y0, y1]
            covered_height += current[1] - current[0]
            if covered_height > content_height * 0.35:
                continue
        left = [b for b in narrow if b.rect.x1 <= gap[0] + 1.0]
        right = [b for b in narrow if b.rect.x0 >= gap[1] - 1.0]
        if len(left) < 2 or len(right) < 2:
            continue
        span_left = max(b.rect.y1 for b in left) - min(b.rect.y0 for b in left)
        span_right = max(b.rect.y1 for b in right) - min(b.rect.y0 for b in right)
        # 双栏区可能只占页面下半（上半是通栏图），按 25% 高度判定仍有栏。
        if span_left < content_height * 0.25 or span_right < content_height * 0.25:
            continue
        gutters.append(gap)

    if not gutters:
        return ColumnLayout(1, [])

    # 只保留能把页面切成等宽栏的空白：间隔太近的合并成一条。
    merged: list[tuple[float, float]] = []
    for gap in gutters:
        if merged and gap[0] - merged[-1][1] < 24.0:
            merged[-1] = (merged[-1][0], gap[1])
        else:
            merged.append(gap)
    count = min(4, len(merged) + 1)
    return ColumnLayout(count, merged[: count - 1])


def _column_of(block: _Block, gutters: list[tuple[float, float]]) -> int:
    center = (block.rect.x0 + block.rect.x1) / 2
    column = 0
    for _start, end in gutters:
        if center > end:
            column += 1
    return column


def _order_blocks(
    blocks: list[_Block], page_rect: fitz.Rect, layout: ColumnLayout
) -> list[_Block]:
    """按栏序排列：通栏块切横带，带内先左栏到底、再右栏。"""
    if not blocks:
        return []
    for block in blocks:
        block.column = _column_of(block, layout.gutters) if layout.count > 1 else 0
    if layout.count == 1:
        return sorted(blocks, key=lambda b: (round(b.rect.y0, 1), b.rect.x0))

    wide = [b for b in blocks if b.rect.width > page_rect.width * WIDE_RATIO]
    wide_ids = {id(b) for b in wide}
    wide_sorted = sorted(wide, key=lambda b: b.rect.y0)

    def band_of(block: _Block) -> int:
        if id(block) in wide_ids:
            return -1
        band = 0
        for item in wide_sorted:
            if block.rect.y0 >= item.rect.y1:
                band += 1
            elif block.rect.y1 <= item.rect.y0:
                break
            else:
                return band
        return band

    def key(block: _Block) -> tuple[int, int, float, float]:
        band = band_of(block)
        column = -1 if id(block) in wide_ids else block.column
        return (band, column, round(block.rect.y0, 1), block.rect.x0)

    return sorted(blocks, key=key)


def _page_tables(page: fitz.Page) -> list[tuple[fitz.Rect, list[list[str]], float]]:
    tables: list[tuple[fitz.Rect, list[list[str]], float]] = []
    try:
        # find_tables 会在 stdout 打一条推荐 pymupdf_layout 的提示，这里静音。
        with redirect_stdout(io.StringIO()):
            found = page.find_tables()
        for table in found.tables:
            rows = [
                [sanitize_glyphs(" ".join((cell or "").split())) for cell in row]
                for row in table.extract()
            ]
            rows = [row for row in rows if any(row)]
            if len(rows) >= 2 and len(rows[0]) >= 2:
                confidence = 0.9 if len(rows) >= 3 else 0.75
                tables.append((fitz.Rect(table.bbox), rows, confidence))
    except Exception:
        return []
    return tables


def split_caption(text: str) -> tuple[str, str]:
    """抽出 "Figure 1." / "Table 2" 之类标签，返回（中文标签, 去掉标签的正文）。"""
    match = _LABEL_RE.match(text)
    if not match:
        return "", text
    word = match.group(1).lower().rstrip(".")
    label = f"{_LABELS.get(word, '图')} {match.group(2)}"
    return label, text[match.end() :].strip()


def _norm_heading(text: str) -> str:
    return " ".join((text or "").split()).lower().strip(" .:")


def _document_body_size(blocks: list[_Block]) -> float:
    weights: dict[float, int] = {}
    for block in blocks:
        if block.size <= 0:
            continue
        weights[block.size] = weights.get(block.size, 0) + len(block.text)
    if not weights:
        return 10.5
    return max(weights.items(), key=lambda item: item[1])[0]


def _numbering_level(text: str) -> int:
    """从 "3.1.2 标题" 推断层级；无编号返回 0。"""
    match = _NUMBERED_RE.match(text.strip())
    if not match:
        return 0
    return min(3, match.group(1).count(".") + 1)


def _looks_centered(block: _Block, page_rect: fitz.Rect) -> bool:
    center = (block.rect.x0 + block.rect.x1) / 2
    page_center = (page_rect.x0 + page_rect.x1) / 2
    return abs(center - page_center) < page_rect.width * 0.06


def _near_caption(rect: fitz.Rect, caption: fitz.Rect) -> bool:
    """横向有足够重叠即视为同一列的图/表（题注通常在图形的正上方或正下方）。"""
    overlap = min(rect.x1, caption.x1) - max(rect.x0, caption.x0)
    return overlap > 0.25 * min(rect.width, caption.width)


# ---------- 公式/算法区还原 ----------

_MATH_CHARS = set(
    "=<>^_±≤≥√∑∫∇∈∉⊂⊃∪∩→∞∂∏≡≈≠×÷·∘∗⊕⊗⊤⊥∥|{}[]%∼ˆ′″−"
)
_MATH_DENSITY = 0.10
_PUA_RANGE = ("\ue000", "\uf8ff")
# 算法伪代码的行号块（"1:"、"12."）常被 PDF 单独拆出，需并入算法截图。
_LINE_NO_RE = re.compile(r"^\d{1,2}\s*[:.]?$")


def _has_noise(text: str) -> bool:
    """含私用区码点或控制字符：源 PDF 数学字体编码噪声，文本无法还原。"""
    for ch in text:
        if _PUA_RANGE[0] <= ch <= _PUA_RANGE[1]:
            return True
        if ord(ch) < 0x20 and ch not in "\n\t":
            return True
    return False


def _math_density(text: str) -> float:
    if not text:
        return 0.0
    hits = sum(
        1
        for ch in text
        if ch in _MATH_CHARS
        or "\u0370" <= ch <= "\u03ff"
        or ch.isdigit()
        or _PUA_RANGE[0] <= ch <= _PUA_RANGE[1]
    )
    return hits / max(1, len(text))


def _mathy(block: _Block) -> bool:
    return _has_noise(block.text) or _math_density(block.text) >= _MATH_DENSITY


def _fragment(block: _Block) -> bool:
    """单行高度的小块：公式被 PDF 拆开后的典型形态。"""
    return block.rect.height <= block.size * 1.75


def _prose_block(block: _Block) -> bool:
    """英文长句正文：终止公式区，避免把可翻译的段落吞进截图。"""
    if _fragment(block) or _has_noise(block.text):
        return False
    letters = sum(1 for ch in block.text if ch.isascii() and ch.isalpha())
    return letters >= 30 and _math_density(block.text) < 0.10


def _zone_seed(block: _Block) -> bool:
    """公式区起点：已判定为公式的块，或含数学符号/编码噪声的短块。"""
    if block.kind == "equation":
        return True
    if not _mathy(block):
        return False
    return _fragment(block) or len(block.text) <= 48


def _raster_region(page: fitz.Page, rect: fitz.Rect) -> tuple[bytes, tuple[int, int]]:
    zoom = 200 / 72
    pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png"), (pix.width, pix.height)


def _merge_math_zones(blocks: list[_Block], page: fitz.Page) -> list[_Block]:
    """把无法还原的公式/算法碎片合并为一个截图块。

    LaTeX 论文的显示公式、算法伪代码会被 PDF 拆成几十个同基线的小块，
    逐块排版必然爆行、丢字形。这里按“同栏 + 纵向相邻”聚成区域，整体截取
    原版版面交给排版器作为一张居中图片；与区域相交的零散块一并吞掉，
    避免同一内容既画进截图又单独排版（重叠、重影）。
    """
    total = len(blocks)
    group = [-1] * total
    runs: list[dict] = []

    for index, block in enumerate(blocks):
        if group[index] != -1 or not _zone_seed(block):
            continue
        members = [index]
        union = fitz.Rect(block.rect)
        cursor = index + 1
        while cursor < total:
            candidate = blocks[cursor]
            size = max(candidate.size, block.size)
            if not (
                candidate.column == block.column
                and candidate.kind in {"body", "equation", "figure_text", "code"}
                and not _prose_block(candidate)
                and (_fragment(candidate) or _mathy(candidate))
                and candidate.rect.y0 - union.y1 <= size * 0.9
                and candidate.rect.y1 >= union.y0 - size * 0.3
            ):
                break
            members.append(cursor)
            union |= candidate.rect
            cursor += 1
        # 向前吞并：公式左端（如 “L_{R−D} = E_{x∼p}”）常单独成块且排在种子之前。
        back = index - 1
        absorbed = 0
        while back >= 0 and absorbed < 3 and group[back] == -1:
            previous = blocks[back]
            size = max(previous.size, block.size)
            if (
                previous.column == block.column
                and previous.kind in {"body", "equation", "figure_text"}
                and _mathy(previous)
                and len(previous.text) <= 48
                and previous.rect.y1 >= union.y0 - size * 0.9
                and previous.rect.y0 <= union.y1 + size * 0.9
            ):
                members.insert(0, back)
                union |= previous.rect
                back -= 1
                absorbed += 1
                continue
            break
        if len(members) < 2:
            continue
        for member in members:
            group[member] = len(runs)
        runs.append({"members": members, "rect": union, "seed": index})

    # 几何吸收：任何仍未被分组的块，只要大部分面积落在某个截图区域内，
    # 或被判定为算法行号且紧贴区域，就视为已包含在截图里，不再单独排版。
    for index, block in enumerate(blocks):
        if group[index] != -1 or block.kind not in {"body", "equation", "figure_text", "code"}:
            continue
        area = block.rect.get_area()
        if area <= 0:
            continue
        line_no = bool(_LINE_NO_RE.match(block.text.strip()))
        for run_index, run in enumerate(runs):
            rect = run["rect"]
            overlap = (rect & block.rect).get_area()
            near = (
                line_no
                and block.column == blocks[run["seed"]].column
                and block.rect.y1 >= rect.y0 - 1.6 * block.size
                and block.rect.y0 <= rect.y1 + 1.6 * block.size
            )
            if overlap > area * 0.5 or (
                overlap > 4.0 and (line_no or len(block.text) <= 24)
            ) or near:
                run["members"].append(index)
                group[index] = run_index
                run["rect"] = rect | block.rect
                break

    # 相交区域合并：算法伪代码常被拆成上下两段碎片各自成区，两张截图会
    # 互相覆盖，同一行被各截一半，出现重复/截断的窄条。区域有交集即合并。
    changed = True
    while changed:
        changed = False
        for i in range(len(runs)):
            for j in range(i + 1, len(runs)):
                if (runs[i]["rect"] & runs[j]["rect"]).get_area() <= 0:
                    continue
                if runs[j]["seed"] < runs[i]["seed"]:
                    runs[i], runs[j] = runs[j], runs[i]
                runs[i]["members"] = sorted(
                    set(runs[i]["members"]) | set(runs[j]["members"])
                )
                runs[i]["rect"] = runs[i]["rect"] | runs[j]["rect"]
                del runs[j]
                runs[i]["seed"] = runs[i]["members"][0]
                changed = True
                break
            if changed:
                break
    for run_index, run in enumerate(runs):
        for member in run["members"]:
            group[member] = run_index

    raster: dict[int, _Block] = {}
    for run_index, run in enumerate(runs):
        rect = fitz.Rect(run["rect"])
        # 上下略作扩张：上标/下标的独立字形常落在主块之外（Σ 的指数 2）。
        # 右侧多留一点：公式编号常被 PDF 归到下一段，截窄了会把 "(1)" 切掉。
        seed = blocks[run["seed"]]
        pad = seed.size * 0.9
        rect = fitz.Rect(
            rect.x0 - pad * 0.4,
            rect.y0 - pad,
            rect.x1 + pad * 1.4,
            # 公式编号 (1) 的 bbox 常不带降部，下沿只留 0.4 字号会把编号拦腰截断。
            rect.y1 + pad * 1.1,
        )
        # 扩张不能压到同栏相邻的非成员块（算法题注上方的 Require 段最典型）。
        members = set(run["members"])
        above = [
            item.rect.y1
            for index, item in enumerate(blocks)
            if index not in members
            and item.column == seed.column
            and item.rect.y1 <= run["rect"].y0 + 0.5
            and _near_caption(item.rect, run["rect"])
        ]
        below = [
            item.rect.y0
            for index, item in enumerate(blocks)
            if index not in members
            and item.column == seed.column
            and item.rect.y0 >= run["rect"].y1 - 0.5
            and _near_caption(item.rect, run["rect"])
        ]
        if above:
            rect.y0 = max(rect.y0, max(above) + 1.0)
        if below:
            rect.y1 = min(rect.y1, min(below) - 0.4)
        rect &= page.rect
        if rect.width < 30 or rect.height < 8 or rect.height > page.rect.height * 0.8:
            for member in run["members"]:
                group[member] = -1
            continue
        data, size = _raster_region(page, rect)
        raster[run_index] = _Block(
            text="",
            page=seed.page,
            bbox=rect,
            size=seed.size,
            bold_ratio=0.0,
            kind="equation",
            column=seed.column,
            confidence=0.8,
            notes=[f"公式/算法区整体保留原版（{len(run['members'])} 个碎片）"],
            skip=True,
            translatable=False,
            raster=rect,
            image=data,
            image_size=size,
        )

    result: list[_Block] = []
    emitted: set[int] = set()
    for index, block in enumerate(blocks):
        run_index = group[index]
        if run_index == -1:
            result.append(block)
            continue
        if run_index in emitted or run_index not in raster:
            continue
        emitted.add(run_index)
        result.append(raster[run_index])
    return result


def _vector_figures(
    page: fitz.Page,
    blocks: list[_Block],
    tables: list[tuple[fitz.Rect, list[list[str]], float]],
) -> list[tuple[fitz.Rect, bytes, tuple[int, int]]]:
    """把矢量图区域栅格化为图片。

    CVPR/LaTeX 论文的图多由 TikZ/matplotlib 生成，没有位图 xref；它们会被
    `find_tables()` 误判成表格，图注还在、图却整块丢失。这里以“图 N”题注为
    锚点，把相邻的表格候选与矢量绘图合并后截图，作为图元素交给排版。
    """
    page_rect = page.rect
    drawings = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing.get("rect", (0, 0, 0, 0)))
        if rect.is_empty or rect.width < 12 or rect.height < 12:
            continue
        if rect.width > page_rect.width * 0.96 and rect.height > page_rect.height * 0.96:
            continue
        drawings.append(rect)

    found: list[tuple[fitz.Rect, bytes, tuple[int, int]]] = []
    body = _document_body_size(blocks)
    captions = [
        block
        for block in blocks
        if split_caption(block.text)[0].startswith("图") and block.size < body * 0.98
    ]
    floor = page_rect.y0
    for block in sorted(captions, key=lambda item: item.rect.y0):
        caption = block.rect
        for direction in (1, -1):
            low = max(caption.y0 - 300, floor) if direction == 1 else caption.y1
            high = caption.y0 - 2 if direction == 1 else caption.y1 + 300
            if high - low < 30:
                continue
            band = fitz.Rect(page_rect.x0, low, page_rect.x1, high)
            picks = [
                rect
                for rect, _rows, _score in tables
                if (rect & band).get_area() > rect.get_area() * 0.8 and _near_caption(rect, caption)
            ]
            picks += [
                rect
                for rect in drawings
                if (rect & band).get_area() > rect.get_area() * 0.8 and _near_caption(rect, caption)
            ]
            if not picks:
                continue
            union = picks[0]
            for rect in picks[1:]:
                union |= rect
            if union.width < 120 or union.height < 50:
                continue
            if union.get_area() > page_rect.get_area() * 0.55:
                continue
            clip = (union + (-3, -3, 3, 3)) & page_rect
            try:
                pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(2, 2), alpha=False)
                data = pix.tobytes("png")
            except Exception:
                continue
            found.append((clip, data, (pix.width, pix.height)))
            floor = max(floor, caption.y1)
            break
    return found


def build_document(doc: fitz.Document) -> Document:
    """解析整篇 PDF，返回有序元素树。"""
    raw_pages: list[list[_Block]] = []
    tables_by_page: list[list[tuple[fitz.Rect, list[list[str]], float]]] = []
    figures_by_page: list[list[tuple[fitz.Rect, bytes, tuple[int, int]]]] = []
    headers_by_page: list[list[_Block]] = []
    footers_by_page: list[list[_Block]] = []
    layouts: list[ColumnLayout] = []
    dropped_headers = 0

    for page_index, page in enumerate(doc):
        page_rect = page.rect
        page_blocks: list[_Block] = []
        headers: list[_Block] = []
        footers: list[_Block] = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for info in _block_from_dict(block, page_index):
                if info.rect.y1 <= page_rect.y0 + HEADER_BAND:
                    headers.append(info)
                    continue
                if info.rect.y0 >= page_rect.y1 - FOOTER_BAND:
                    footers.append(info)
                    continue
                page_blocks.append(info)
        dropped_headers += len(headers) + len(footers)

        tables = _page_tables(page)
        tables_by_page.append(tables)
        if tables:
            page_blocks = [
                b
                for b in page_blocks
                if not any((b.rect & rect).get_area() > b.rect.get_area() * 0.5 for rect, _r, _c in tables)
            ]

        figures: list[tuple[fitz.Rect, bytes, tuple[int, int]]] = []
        for info in page.get_image_info(xrefs=True):
            rect = fitz.Rect(info.get("bbox", (0, 0, 0, 0))) & page_rect
            if rect.is_empty or rect.width < 24 or rect.height < 24:
                continue
            xref = int(info.get("xref") or 0)
            if xref <= 0:
                continue
            try:
                image = doc.extract_image(xref)
            except Exception:
                continue
            data = image.get("image")
            if not data:
                continue
            figures.append((rect, data, (int(image.get("width") or 0), int(image.get("height") or 0))))
            page_blocks = [
                b
                for b in page_blocks
                if (b.rect & rect).get_area() <= b.rect.get_area() * 0.5
            ]
        figures_by_page.append(figures)
        headers_by_page.append(headers)
        footers_by_page.append(footers)

        # 矢量图：没有位图 xref 的图会被 find_tables 误判成表格，这里还原成图，
        # 并把覆盖区域内的“伪表格”和刻度文字一起移除。
        vector_figures = _vector_figures(page, page_blocks, tables)
        if vector_figures:
            figures.extend(vector_figures)
            rects = [rect for rect, _data, _size in vector_figures]
            tables = [
                item
                for item in tables
                if not any((item[0] & rect).get_area() > item[0].get_area() * 0.4 for rect in rects)
            ]
            tables_by_page[-1] = tables
            page_blocks = [
                block
                for block in page_blocks
                if not any((block.rect & rect).get_area() > block.rect.get_area() * 0.5 for rect in rects)
            ]

        layout = detect_columns(page_blocks, page_rect)
        layouts.append(layout)
        raw_pages.append(_order_blocks(page_blocks, page_rect, layout))

    all_blocks = [block for blocks in raw_pages for block in blocks]
    body_size = _document_body_size(all_blocks)
    page_width = doc[0].rect.width if doc.page_count else 595.0

    # 角色判定：先按字号/加粗/编号打标签，再处理图注、公式、参考文献与脚注。
    reference_mode = False
    for page_index, blocks in enumerate(raw_pages):
        tiny = sum(1 for block in blocks if block.size < body_size * 0.95)
        page_bottom = doc[page_index].rect.y1
        for block in blocks:
            text = block.text
            norm = _norm_heading(text)
            label, _rest = split_caption(text)
            numbered = _numbering_level(text)

            # 只有“小字号或单行”的 Figure/Table 块才是题注；正文里
            # “Fig. 2 further illustrates...” 这类引用段不能被当成题注。
            caption_like = (
                block.size < body_size * 0.98 or block.rect.height <= block.size * 2.2
            )
            if label and CAPTION_RE.match(text) and caption_like:
                block.kind = "caption"
                block.label = label
                block.confidence = 0.92 if label.startswith("表") else 0.9
                block.notes.append(f"题注标签匹配：{label}")
                continue
            if norm in REFERENCE_TITLES:
                block.kind = "heading"
                block.level = 1
                block.confidence = 0.95
                block.notes.append("命中 References/Bibliography 标题词")
                reference_mode = True
                continue
            if reference_mode:
                block.kind = "reference"
                block.skip = True
                block.translatable = False
                block.confidence = 0.9
                block.notes.append("References 之后的条目，保留英文原文")
                continue
            if norm in ABSTRACT_TITLES:
                block.kind = "heading"
                block.level = 1
                block.confidence = 0.9
                block.notes.append("命中 Abstract/Summary 标题词")
                continue
            if (
                block.size < body_size * 0.95
                and tiny >= 2
                and block.rect.y0 < page_bottom - 150
            ):
                block.kind = "figure_text"
                block.skip = True
                block.translatable = False
                block.confidence = 0.85
                block.notes.append("字号明显小于正文且同页小字块较多，判为图内标注")
                continue
            if len(text) <= 6 and not re.search(r"[A-Za-z]{3,}", text):
                block.kind = "equation"
                block.confidence = 0.6
                block.notes.append("短数学符号，按公式处理")
                continue
            if numbered and block.size >= body_size * 1.02 and len(text) <= 120:
                block.kind = "heading"
                block.level = min(3, numbered)
                block.confidence = 0.93
                block.notes.append(f"命中章节编号 {text.split()[0]}")
                continue
            if block.bold and block.size >= body_size * TITLE_MIN_RATIO and len(text) <= 160:
                block.kind = "heading"
                block.level = 1
                block.confidence = 0.72
                block.notes.append("仅凭字号与加粗判定为一级标题")
                continue
            if block.bold and block.size >= body_size * HEADING_MIN_RATIO and len(text) <= 90:
                block.kind = "heading"
                block.level = 2
                block.confidence = 0.65
                block.notes.append("仅凭字号与加粗判定为二级标题")
                continue
            if norm.rstrip("s") in SECTION_WORDS and len(text) <= 60:
                block.kind = "heading"
                block.level = 1
                block.confidence = 0.8
                block.notes.append("命中常见章节名但无编号")
                continue
            ok, reason = classify(text)
            if not ok and reason == "代码":
                # classify 的 CODE_START_RE 对大小写不敏感，"Let Q = J⊤J 表示
                # Hessian 矩阵。" 这类以 Let 开头的英文句子会被当成 JS 的 let。
                # 以句号收尾、且有 8 个以上单词的，按正文处理。
                words = re.findall(r"[A-Za-z]{2,}", text)
                if len(words) >= 8 and text.rstrip().endswith((".", "。", ":", "：")):
                    block.kind = "body"
                    block.confidence = 0.9
                    block.notes.append("疑似代码但具备完整句子结构，按正文处理")
                    continue
                block.kind = "code"
                block.translatable = False
                block.confidence = 0.85
                block.notes.append(reason)
                continue
            if not ok and len(text) <= 160 and re.search(r"[=<>^_±≤≥√∑∫]", text):
                block.kind = "equation"
                block.confidence = 0.75
                block.notes.append(reason)
                continue
            block.kind = "body"
            block.confidence = 0.95
            if not ok:
                block.translatable = False
                block.notes.append(reason)

    # 参考文献条目常被 PDF 拆成两块（续行以 ":949–959" / 小写作者名开头），
    # 不合并就会在译文中出现以标点开头的独立段落。
    for blocks in raw_pages:
        merged: list[_Block] = []
        for block in blocks:
            previous = merged[-1] if merged else None
            if (
                previous is not None
                and block.kind == previous.kind == "reference"
                and block.text[:1]
                and (block.text[0] in NO_LINE_START or block.text[0].islower())
                and block.rect.y0 - previous.rect.y1 < block.size * 2.4
                and abs(block.rect.x0 - previous.rect.x0) < 14
            ):
                previous.text = f"{previous.text} {block.text}".strip()
                previous.bbox |= block.rect
                continue
            merged.append(block)
        blocks[:] = merged

    # 公式编号常被拆成 ", (4)"、". (17)" 这样的独立小块。它们既不是正文也不是
    # 独立公式，留在版面上会出现“标点开头的行”。这里并回同一行的前一个可排版块。
    punctuation_only = re.compile(r"^[,.;:，。；：]\s*(?:\(\d+\))?$")
    for blocks in raw_pages:
        merged = []
        for block in blocks:
            if punctuation_only.match(block.text.strip()):
                centre = (block.rect.y0 + block.rect.y1) / 2
                for candidate in reversed(merged[-6:]):
                    if candidate.kind not in {"body", "equation"}:
                        continue
                    other = (candidate.rect.y0 + candidate.rect.y1) / 2
                    if abs(centre - other) < max(block.size, candidate.size) * 1.5:
                        candidate.text = f"{candidate.text} {block.text}".strip()
                        candidate.bbox |= block.rect
                        block = None
                        break
                if block is None:
                    continue
            merged.append(block)
        blocks[:] = merged

    # 数学/算法碎片整体还原：碎片无法重排成可读文本时按原版区域截图。
    for page_index, blocks in enumerate(raw_pages):
        raw_pages[page_index] = _merge_math_zones(blocks, doc[page_index])

    # 首页最大字号块作为文档标题；标题之后、第一个章节标题之前的行算副标题。
    if raw_pages and raw_pages[0]:
        first_page = raw_pages[0]
        title = max(first_page, key=lambda b: b.size)
        if title.size >= body_size * TITLE_MIN_RATIO:
            title.kind = "title"
            title.confidence = 0.9 if title.bold else 0.75
            title.notes.append("首页最大字号块")
            title_pos = first_page.index(title)
            # 标题常被 PyMuPDF 拆成多行/多块；字号一致的后续块并回标题。
            continuation = [
                block
                for block in first_page[title_pos + 1 :]
                if block.size >= title.size - 0.6 and block.kind == "body"
            ][:2]
            if continuation:
                title.text = " ".join([title.text] + [b.text for b in continuation])
                for block in continuation:
                    first_page.remove(block)
            for block in first_page[title_pos + 1 :]:
                if block.kind == "heading":
                    break
                if block.kind == "body":
                    block.kind = "subtitle"
                    block.confidence = 0.55
                    block.notes.append("标题与首个章节标题之间的短块，按作者/单位处理")
            subtitles = [block for block in first_page if block.kind == "subtitle"]
            if len(subtitles) > 1:
                head = subtitles[0]
                head.text = " ".join(block.text for block in subtitles)
                for block in subtitles[1:]:
                    first_page.remove(block)

    # 无编号文档（报告、白皮书）里加粗小标题就是一级标题，不应降成二级。
    has_numbered = any(
        _numbering_level(block.text) > 0
        for blocks in raw_pages
        for block in blocks
        if block.kind == "heading"
    )
    if not has_numbered:
        for blocks in raw_pages:
            for block in blocks:
                if (
                    block.kind == "heading"
                    and block.level >= 2
                    and block.size >= body_size * 1.15
                ):
                    block.level = 1

    elements: list[Element] = []
    reference_started = False
    for page_index, blocks in enumerate(raw_pages):
        tables = tables_by_page[page_index]
        figures = figures_by_page[page_index]
        page_bottom = doc[page_index].rect.y1
        page_rect = doc[page_index].rect
        captions = [b for b in blocks if b.kind == "caption" and b.label.startswith("图")]

        wide_sorted = sorted(
            [b for b in blocks if b.rect.width > page_rect.width * WIDE_RATIO],
            key=lambda b: b.rect.y0,
        )

        def key_pos(rect: fitz.Rect, column: int) -> tuple[int, int, float, float]:
            """与正文同一套阅读顺序：通栏块切横带，带内按栏、再按纵坐标。"""
            band = 0
            for item in wide_sorted:
                if rect.y0 >= item.rect.y1:
                    band += 1
                elif rect.y1 <= item.rect.y0:
                    break
            return (band, column, round(rect.y0, 1), rect.x0)

        items: list[tuple[tuple, Element]] = []

        for block in blocks:
            text = block.text
            norm = _norm_heading(text)
            kind = block.kind
            skip = block.skip
            if kind == "heading" and norm in REFERENCE_TITLES:
                reference_started = True
            elif reference_started and kind in ("body", "footnote"):
                kind = "reference"
                skip = True
                block.confidence = 0.9
            if kind == "body" and block.size < body_size * 0.86 and block.rect.y0 > page_bottom - 150:
                kind = "footnote"
                block.confidence = 0.6
                block.notes.append("页底小字号，按脚注处理")
            key = key_pos(block.rect, block.column)
            if block.raster is not None:
                items.append(
                    (
                        key,
                        Element(
                            kind="equation",
                            page=page_index,
                            bbox=tuple(block.rect),
                            size=block.size,
                            image=block.image,
                            image_size=block.image_size,
                            skip=True,
                            column=block.column,
                            confidence=block.confidence,
                            confidence_notes=list(block.notes),
                            translatable=False,
                        ),
                    )
                )
                continue
            items.append(
                (
                    key,
                    Element(
                        kind=kind,
                        text=text,
                        page=page_index,
                        bbox=tuple(block.rect),
                        size=block.size,
                        bold=block.bold,
                        level=block.level,
                        label=block.label,
                        skip=skip,
                        column=block.column,
                        confidence=block.confidence,
                        confidence_notes=list(block.notes),
                        translatable=block.translatable,
                    ),
                )
            )

        for rect, rows, confidence in tables:
            column = _column_of(
                _Block("", page_index, rect, 9.0, 0.0), layouts[page_index].gutters
            )
            # 表格同时留一份原版截图：行列抽取不可靠时（宽数值表、跨栏窄格）
            # 排版阶段直接贴图，比把 13 列挤成 2 个字宽可读得多。
            image, image_size = _raster_region(doc[page_index], rect)
            items.append(
                (
                    key_pos(rect, column),
                    Element(
                        kind="table",
                        page=page_index,
                        bbox=tuple(rect),
                        rows=rows,
                        size=9.0,
                        skip=True,
                        column=column,
                        confidence=confidence,
                        confidence_notes=["find_tables 识别"],
                        image=image,
                        image_size=image_size,
                    ),
                )
            )

        for rect, data, size in figures:
            caption = next(
                (
                    item
                    for item in captions
                    if -2.0 <= item.rect.y0 - rect.y1 <= 90.0
                    and item.rect.x0 < rect.x1
                    and item.rect.x1 > rect.x0
                ),
                None,
            )
            column = _column_of(
                _Block("", page_index, rect, 9.0, 0.0), layouts[page_index].gutters
            )
            items.append(
                (
                    # 图片按底边排序：与图片上沿齐平的文字（标题/副标题）应先读。
                    key_pos(
                        fitz.Rect(rect.x0, rect.y1, rect.x1, rect.y1 + 1.0), column
                    ),
                    Element(
                        kind="figure",
                        page=page_index,
                        bbox=tuple(rect),
                        image=data,
                        image_size=size,
                        skip=True,
                        column=column,
                        confidence=0.95 if caption else 0.8,
                        confidence_notes=["位图对象"] + ([] if caption else ["未找到相邻图注"]),
                    ),
                )
            )

        # 页眉页脚进入元素树（标 skip），便于核对丢弃是否正确。
        for header in headers_by_page[page_index]:
            elements.append(
                Element(
                    kind="header",
                    text=header.text,
                    page=page_index,
                    bbox=tuple(header.rect),
                    size=header.size,
                    skip=True,
                    confidence=0.8,
                    confidence_notes=["位于页眉带，默认丢弃"],
                )
            )
        ordered = sorted(items, key=lambda item: item[0])
        for _key, element in ordered:
            elements.append(element)

        for footer in footers_by_page[page_index]:
            elements.append(
                Element(
                    kind="footer",
                    text=footer.text,
                    page=page_index,
                    bbox=tuple(footer.rect),
                    size=footer.size,
                    skip=True,
                    confidence=0.8,
                    confidence_notes=["位于页脚带，默认丢弃"],
                )
            )

    # 跨栏/跨页续接：CVPR 双栏把一句话在栏底切断，PDF 里是两个独立 block。
    # 不合并就会在译文里出现"……参数要么"+"手动调整或……"这样的断段。
    elements = _merge_continuations(elements)

    # 合并之后仍是短碎片的，多半是图里的标注（`M3[3] applies model fine-tuning`）：
    # 没有句末标点、不到 8 个词，交给模型它会把下一段一起译进来。
    elements = _mark_short_fragments(elements)
    # 参考文献按 "[n]" 重新聚成条目，续行不再各自成段。
    elements = _merge_references(elements)
    # 整栏文献被解析成单个 block 时，编号全落在行中，重排后看不出条目边界；
    # 这里再按 "[n]" 标记把一条大段落拆回逐条文献。
    elements = _split_reference_entries(elements)

    # 图注/表题挂到图/表下，形成"图+图注""表+表题"的结构。
    elements = _attach_captions(elements)

    # 图里的文字（坐标轴标注、流程框里的英文）已经随位图一起贴进新文档，
    # 再按正文排一遍就会在图的下面多出一行孤零零的英文。
    elements = _absorb_figure_labels(elements)

    # 编号与顺序：按最终阅读顺序重排。
    for order, element in enumerate(elements):
        element.order = order
        _assign_ids(element, order)

    page_columns = [layout.count for layout in layouts]
    page_gutters = [layout.gutters for layout in layouts]
    degraded = ""
    if page_columns and max(page_columns) >= 3:
        degraded = "检测到三栏及以上版面：按单栏重排，图表就近放置"

    return Document(
        source=str(doc.name or ""),
        page_size=(page_width, doc[0].rect.height) if doc.page_count else (595.0, 842.0),
        elements=elements,
        body_size=body_size,
        dropped_headers=dropped_headers,
        page_columns=page_columns,
        page_gutters=page_gutters,
        degraded=degraded,
    )


def _split_reference_entries(elements: list[Element]) -> list[Element]:
    """把"整栏文献"拆回逐条。

    有些 PDF 的参考文献整栏就是单个 text block，段落切分拿不到条目边界，
    "[2]""[3]" 全落在行中间，重排后读者无法判断编号是否连续。
    """
    result: list[Element] = []
    for element in elements:
        if element.kind != "reference":
            result.append(element)
            continue
        text = element.text.strip()
        parts = [part.strip() for part in _REF_SPLIT_RE.split(text) if part.strip()]
        if len(parts) <= 1:
            result.append(element)
            continue
        for part in parts:
            clone = replace(element, text=part)
            clone.confidence_notes = list(element.confidence_notes)
            result.append(clone)
    return result


def _merge_references(elements: list[Element]) -> list[Element]:
    """把参考文献的多行碎片合并回条目。

    解析阶段会把一条文献按行距拆成多个 reference 元素，续行没有编号，
    重排后每条都从行首开始，看起来像编号丢失/不连续。这里以 "[n]" 为条目
    起点，把后续无编号的 reference 元素并回上一条。
    """
    result: list[Element] = []
    for element in elements:
        if (
            element.kind == "reference"
            and result
            and result[-1].kind == "reference"
            and not _REF_ITEM_RE.match(element.text.strip())
        ):
            head = result[-1].text.rstrip()
            tail = element.text.lstrip()
            result[-1].text = head[:-1] + tail if head.endswith("-") else head + " " + tail
            continue
        result.append(element)
    return result


def _merge_continuations(elements: list[Element]) -> list[Element]:
    """合并被栏底/页底切断的同一段正文。

    判据：上段没有句末标点、下一段以小写字母开头（英文续行），字号一致；
    页眉页脚、图表与题注不打断判定（句子本就跨过它们），公式与标题会打断。
    """
    terminal = tuple(".!?:;。！？：；…”’\"'")
    result: list[Element] = []
    previous: Element | None = None
    crossed = 0
    for element in elements:
        if element.kind in ("header", "footer", "footnote"):
            # 页脚脚注夹在栏底与下页栏首之间，不能当作段落边界。
            result.append(element)
            continue
        if element.kind in ("figure", "table", "caption", "figure_text"):
            # 图表及其题注插在句子中间（"…第一个解决方案" / 图 1 / "采用…"），
            # 不算段落边界；一个图表区可能是 图+子图标签+题注 多个元素。
            result.append(element)
            crossed = crossed + 1 if previous is not None else 0
            continue
        if (
            previous is not None
            and element.kind == "body"
            and previous.kind == "body"
            and previous.translatable
            and element.translatable
            and abs(previous.size - element.size) <= 0.8
            and _starts_continuation(previous.text, element.text)
            and previous.text.rstrip()[-1:] not in terminal
            and previous.page in (element.page, element.page - 1)
            and crossed <= 6
        ):
            head = previous.text.rstrip()
            if head.endswith("-"):
                # 栏底断词："R-" / "D optimization" → "R-D optimization"
                previous.text = head[:-1] + element.text.lstrip()
            else:
                previous.text = head + " " + element.text.lstrip()
            previous.confidence_notes.append("跨栏/跨页续接合并")
            continue
        result.append(element)
        previous = element if element.kind in ("body",) else None
        crossed = 0
    return result


def _mark_short_fragments(elements: list[Element]) -> list[Element]:
    """把合并后仍不成句的短片段标为不可译（原文保留）。

    但若紧跟着还有正文，就先尝试并进去：跨栏/跨页被切断的句子常留下半句英文
    （`While promising, token pruning methods have certain` + 下一段的「缺点在于…」），
    直接保留原文会在中文段落中间夹一句英文。
    """
    merged: list[Element] = []
    index = 0
    while index < len(elements):
        element = elements[index]
        text = element.text.strip()
        dangling = (
            element.kind == "body"
            and not element.skip
            and text
            and len(text.split()) < 8
            and not text.endswith((".", "。", "?", "!", ":", "："))
        )
        if dangling:
            # 中间可能夹着页眉页脚（`9392`）等 skip 元素，往后找真正的下一段。
            target = None
            for offset in (1, 2, 3):
                if index + offset >= len(elements):
                    break
                candidate = elements[index + offset]
                if candidate.kind in ("footer", "header") or candidate.skip:
                    continue
                if candidate.kind == "body" and not candidate.children:
                    target = candidate
                break
            if target is not None:
                target.text = f"{text} {target.text.lstrip()}"
                target.confidence_notes.append("拼接栏底断句")
                index += 1
                continue
            element.translatable = False
            element.confidence_notes.append("短碎片，保留原文")
        merged.append(element)
        index += 1
    return merged


def _absorb_figure_labels(elements: list[Element]) -> list[Element]:
    """丢弃落在图片区域里的文字块。

    位图对象（流程图、架构图）里的标注本来就是图的一部分，另存成截图时已经带上；
    若再作为 body 排一遍，就会在图的下面多出一行没翻译的英文（`M3[3] applies
    model fine-tuning`）。只处理「大体落在图内」（覆盖率 ≥70%）的短文字块，
    正文段落即使与图有交叠也不会被误删。
    """
    boxes = [
        (element.page, fitz.Rect(element.bbox))
        for element in elements
        if element.kind == "figure" and element.image
    ]
    if not boxes:
        return elements
    text_kinds = {"title", "subtitle", "heading", "body", "caption", "footnote"}
    result: list[Element] = []
    for element in elements:
        if element.kind in text_kinds and element.text.strip():
            rect = fitz.Rect(element.bbox)
            area = max(rect.get_area(), 1.0)
            for page, box in boxes:
                if page != element.page:
                    continue
                overlap = rect & box
                if not overlap.is_empty and overlap.get_area() / area >= 0.7:
                    element.skip = True
                    element.translatable = False
                    element.skip_reason = element.skip_reason or "图内文字"
                    break
        result.append(element)
    return result


def _attach_captions(elements: list[Element]) -> list[Element]:
    """把紧邻图/表的题注作为子节点挂接，避免图注与图分家。"""
    result: list[Element] = []
    consumed: set[int] = set()
    for index, element in enumerate(elements):
        if index in consumed:
            continue
        if element.kind in ("figure", "table"):
            child = None
            for offset in (1, -1):
                probe = index + offset
                if probe in consumed or not (0 <= probe < len(elements)):
                    continue
                candidate = elements[probe]
                if candidate.kind != "caption" or candidate.page != element.page:
                    continue
                already_top = any(candidate is item for item in result)
                if already_top and not (offset == -1 and result and result[-1] is candidate):
                    continue
                if any(candidate is child for item in result for child in item.children):
                    continue
                child = candidate
                consumed.add(probe)
                if offset == -1 and result and result[-1] is candidate:
                    # 表题原本排在表前面，挂到表下后从顶层移除，避免重复。
                    result.pop()
                break
            if child is not None:
                element.children.append(child)
            elif not element.confidence_notes:
                element.confidence_notes.append("未绑定题注")
        result.append(element)
    return result


def _assign_ids(element: Element, order: int) -> None:
    element.id = f"e{order + 1}"
    for child_index, child in enumerate(element.children, start=1):
        child.id = f"{element.id}.{child_index}"
        child.order = order
