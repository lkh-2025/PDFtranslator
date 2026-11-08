"""中文排版生成（阶段 ④）：新开页面按中文规范重排，不沿用原文坐标。

排版器只认样式表（styles.py）里的样式名：正文/标题/图注/表题/表内文字/
公式/参考文献/页脚。正文 10.5pt、段首 2 字符、两端对齐，行距按版心宽度
与段落密度在 1.5～1.9 之间自适应；标题按层级递减；图表居中、题注跟随。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import fitz

from . import cjk
from .cjk import BOLD_FONT_NAME, FALLBACK_FONT_NAME, FONT_NAME
from .mathtext import normalize_math
from .structure import Document, Element, split_caption
from .styles import (
    MARGIN_BOTTOM,
    MARGIN_TOP,
    MARGIN_X,
    Style,
    StyleSheet,
    default_sheet,
)

CAPTION_LINE_RATIO = 1.45
TABLE_MIN_COL = 34.0
# 结构阶段按 200 dpi 截的图，换算回版面尺寸（pt）用。
RASTER_ZOOM = 200.0 / 72.0


def _natural_pts(element: Element) -> float:
    """截图的原始版面宽度（pt）；拿不到时返回 0。"""
    width, height = element.image_size
    if width <= 0 or height <= 0:
        return 0.0
    return width / RASTER_ZOOM
GRAY = (0.35, 0.35, 0.35)
LIGHT_GRAY = (0.55, 0.55, 0.55)


@dataclass
class LayoutStats:
    pages: int = 0
    lines: int = 0
    missing: list[str] | None = None

    def as_dict(self) -> dict:
        return {
            "pages": self.pages,
            "lines": self.lines,
            "missing_glyphs": self.missing or [],
        }


class Typographer:
    def __init__(
        self,
        dst: Path,
        page_size: tuple[float, float],
        font_file: Path | None,
        sheet: StyleSheet | None = None,
    ):
        self.dst = Path(dst)
        self.stack = cjk.font_stack(font_file)
        self.font_file = self.stack.primary_path
        self.bold_file = self.stack.bold_path
        self.symbol_file = self.stack.symbol_path
        self.font = self.stack.primary
        self.bold_font = self.stack.bold
        self.sheet = sheet or default_sheet()
        self.doc = fitz.open()
        self.pw, self.ph = page_size
        self.left = MARGIN_X
        self.right = self.pw - MARGIN_X
        self.width = self.right - self.left
        self.top = MARGIN_TOP
        self.bottom = self.ph - MARGIN_BOTTOM
        self.page: fitz.Page
        self.y = self.top
        self.stats = LayoutStats()
        self.pending_captions: list[tuple[str, str, bool]] = []
        # 放不下整页的图/表先挂起，让后面的正文把当前页填满，再在新页顶部落地。
        self.pending_floats: list[tuple[Element, list[tuple[str, str, bool]]]] = []
        self._flushing_floats = False
        self.body_height = self.bottom - self.top
        self.page_footnotes: list[str] = []
        self.footnote_reserve = 0.0
        self._in_footnotes = False
        self._ink_bottom = 0.0
        self._new_page()

    # ---------- 页面 ----------

    def _new_page(self) -> None:
        self._flush_footnotes()
        self.page = self.doc.new_page(width=self.pw, height=self.ph)
        self.page.insert_font(fontname=FONT_NAME, fontfile=str(self.font_file))
        if self.bold_file:
            self.page.insert_font(fontname=BOLD_FONT_NAME, fontfile=str(self.bold_file))
        if self.symbol_file:
            self.page.insert_font(fontname=FALLBACK_FONT_NAME, fontfile=str(self.symbol_file))
        self.y = self.top
        self._ink_bottom = self.top
        self.footnote_reserve = 0.0
        if self.page_footnotes and not self._flushing_floats:
            # 上一页塞不下的脚注留到本页页底：只占位，等本页排完再画
            # （在本页开头就画会被后续正文覆盖）。
            self.footnote_reserve = self._footnotes_height_of(self.page_footnotes) + 8.0
        if self.pending_floats and not self._flushing_floats:
            self._flush_floats()

    def _float_height(self, element: Element) -> float:
        """图/表截图在本版式下占的高度（含随附题注）。"""
        if not element.image:
            return 0.0
        image_width, image_height = element.image_size
        if image_width <= 0 or image_height <= 0:
            image_width, image_height = 1, 1
        if element.kind == "table":
            width, cap = self.width, 380.0
        else:
            natural = _natural_pts(element)
            width = min([w for w in (self.width, 460.0, natural * 1.15 if natural else 1e9) if w > 0])
            cap = 330.0 if element.kind == "equation" else 420.0
        height = width * image_height / image_width
        if height > cap:
            height = cap
        need = height + 10.0
        for _label, text, is_table in self.pending_captions:
            style = self.sheet.get("table_caption" if is_table else "caption")
            need += (
                style.size
                * 1.45
                * max(1, len(cjk.wrap(text, self.stack, style.size, self.width)))
                + 4.0
            )
        return need

    def defer_float(self, element: Element) -> bool:
        """放不下当前页、但单独一页放得下时挂起，交给后续正文填满本页。"""
        height = self._float_height(element)
        if not height:
            return False
        remaining = self.bottom - self.y
        if height <= remaining + 0.5:
            return False
        if height > self.body_height * 0.92:
            return False  # 整页也放不下，正常走分页逻辑
        self.pending_floats.append((element, self.pending_captions))
        self.pending_captions = []
        return True

    def attach_caption_to_float(self) -> bool:
        """紧跟在挂起图后面的题注（源文件里题注常单独成块）一并挂到那张图上。"""
        if not self.pending_floats or self.pending_floats[-1][1]:
            return False
        element, _captions = self.pending_floats[-1]
        self.pending_floats[-1] = (element, list(self.pending_captions))
        self.pending_captions = []
        return True

    def _flush_floats(self) -> None:
        if self._flushing_floats:
            return
        self._flushing_floats = True
        try:
            while self.pending_floats:
                element, captions = self.pending_floats.pop(0)
                self.pending_captions = list(captions)
                if element.kind == "table":
                    self.table_image(element)
                elif element.kind == "equation":
                    self.equation_image(element)
                else:
                    self.figure(element)
                self._flush_caption()
        finally:
            self._flushing_floats = False

    def _ensure(self, height: float) -> None:
        # 页脚区与浮动块落地期间都不能再翻页：脚注已经排在页底，
        # 浮动块也在新页顶部，再翻一次会留下一整页空白。
        if self._in_footnotes or self._flushing_floats:
            return
        limit = self.bottom - self.footnote_reserve
        if self.y + height > limit + 0.5 and self.y > self.top + 0.5:
            self._new_page()

    # ---------- 文字 ----------

    def _put(
        self,
        text: str,
        x: float,
        baseline: float,
        size: float,
        color,
        bold: bool = False,
    ) -> float:
        if not text:
            return 0.0
        cursor = x
        for name, piece in self.stack.runs(text, bold):
            self.page.insert_text(
                (cursor, baseline), piece, fontname=name, fontsize=size, color=color
            )
            cursor += self.stack.measure(piece, size, bold)
        return cursor - x

    def _draw_line(
        self,
        text: str,
        size: float,
        line_height: float,
        color,
        align: str,
        indent: float,
        justify: bool,
        bold: bool = False,
    ) -> None:
        font = self.bold_font if bold else self.font
        baseline = self.y + font.ascender * size
        natural = self.stack.measure(text, size, bold)
        if align == "center":
            x = self.left + (self.width - natural) / 2
        elif align == "right":
            x = self.right - natural
        else:
            x = self.left + indent
        # 内容边界钳制：任何情况下都不让真实墨迹越过版心右界。
        if x + natural > self.right + 0.5 and align == "left":
            x = max(self.left, self.right - natural)
        if justify and align == "left" and len(text) > 1:
            cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
            room = self.right - x - natural
            extra = room / (len(text) - 1)
            # 中西混排或短行强行两端对齐会把词间拉出大洞；只在中文占比高、
            # 拉伸量很小时才对齐，否则保持左对齐。
            if room > 0.5 and cjk >= len(text) * 0.6 and extra <= size * 0.3:
                cursor = x
                for ch in text:
                    cursor += self._put(ch, cursor, baseline, size, color, bold) + extra
                self.y += line_height
                self._ink_bottom = max(self._ink_bottom, self.y)
                self.stats.lines += 1
                return
        self._put(text, x, baseline, size, color, bold)
        self.y += line_height
        self._ink_bottom = max(self._ink_bottom, self.y)
        self.stats.lines += 1

    def paragraph(
        self,
        text: str,
        *,
        size: float,
        bold: bool = False,
        color=(0.0, 0.0, 0.0),
        align: str = "left",
        indent_chars: float = 0.0,
        hanging_chars: float = 0.0,
        line_ratio: float = 1.7,
        justify: bool = True,
        gap_before: float = 0.0,
        gap_after: float = 0.0,
        keep_next: float = 0.0,
    ) -> None:
        text = normalize_math(text)
        lines = cjk.wrap(
            text,
            self.stack,
            size,
            self.width - hanging_chars * size,
            indent_chars * size,
            bold=bold,
        )
        if not lines:
            return
        line_height = size * line_ratio
        # 分页前瞻：本段与下一张公式图必须同页时，宁可整段换页，也不留下
        # “引出句在页底、公式在下一页”的断裂。
        block_height = gap_before + len(lines) * line_height + gap_after
        limit = self.bottom - self.footnote_reserve
        if (
            keep_next
            and block_height + keep_next <= self.bottom - self.top
            and self.y + block_height + keep_next > limit
            and self.y > self.top + 0.5
        ):
            self._new_page()
        self._ensure(gap_before + line_height + gap_after)
        self.y += gap_before
        index = 0
        first_chunk = True
        while index < len(lines):
            room = self.bottom - self.footnote_reserve - self.y
            max_lines = int(room // line_height)
            # 孤行/寡行控制：段首至少两行；段尾不把单独一行甩到下一页。
            # 页脚区不适用（那里本来就只有一两行，翻页会把脚注丢到下一页）。
            if max_lines < 2 and self.y > self.top + 0.5 and not self._in_footnotes:
                self._new_page()
                first_chunk = False
                continue
            if max_lines <= 0:
                if self._in_footnotes:
                    break
                self._new_page()
                first_chunk = False
                continue
            tail = len(lines) - index - max_lines
            if tail == 1 and max_lines > 2:
                max_lines -= 1
            chunk = lines[index : index + max_lines]
            for offset, line in enumerate(chunk):
                if first_chunk and offset == 0:
                    indent = indent_chars * size
                else:
                    # 悬挂缩进：参考文献续行右移，条目边界一眼可辨。
                    indent = hanging_chars * size
                is_last = index + offset == len(lines) - 1
                self._draw_line(
                    line,
                    size,
                    line_height,
                    color,
                    align,
                    indent,
                    justify and not is_last,
                    bold=bold,
                )
            index += len(chunk)
            first_chunk = False
            if index < len(lines):
                self._new_page()
        self.y += gap_after

    def _apply(self, style: Style, text: str, **overrides) -> None:
        params = {
            "size": style.size,
            "bold": style.bold,
            "color": style.color,
            "align": style.align,
            "indent_chars": style.indent_chars,
            "line_ratio": style.ratio_for(self.width, len(text)),
            "justify": style.justify,
            "gap_before": style.gap_before,
            "gap_after": style.gap_after,
        }
        params.update(overrides)
        self.paragraph(text, **params)

    # ---------- 元素 ----------

    def title(self, text: str) -> None:
        self._apply(self.sheet.get("title"), text)

    def subtitle(self, text: str) -> None:
        self._apply(self.sheet.get("subtitle"), text)

    def heading(self, text: str, level: int, extra: float = 0.0) -> None:
        style = self.sheet.resolve("heading", level)
        # 标题后至少能带四行正文；后面紧跟图表时还要给图表留出位置。
        self._ensure(
            style.size * style.ratio_for(self.width, len(text))
            + style.gap_after
            + 4 * self.sheet.get("body").size * 1.7
            + extra
        )
        self._apply(style, text)

    def body(self, text: str, keep_next: float = 0.0) -> None:
        style = self.sheet.get("body")
        # 纯数字/链接/代码块不按中文段落处理，不缩进、不两端对齐。
        if not any("\u4e00" <= ch <= "\u9fff" for ch in text):
            self._apply(style, text, indent_chars=0.0, justify=False, keep_next=keep_next)
            return
        self._apply(style, text, keep_next=keep_next)

    def reserve_footnote(self, text: str) -> None:
        """正文段落开始前就替后面的脚注占住页底，否则正文会一路排到底。"""
        text = (text or "").strip()
        if not text:
            return
        height = self._footnotes_height_of([text])
        if height > self.footnote_reserve:
            self.footnote_reserve = height

    def footnote(self, text: str) -> None:
        # 脚注不参与正文流：先攒着，等这一页排完再压到页底（见 _flush_footnotes）。
        if not text.strip():
            return
        self.page_footnotes.append(text.strip())
        # 页底要给它留位置，否则正文一路排到底、脚注只能被丢掉。
        self.footnote_reserve = self._footnotes_height_of(self.page_footnotes)

    def _flush_footnotes(self) -> None:
        notes = self.page_footnotes
        self.footnote_reserve = 0.0
        if not notes:
            return
        self.page_footnotes = []
        style = self.sheet.get("footnote")
        total = self._footnotes_height_of(notes)
        # 正文实际写到的位置（self.y 会因 gap/浮动块与真实墨迹不一致）。
        inked = []
        for block in self.page.get_text("blocks"):
            if block[3] < self.bottom + 1.0:
                inked.append(float(block[3]))
        for info in self.page.get_image_info():
            if info["bbox"][3] < self.bottom + 1.0:
                inked.append(float(info["bbox"][3]))
        ink = max([self.y, self._ink_bottom, *inked])
        start = max(self.bottom - total, ink + 16.0)
        if start + total > self.bottom + 2.0:
            # 这一页实在没地方：把脚注留到下一页页底（不能就这么排进正文流，
            # 那会在页尾触发翻页，留下一整页只有一行的空白页）。
            self.page_footnotes = list(notes)
            self.footnote_reserve = self._footnotes_height_of(notes)
            return
        self.page.draw_line(
            (self.left, start - 6.0),
            (self.left + self.width * 0.30, start - 6.0),
            color=style.color,
            width=0.4,
        )
        saved = self.y
        self.y = start
        self._in_footnotes = True
        try:
            for text in notes:
                self._apply(style, text, indent_chars=0.0, justify=False)
        finally:
            self._in_footnotes = False
            self.y = saved
        # 脚注已经画在这一页上，后面续排的正文必须停在它上方。
        self.footnote_reserve = max(self.footnote_reserve, self.bottom - (start - 8.0))

    def _footnotes_height_of(self, notes: list[str]) -> float:
        if not notes:
            return 0.0
        style = self.sheet.get("footnote")
        size = style.size
        line_height = size * style.ratio_for(self.width, 80)
        total = 0.0
        for text in notes:
            total += len(cjk.wrap(text, self.stack, size, self.width)) * line_height
        return total + 10.0 + 3.0 * max(0, len(notes) - 1)

    def caption(
        self,
        label: str,
        text: str,
        *,
        is_table: bool = False,
        defer: bool = False,
        keep: float = 0.0,
    ) -> None:
        if label:
            # 译文若还带着 "Figure 1." 之类英文前缀，这里去掉，避免"图 1 Figure 1."。
            _label, body = split_caption(text)
            if _label:
                text = body
        self.pending_captions.append((label, text, is_table))
        if not defer:
            if keep:
                style = self.sheet.get("table_caption" if is_table else "caption")
                line = f"{label}　{text}" if label else text
                lines = cjk.wrap(line, self.stack, style.size, self.width)
                self._ensure(
                    style.size * style.ratio_for(self.width, len(line)) * max(1, len(lines))
                    + style.gap_after
                    + keep
                )
            self._flush_caption()

    def _flush_caption(self) -> None:
        if not self.pending_captions:
            return
        pending, self.pending_captions = self.pending_captions, []
        for label, text, is_table in pending:
            line = f"{label}　{text}" if label else text
            style = self.sheet.get("table_caption" if is_table else "caption")
            # 图注/表题句子长、中西混排多，两端对齐会把词间拉出大洞。
            self._apply(style, line, justify=False)

    def equation(self, text: str) -> None:
        self._apply(self.sheet.get("equation"), text)

    def equation_image(self, element: Element) -> None:
        """公式/算法区整体截图：按版心宽度等比缩放，题注与图同页。"""
        if not element.image:
            return
        image_width, image_height = element.image_size
        if image_width <= 0 or image_height <= 0:
            return
        # 不放大：小公式按原始版面尺寸排，否则一行公式会被拉成半页高。
        natural = _natural_pts(element)
        width = min([w for w in (self.width, 460.0, natural * 1.08 if natural else 1e9) if w > 0])
        height = width * image_height / image_width
        if height > 330.0:
            height = 330.0
            width = height * image_width / image_height
        caption_need = 0.0
        for _label, text, _is_table in self.pending_captions:
            style = self.sheet.get("table_caption" if _is_table else "caption")
            caption_need += (
                style.size
                * 1.45
                * max(1, len(cjk.wrap(text, self.stack, style.size, self.width)))
                + 4.0
            )
        self._ensure(caption_need + height + 12.0)
        self._flush_caption()
        self._ensure(height + 12.0)
        rect = fitz.Rect(
            self.left + (self.width - width) / 2,
            self.y,
            self.left + (self.width + width) / 2,
            self.y + height,
        )
        try:
            self.page.insert_image(rect, stream=element.image, keep_proportion=True)
        except Exception:
            return
        self.y += height + 10.0

    def reference(self, text: str) -> None:
        self._apply(self.sheet.get("reference"), text, hanging_chars=1.0)

    def figure(self, element: Element) -> None:
        if not element.image:
            return
        image_width, image_height = element.image_size
        if image_width <= 0 or image_height <= 0:
            image_width, image_height = 1, 1
        natural = _natural_pts(element)
        width = min([w for w in (self.width, 460.0, natural * 1.25 if natural else 1e9) if w > 0])
        height = width * image_height / image_width
        if height > 420.0:
            height = 420.0
            width = height * image_width / image_height
        # 图与紧随其后的题注必须同页：把题注高度一并计入。
        caption_need = 0.0
        for _label, text, _is_table in self.pending_captions:
            caption_need += self.sheet.get("caption").size * 1.45 * (
                max(1, len(cjk.wrap(text, self.stack, self.sheet.get("caption").size, self.width)) )
            ) + 10.0
        self._ensure(height + 10.0 + caption_need)
        rect = fitz.Rect(
            self.left + (self.width - width) / 2,
            self.y,
            self.left + (self.width + width) / 2,
            self.y + height,
        )
        try:
            self.page.insert_image(rect, stream=element.image, keep_proportion=True)
        except Exception:
            return
        self.y += height + 8.0

    def table_image(self, element: Element) -> None:
        """行列抽取不可靠的表格按原版截图贴入，保留表头对齐与全部数字。"""
        if not element.image:
            return
        image_width, image_height = element.image_size
        if image_width <= 0 or image_height <= 0:
            image_width, image_height = 1, 1
        width = self.width
        height = width * image_height / image_width
        if height > 380.0:
            height = 380.0
            width = height * image_width / image_height
        if self.pending_captions:
            # 表题在上、表格紧跟其后：整块（题注+图）一次预留，避免表题孤悬在上一页页底。
            caption_need = 0.0
            for _label, text, is_table in self.pending_captions:
                style = self.sheet.get("table_caption" if is_table else "caption")
                caption_need += (
                    style.size
                    * 1.45
                    * max(1, len(cjk.wrap(text, self.stack, style.size, self.width)))
                    + 4.0
                )
            self._ensure(caption_need + height + 10.0)
            self._flush_caption()
        self._ensure(height + 10.0)
        rect = fitz.Rect(
            self.left + (self.width - width) / 2,
            self.y,
            self.left + (self.width + width) / 2,
            self.y + height,
        )
        try:
            self.page.insert_image(rect, stream=element.image, keep_proportion=True)
        except Exception:
            return
        self.y += height + 8.0

    def table(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        cell_style = self.sheet.get("table_cell")
        if self.pending_captions:
            # 表题与表格的开头必须同页，避免表题孤悬在上一页页底。
            self._ensure(cell_style.size * CAPTION_LINE_RATIO * 2 + 110.0)
            self._flush_caption()
        size = cell_style.size
        line_height = size * cell_style.ratio_for(self.width, 0) + 0.5
        pad = 3.0
        columns = max(len(row) for row in rows)
        rows = [list(row) + [""] * (columns - len(row)) for row in rows]

        natural = [
            max(self.stack.measure(row[index], size) for row in rows) for index in range(columns)
        ]
        total = sum(natural) or 1.0
        widths = [max(TABLE_MIN_COL, self.width * value / total) for value in natural]
        scale = self.width / sum(widths)
        widths = [value * scale for value in widths]

        cells = [
            [cjk.wrap(text, self.stack, size, width - 2 * pad) for text, width in zip(row, widths)]
            for row in rows
        ]
        heights = [max(1, max(len(item) for item in row)) * line_height + 2 * pad for row in cells]

        header = (cells[0], heights[0])
        body = list(zip(cells[1:], heights[1:]))
        self._ensure(header[1] + (body[0][1] if body else 0) + 8.0)
        self._draw_row(header[0], widths, header[1], size, line_height, pad, bold=True)
        for row, height in body:
            if self.y + height > self.bottom:
                self._new_page()
                self._draw_row(header[0], widths, header[1], size, line_height, pad, bold=True)
            self._draw_row(row, widths, height, size, line_height, pad)
        self.y += 8.0

    def _draw_row(
        self,
        cells: list[list[str]],
        widths: list[float],
        height: float,
        size: float,
        line_height: float,
        pad: float,
        bold: bool = False,
    ) -> None:
        x = self.left
        top = self.y
        font = self.bold_font if bold else self.font
        for index, lines in enumerate(cells):
            rect = fitz.Rect(x, top, x + widths[index], top + height)
            self.page.draw_rect(rect, color=LIGHT_GRAY, width=0.5)
            for offset, line in enumerate(lines):
                baseline = top + pad + font.ascender * size + offset * line_height
                self._put(line, x + pad, baseline, size, (0.0, 0.0, 0.0), bold)
            x += widths[index]
        self.y = top + height

    # ---------- 收尾 ----------

    def finish(self) -> Path:
        self._flush_footnotes()
        if self.page_footnotes:
            # 末页页底也塞不下：退到正文流最后，至少不丢内容。
            style = self.sheet.get("footnote")
            pending, self.page_footnotes = self.page_footnotes, []
            self.footnote_reserve = 0.0
            for text in pending:
                self._apply(style, text, indent_chars=0.0, justify=False)
        total = self.doc.page_count
        style = self.sheet.get("page_number")
        for index, page in enumerate(self.doc):
            page.insert_font(fontname=FONT_NAME, fontfile=str(self.font_file))
            if self.symbol_file:
                page.insert_font(fontname=FALLBACK_FONT_NAME, fontfile=str(self.symbol_file))
            label = f"第 {index + 1} 页 / 共 {total} 页"
            width = self.stack.measure(label, style.size)
            page.insert_text(
                ((self.pw - width) / 2, self.ph - 40.0),
                label,
                fontname=FONT_NAME,
                fontsize=style.size,
                color=style.color,
            )
        self.dst.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(self.dst), garbage=3, deflate=True)
        self.stats.pages = self.doc.page_count
        self.stats.missing = sorted(self.stack.missing)
        self.doc.close()
        return self.dst


_EQUATION_NUMBER_RE = re.compile(r"^\s*[（(]\s*\d{1,3}\s*[)）]\s*")


def _table_rows_usable(rows: list[list[str]]) -> bool:
    """行列结构是否值得按单元格重排：宽数值表常被抽成"一行一格"。"""
    if len(rows) < 2:
        return False
    columns = max(len(row) for row in rows)
    if columns < 2 or columns > 8:
        return False
    cells = [cell.strip() for row in rows for cell in row]
    if not cells:
        return False
    filled = sum(1 for cell in cells if cell)
    if filled < len(cells) * 0.7:
        return False
    for row in rows:
        first = row[0].strip()
        if not first:
            continue
        rest = " ".join(cell.strip() for cell in row[1:] if cell.strip())
        if rest and first.replace(" ", "") == rest.replace(" ", ""):
            return False  # 整行文字同时出现在首格与后续格
        if len(first.split()) >= 6 and sum(1 for cell in row[1:] if cell.strip()) <= 1:
            return False  # 多列表头被塞进首格
    return True


def _equation_image_height(typographer: "Typographer", element: Element | None) -> float:
    """下一元素若是公式截图，返回其排版高度（用于本段的分页前瞻）。"""
    if element is None or element.kind != "equation" or not element.image:
        return 0.0
    image_width, image_height = element.image_size
    if image_width <= 0 or image_height <= 0:
        return 0.0
    width = min(typographer.width, 460.0)
    height = width * image_height / image_width
    if height > 330.0:
        height = 330.0
    return height + 12.0


def _merge_continuations(elements: list[Element]) -> list[Element]:
    """把被 PDF 分栏/换列截断的同一段落接回去。

    抽取按「块」切段，跨栏断开的句子会变成两三个 body 元素：前一段停在
    「…性能指标范围为」，后一段从「0 到 1，除了…」开始，中间还可能夹一个
    单独成块的引用编号「[12]」。排版前按「上一段没有句末标点 + 下一段以
    小写字母/数字/括号/引用开头」合并，避免正文里出现半句独占一段。
    """
    out: list[Element] = []
    for element in elements:
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.kind == "body"
            and element.kind == "body"
            and prev.translation
            and element.translation
        ):
            tail = prev.translation.rstrip()
            head = element.translation.lstrip()
            # 中文段落绝不会以逗号收尾：上一段停在「…集成到工业互联网（IIoT）和边缘设备中，」
            # 下一段接着写，说明是同一句被 PDF 的块切开了。
            dangling_comma = tail.endswith(("，", ",")) and _cjk_char(head[:1])
            # 「…衰减系数 γ 在 [0.」这种：括号没闭合，说明句子被块切断了。
            unbalanced = (
                tail.count("[") > tail.count("]") or tail.count("（") > tail.count("）")
                or tail.count("(") > tail.count(")")
            )
            continues = (
                bool(_CONTINUATION_RE.match(head)) or dangling_comma or unbalanced
            ) and not tail.endswith(
                ("。", "！", "？", "；", "：", "…", "）", "】", "」", "』")
            )
            if continues:
                merged = copy.copy(prev)
                glue = "" if _cjk_char(tail[-1:]) or _cjk_char(head[:1]) else " "
                merged.translation = f"{tail}{glue}{head}"
                out[-1] = merged
                continue
        out.append(element)
    return out


_CONTINUATION_RE = re.compile(r"(?:[a-z0-9]|[（(【\[]|\[[\d,\s–-]{1,12}\])")

_NORM_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _norm_text(text: str) -> str:
    """去掉空白与标点，只留字，用来比对两段是不是同一段。"""
    return _NORM_STRIP_RE.sub("", text or "")


def _coverage(short: str, long_: str) -> float:
    """短串有多大比例（按字符）能在长串里按顺序对上。"""
    if not short:
        return 0.0
    matcher = SequenceMatcher(None, short, long_, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(short)


def _drop_duplicate_bodies(elements: list[Element]) -> list[Element]:
    """丢掉同一段被 PDF 抽了两遍的 body（原论文里的宽公式/侧栏常这样）。

    只比对相邻三个 body、只处理 ≥30 字、且较短的一段有 85% 以上被另一段
    完整包含，才判定重复；两份里保留信息更全的那一份（长的那份）。
    """
    normed = [
        _norm_text(element.translation or element.text) if element.kind == "body" else ""
        for element in elements
    ]
    # 同一段被切两半时，模型的两次译文措辞会不一样（「在 Solution 1 中」vs「在解决方案 1 中」），
    # 单看译文判不出重复；再用原文比一次——原文里前一段就是后一段的前缀。
    normed_src = [
        _norm_text(element.text) if element.kind == "body" else "" for element in elements
    ]
    drop: set[int] = set()
    for index, text in enumerate(normed):
        if len(text) < 30 or index in drop:
            continue
        seen = 0
        for back in range(1, len(normed) + 1):
            prev = index - back
            if prev < 0 or seen >= 3:
                break
            other = normed[prev]
            if len(other) < 30 or prev in drop:
                continue
            seen += 1
            short, long_ = (other, text) if len(other) <= len(text) else (text, other)
            ratio = len(short) / len(long_)
            coverage = _coverage(short, long_)
            src_short, src_long = (
                (normed_src[prev], normed_src[index])
                if len(other) <= len(text)
                else (normed_src[index], normed_src[prev])
            )
            src_prefix = (
                len(src_short) >= 40
                and _coverage(src_short, src_long) >= 0.90
                and len(src_short) / max(1, len(src_long)) >= 0.25
            )
            # 短的那份被长的那份几乎整段包含（≥0.92）时，长度比可以放宽：
            # Zhang 4.4.2 就是「半段被单独译了一次 + 整段又译了一次」。
            if not src_prefix and coverage < (0.92 if ratio < 0.55 else 0.85):
                continue
            if not src_prefix and ratio < 0.40:
                continue
            if len(other) <= len(text):
                drop.add(prev)
            else:
                drop.add(index)
            break
    if not drop:
        return elements
    return [element for index, element in enumerate(elements) if index not in drop]


def _cjk_char(value: str) -> bool:
    return bool(value) and "\u4e00" <= value[0] <= "\u9fff"


def render(
    document: Document,
    dst: Path,
    font_file: str | Path | None,
    sheet: StyleSheet | None = None,
) -> LayoutStats:
    """按文档元素树与样式表重排一版中文 PDF。"""
    typographer = Typographer(dst, document.page_size, font_file, sheet=sheet)
    elements = _drop_duplicate_bodies(_merge_continuations(document.elements))
    previous_image_equation = False
    deferred_index = -10
    for index, element in enumerate(elements):
        nxt_element = elements[index + 1] if index + 1 < len(elements) else None
        if nxt_element is not None and nxt_element.kind == "footnote":
            # 脚注紧跟在这一段后面：先替它占住页底，正文才不会排到底。
            typographer.reserve_footnote(nxt_element.translation or nxt_element.text)
        for child in element.children:
            if child.kind == "caption":
                _render_caption(typographer, child, is_table=element.kind == "table", defer=True)
        if element.kind == "figure":
            if typographer.defer_float(element):
                deferred_index = index
            else:
                typographer.figure(element)
                typographer._flush_caption()
        elif element.kind == "table":
            if element.image and not _table_rows_usable(element.rows):
                if typographer.defer_float(element):
                    deferred_index = index
                else:
                    typographer.table_image(element)
            else:
                typographer.table(element.rows)
        elif element.kind == "title":
            typographer.title(element.translation or element.text)
        elif element.kind == "subtitle":
            typographer.subtitle(element.translation or element.text)
        elif element.kind == "heading":
            nxt = elements[index + 1] if index + 1 < len(elements) else None
            extra = 150.0 if nxt is not None and nxt.kind == "table" else 0.0
            if nxt is not None and nxt.kind == "figure":
                extra = 80.0
            typographer.heading(element.translation or element.text, element.level, extra)
        elif element.kind == "body":
            text = element.translation or element.text
            if previous_image_equation:
                # 公式编号已包含在截图里，正文开头的 "(1)" 是 PDF 归并错误，去掉。
                text = _EQUATION_NUMBER_RE.sub("", text, count=1)
            nxt = elements[index + 1] if index + 1 < len(elements) else None
            # 公式/算法截图现在可以被挂起到下一页，正文不必为了「图跟段」整段搬家
            # （那样会在页底留下大块空白）。只有当下一块是放不下的超高大图时才预留。
            keep = 0.0
            if nxt is not None and nxt.kind == "equation" and nxt.image:
                height = typographer._float_height(nxt)
                if height > typographer.body_height * 0.92:
                    keep = height
            typographer.body(text, keep_next=keep)
        elif element.kind == "code":
            typographer._apply(
                typographer.sheet.get("body"),
                element.text,
                indent_chars=0.0,
                justify=False,
            )
        elif element.kind == "footnote":
            text = element.translation or element.text
            # 公式区里的零碎文字（`WE,WD Ltotal`）会被误判成脚注，按公式排更合适。
            if not any("\u4e00" <= ch <= "\u9fff" for ch in text) and len(text) < 40:
                typographer.equation(text)
            else:
                typographer.footnote(text)
        elif element.kind == "caption":
            nxt = elements[index + 1] if index + 1 < len(elements) else None
            is_algorithm = (element.label or "").startswith("算法")
            is_table = nxt is not None and nxt.kind == "table"
            # 图注后面紧跟图时也要延后：图可能被挂起到下一页，题注不能留在本页。
            follows_figure = nxt is not None and nxt.kind == "figure"
            # 源文件里题注常隔着一两个 figure_text 小块跟在图后面，索引不一定紧邻
            follows_float = 0 < index - deferred_index <= 3 and bool(
                typographer.pending_floats
            )
            _render_caption(
                typographer,
                element,
                # 算法题注是块标题，必须与算法正文同页，按表题（居中、位于内容上方）处理。
                is_table=is_table or is_algorithm,
                # 算法块要整块预留高度，题注必须延后到 equation_image 里一起落版。
                defer=is_table or is_algorithm or follows_figure or follows_float,
                keep=70.0 if is_algorithm else 0.0,
            )
            if follows_float:
                typographer.attach_caption_to_float()
        elif element.kind == "equation":
            if element.image:
                if typographer.defer_float(element):
                    deferred_index = index
                else:
                    typographer.equation_image(element)
            else:
                typographer.equation(element.text)
        elif element.kind == "reference":
            typographer.reference(element.text)
        previous_image_equation = element.kind == "equation" and bool(element.image)
    typographer._flush_floats()
    typographer._flush_caption()
    typographer.finish()
    return typographer.stats


def _render_caption(
    typographer: Typographer,
    element: Element,
    *,
    is_table: bool,
    defer: bool,
    keep: float = 0.0,
) -> None:
    typographer.caption(
        element.label,
        element.translation or element.text,
        is_table=is_table,
        defer=defer,
        keep=keep,
    )
