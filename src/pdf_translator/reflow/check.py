"""重排产物自检：产物没有原文坐标，全部按新页自身的文本度量判断。"""

from __future__ import annotations

import re
from collections import Counter

import fitz

from ..textfilter import NO_LINE_START
from .typeset import MARGIN_BOTTOM, MARGIN_TOP, MARGIN_X

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LETTER_RE = re.compile(r"[A-Za-z]")
EMBEDDED_EXT = {"ttf", "ttc", "otf", "cff", "otc"}
# 正文不允许出现的 LaTeX 残留：命令、\( \)、$...$、裸花括号转义
LATEX_RE = re.compile(r"\\[A-Za-z]{2,}|\\[()\[\]]|\$[^$]{1,60}\$|\\[{}]")
# 模型把"该输出什么"的指令写进译文（Zhang 第 8 页出现过「请直接给出公式或关系式。」）。
INSTRUCTION_RE = re.compile(
    r"(?:请|麻烦|需要)(?:你)?(?:直接)?(?:给出|提供|输出|翻译|写出|列出|补充)"
    r"[^。！？\n]{0,24}(?:公式|译文|结果|内容|关系式|答案|版本|原文)"
    r"|<tool_call>"
)
# NUL/替换符/空心方块/私用区：字体缺字形或数学字体编码噪声，视觉上是空白、方块或丢字
GLYPH_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ue000-\uf8ff\ufffd\u25a1]")
CAPTION_RE = re.compile(r"^\s*图\s*\d+")
# 列表编号被译成「1。步骤」才是缺陷；「0.5。对于…」这种句末小数、以及
# 「算法 2。」这种引用编号都属正常行文（小数带点，不匹配）。
NUMBERING_RE = re.compile(r"^\s*\d{1,2}。")
SUPERSCRIPT_CHARS = set("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˢᵗᵘᵛʷˣʸᶻ₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵪᵀ")
MATHISH_RE = re.compile(r"[=<>^_±≤≥√∑∫∇∈∉⊂⊃∪∩∥∞]|[\u0370-\u03ff]|\d")
BLOCK_CAPTION_RE = re.compile(r"^\s*(?:表|算法)\s*\d+")
MIN_BODY_SIZE = 9.0


def _spans(page: fitz.Page) -> list[tuple[fitz.Rect, str, float, str]]:
    spans: list[tuple[fitz.Rect, str, float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text") or ""
                if not text.strip():
                    continue
                spans.append(
                    (fitz.Rect(span["bbox"]), text, float(span.get("size") or 0.0), str(span.get("font") or ""))
                )
    return spans


def _font_report(
    doc: fitz.Document, cjk_fonts: set[str]
) -> tuple[bool, list[str]]:
    """判断中文正文实际使用的字体是否嵌入，而不是写死宋体。

    排版阶段允许按 宋体 → 思源宋体 → 微软雅黑 回退，自检必须跟随实际
    使用的字体名判断，否则没有宋体的机器会被误判为「字体未嵌入」。
    """
    names: list[str] = []
    embedded_fonts: set[str] = set()
    for page in doc:
        for item in page.get_fonts(full=True):
            _xref, ext, _font_type, basefont, _name = item[:5]
            names.append(f"{basefont} ({ext})")
            if str(ext).lower() in EMBEDDED_EXT:
                embedded_fonts.add(str(basefont))
                embedded_fonts.add(str(basefont).split("+")[-1])

    def is_embedded(font: str) -> bool:
        target = font.lower()
        return any(
            target == base.lower()
            or target in base.lower()
            or base.lower() in target
            for base in embedded_fonts
            if base
        )

    if cjk_fonts:
        embedded = any(is_embedded(font) for font in cjk_fonts)
    else:
        embedded = bool(embedded_fonts)
    return embedded, sorted(set(names))


def _overlaps(spans: list[tuple[fitz.Rect, str, float, str]]) -> list[str]:
    issues: list[str] = []
    ordered = sorted(spans, key=lambda item: (round(item[0].y0, 1), item[0].x0))
    for index, (rect_a, text_a, _size_a, _font_a) in enumerate(ordered):
        for rect_b, text_b, _size_b, _font_b in ordered[index + 1 :]:
            if rect_b.y0 >= rect_a.y1 - 0.5:
                break
            if rect_a.width < 2 or rect_b.width < 2 or rect_a.height < 2 or rect_b.height < 2:
                continue
            inter = rect_a & rect_b
            if inter.is_empty:
                continue
            if inter.get_area() > 1.2:
                issues.append(f"{text_a[:16]!r} × {text_b[:16]!r} 重叠 {inter.get_area():.1f}pt²")
    return issues


def _visual_lines(
    spans: list[tuple[fitz.Rect, str, float, str]]
) -> list[list[tuple[fitz.Rect, str, float, str]]]:
    """按基线把 span 归并成视觉行。

    PDF 里同一行常由多段 insert_text 拼成（中文一种字体、符号另一种字体），
    不同字体的 descent 不同，span 的 y1 会差 1~3pt；直接 round(y1) 分组会把
    一行拆成“前半句 + 以符号开头的一行”，误报行首上标/行首禁则。
    """
    ordered = sorted(spans, key=lambda item: item[0].y1)
    lines: list[list[tuple[fitz.Rect, str, float, str]]] = []
    for span in ordered:
        if lines and abs(span[0].y1 - lines[-1][-1][0].y1) <= 3.0:
            lines[-1].append(span)
        else:
            lines.append([span])
    return lines


def _line_starts(spans: list[tuple[fitz.Rect, str, float, str]]) -> list[str]:
    bad: list[str] = []
    for pieces in _visual_lines(spans):
        first = min(pieces, key=lambda item: item[0].x0)[1].lstrip()
        if first and first[0] in NO_LINE_START:
            bad.append(first[:24])
    return bad


def _line_first_texts(spans: list[tuple[fitz.Rect, str, float, str]]) -> list[tuple[str, fitz.Rect]]:
    result: list[tuple[str, fitz.Rect]] = []
    for pieces in _visual_lines(spans):
        rect, text, _size, _font = min(pieces, key=lambda item: item[0].x0)
        result.append((text, rect))
    return result


def check_output(src_path, dst_path, missed: list[str] | None = None) -> dict:
    """对重排产物做版式、字体与中文覆盖检查。"""
    source = fitz.open(str(src_path))
    output = fitz.open(str(dst_path))
    try:
        source_letters = sum(
            len(LETTER_RE.findall(page.get_text("text"))) for page in source
        )
        output_cjk = sum(len(CJK_RE.findall(page.get_text("text"))) for page in output)

        margin_issues: list[str] = []
        overlap_issues: list[str] = []
        punctuation_issues: list[str] = []
        latex_issues: list[str] = []
        instruction_issues: list[str] = []
        glyph_issues: list[str] = []
        caption_issues: list[str] = []
        heading_issues: list[str] = []
        numbering_issues: list[str] = []
        superscript_issues: list[str] = []
        fragment_issues: list[str] = []
        caption_break_issues: list[str] = []
        tiny_font_issues: list[str] = []
        blank_pages: list[int] = []
        sizes: Counter[float] = Counter()
        pages_with_text: list[bool] = []
        cjk_fonts: set[str] = set()

        for index, page in enumerate(output):
            spans = _spans(page)
            pages_with_text.append(bool(spans))
            if not spans:
                blank_pages.append(index + 1)
                continue
            images = [fitz.Rect(item["bbox"]) for item in page.get_image_info()]
            for rect, text, size, font in spans:
                if CJK_RE.search(text):
                    cjk_fonts.add(font)
                sizes[round(size, 1)] += len(text)
                if LATEX_RE.search(text):
                    latex_issues.append(f"第 {index + 1} 页 {text[:28]!r}")
                if INSTRUCTION_RE.search(text):
                    instruction_issues.append(f"第 {index + 1} 页 {text[:28]!r}")
                if GLYPH_RE.search(text):
                    glyph_issues.append(
                        f"第 {index + 1} 页 ({rect.x0:.0f},{rect.y0:.0f}) {text[:24]!r}"
                    )
                if size < MIN_BODY_SIZE - 0.1:
                    tiny_font_issues.append(
                        f"第 {index + 1} 页 {size:.1f}pt ({rect.x0:.0f},{rect.y0:.0f}) {text[:20]!r}"
                    )
                # 真正的题注是小字号；正文里"表4的最后一行…"这类句子不算。
                if (
                    size <= 9.6
                    and BLOCK_CAPTION_RE.match(text)
                    and rect.y1 > page.rect.height - MARGIN_BOTTOM - 55
                ):
                    caption_break_issues.append(
                        f"第 {index + 1} 页 {text[:22]!r} 位于页底（y={rect.y1:.0f}）"
                    )
                if CAPTION_RE.match(text) and size <= 9.5:
                    if not images:
                        caption_issues.append(
                            f"第 {index + 1} 页 {text[:24]!r} 无图"
                        )
                    else:
                        best = min(
                            max(rect.y0 - item.y1, item.y0 - rect.y1, 0.0) for item in images
                        )
                        if best > 150:
                            caption_issues.append(
                                f"第 {index + 1} 页 {text[:24]!r} 距图 {best:.0f}pt"
                            )
                if size >= 12.5 and rect.y1 > page.rect.height - MARGIN_BOTTOM - 8:
                    heading_issues.append(f"第 {index + 1} 页 {text[:24]!r} 页底标题")
                if rect.y0 >= page.rect.height - MARGIN_BOTTOM:
                    # 页码所在的页脚带，不参与正文边界判定。
                    continue
                if rect.x0 < MARGIN_X - 2 or rect.x1 > page.rect.width - MARGIN_X + 2:
                    margin_issues.append(f"第 {index + 1} 页 {text[:18]!r} 超出左右边界")
                if rect.y0 < MARGIN_TOP - 4:
                    margin_issues.append(f"第 {index + 1} 页 {text[:18]!r} 超出上边界")
                if rect.y1 > page.rect.height - MARGIN_BOTTOM + 4:
                    margin_issues.append(f"第 {index + 1} 页 {text[:18]!r} 超出下边界")

            for first, rect in _line_first_texts(spans):
                # 编号只可能在行首成立。按 span 全量匹配会把 "1e−4。我们使用…"
                # 这种句末数字误判成编号（该 span 恰好从 "4。" 开始）；换行后
                # 的 "算法 2。" 也会被同一个 span 命中。行首编号必须贴着左边距
                # （缩进两字以内）。
                if NUMBERING_RE.match(first) and rect.x0 <= MARGIN_X + 40:
                    numbering_issues.append(
                        f"第 {index + 1} 页 ({rect.x0:.0f},{rect.y0:.0f}) {first[:20]!r}"
                    )
                if first[:1] in SUPERSCRIPT_CHARS:
                    superscript_issues.append(
                        f"第 {index + 1} 页 ({rect.x0:.0f},{rect.y0:.0f}) 行首上标 {first[:16]!r}"
                    )

            # 同页连续出现多条“居中 + 短 + 纯数学”的整行，说明公式仍被拆成碎片。
            # 必须按视觉行合并后再判断：一条公式由多个 span（中文/符号字体）
            # 拼成时，逐 span 统计会把同一个公式数成三条碎片。
            centre = page.rect.width / 2
            fragments: list[str] = []
            for pieces in _visual_lines(spans):
                pieces = sorted(pieces, key=lambda item: item[0].x0)
                text = "".join(item[1] for item in pieces)
                rect = pieces[0][0] | pieces[-1][0]
                size = max(item[2] for item in pieces)
                if (
                    not CJK_RE.search(text)
                    and len(text) <= 30
                    and size >= 9.5
                    and rect.y1 < page.rect.height - MARGIN_BOTTOM - 20
                    and MATHISH_RE.search(text)
                    and abs((rect.x0 + rect.x1) / 2 - centre) < 26
                ):
                    fragments.append(text)
            if len(fragments) >= 3:
                fragment_issues.append(
                    f"第 {index + 1} 页 {len(fragments)} 条居中公式碎片：{fragments[:3]}"
                )

            overlap_issues.extend(
                f"第 {index + 1} 页 {issue}" for issue in _overlaps(spans)
            )
            punctuation_issues.extend(
                f"第 {index + 1} 页 行首标点 {item!r}" for item in _line_starts(spans)
            )

        # 表题/算法题写在页底、正文却从下一页开始：题注与内容被分页拆开。
        kept: list[str] = []
        for issue in caption_break_issues:
            number = int(issue.split(" 页")[0].split()[-1])
            if number < len(pages_with_text) and pages_with_text[number]:
                kept.append(issue)
        caption_break_issues = kept

        embedded, font_names = _font_report(output, cjk_fonts)
        ratio = output_cjk / max(1, source_letters)
        missed = list(missed or [])
        blockers: list[str] = []
        warnings: list[str] = []
        if not embedded:
            blockers.append("中文正文字体未嵌入")
        if margin_issues:
            blockers.append(f"{len(margin_issues)} 处越界")
        if overlap_issues:
            blockers.append(f"{len(overlap_issues)} 处文本重叠")
        if punctuation_issues:
            blockers.append(f"{len(punctuation_issues)} 处行首禁则标点")
        if latex_issues:
            blockers.append(f"{len(latex_issues)} 处 LaTeX 源码残留")
        if instruction_issues:
            blockers.append(f"{len(instruction_issues)} 处提示词回吐")
        if glyph_issues:
            blockers.append(f"{len(glyph_issues)} 处缺字形/私用区字符")
        if numbering_issues:
            blockers.append(f"{len(numbering_issues)} 处编号带全角句号")
        if superscript_issues:
            blockers.append(f"{len(superscript_issues)} 处行首上标（公式被拆散）")
        if fragment_issues:
            blockers.append(f"{len(fragment_issues)} 处公式碎片")
        if caption_break_issues:
            blockers.append(f"{len(caption_break_issues)} 处表题/算法题与内容分页")
        if tiny_font_issues:
            blockers.append(f"{len(tiny_font_issues)} 处字号小于 {MIN_BODY_SIZE}pt")
        if caption_issues:
            blockers.append(f"{len(caption_issues)} 处图注与图分离")
        if heading_issues:
            blockers.append(f"{len(heading_issues)} 处标题孤行")
        if blank_pages:
            blockers.append(f"空白页 {blank_pages}")
        if missed:
            note = f"{len(missed)} 段疑似未翻译/被截断"
            # 个别段落被模型截断时先记警告；整体中文覆盖过低才阻断。
            if ratio < 0.12:
                blockers.append(note)
            else:
                warnings.append(note)
        if ratio < 0.15:
            warnings.append(f"汉字/英文字母比为 {ratio:.2f}，整体中文偏少")

        return {
            "ok": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "missed": missed[:20],
            "source_pages": source.page_count,
            "output_pages": output.page_count,
            "source_letters": source_letters,
            "output_cjk": output_cjk,
            "cjk_per_letter": round(ratio, 3),
            "font_embedded": embedded,
            "fonts": font_names,
            "font_sizes": {str(k): v for k, v in sorted(sizes.items())},
            "margin_issues": margin_issues[:20],
            "overlap_issues": overlap_issues[:20],
            "punctuation_issues": punctuation_issues[:20],
            "latex_issues": latex_issues[:20],
            "instruction_issues": instruction_issues[:20],
            "glyph_issues": glyph_issues[:20],
            "numbering_issues": numbering_issues[:20],
            "superscript_issues": superscript_issues[:20],
            "fragment_issues": fragment_issues[:20],
            "caption_break_issues": caption_break_issues[:20],
            "tiny_font_issues": tiny_font_issues[:20],
            "caption_issues": caption_issues[:20],
            "heading_issues": heading_issues[:20],
            "blank_pages": blank_pages,
        }
    finally:
        source.close()
        output.close()
