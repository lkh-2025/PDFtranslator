"""中文论文默认样式表：样式与内容分离。

这里的默认值不抄任何一篇 PDF，而是排印常识的合理区间：
正文 10.5pt、段首 2 字符、标题按层级递减；行距按"版心宽度 + 段落密度"
在 1.5～1.9 之间自适应。排版器只认样式名，不认具体论文。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# 页面与版心（A4，左右 64pt、上下 66pt ≈ 2.2cm）
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
MARGIN_X = 64.0
MARGIN_TOP = 66.0
MARGIN_BOTTOM = 66.0

# 正文基准
BODY_SIZE = 10.5
MIN_LINE_RATIO = 1.50
MAX_LINE_RATIO = 1.90
BASE_LINE_RATIO = 1.70

Color = tuple[float, float, float]
BLACK: Color = (0.0, 0.0, 0.0)
GRAY: Color = (0.35, 0.35, 0.35)
LIGHT_GRAY: Color = (0.55, 0.55, 0.55)


def adaptive_line_ratio(
    measure_width: float,
    size: float = BODY_SIZE,
    chars: int = 0,
) -> float:
    """按版心宽度与段落长度取行距倍数，限制在 1.5～1.9。

    - 版心窄（<380pt）时行略松一点补偿每行字数少；
    - 长段落（>=10 行、>=16 行）逐步放行，避免密集文字墙；
    - 一兩行的短块收紧，避免松散。
    """
    per_line = max(1.0, measure_width / max(1.0, size))
    lines = max(1, math.ceil(max(0, chars) / per_line)) if chars else 1
    ratio = BASE_LINE_RATIO
    if measure_width < 380.0:
        ratio -= 0.08
    elif measure_width > 520.0:
        ratio += 0.04
    if lines >= 10:
        ratio += 0.08
    if lines >= 16:
        ratio += 0.05
    if lines <= 2:
        ratio -= 0.12
    ratio = min(MAX_LINE_RATIO, max(MIN_LINE_RATIO, ratio))
    return round(ratio * 20) / 20  # 以 0.05 为步长


@dataclass(frozen=True)
class Style:
    """一种排版样式。line_ratio=0 表示交给 adaptive_line_ratio 计算。"""

    name: str
    size: float
    bold: bool = False
    align: str = "left"
    indent_chars: float = 0.0
    gap_before: float = 0.0
    gap_after: float = 0.0
    color: Color = BLACK
    justify: bool = True
    line_ratio: float = 0.0

    def ratio_for(self, measure_width: float, chars: int = 0) -> float:
        if self.line_ratio:
            return self.line_ratio
        return adaptive_line_ratio(measure_width, self.size, chars)


@dataclass
class StyleSheet:
    """样式表：样式名 → 样式。用户配置可按名字覆盖任一字段。"""

    styles: dict[str, Style] = field(default_factory=dict)

    def get(self, name: str) -> Style:
        return self.styles[name]

    def resolve(self, kind: str, level: int = 0) -> Style:
        """把文档元素类型映射到样式名；未知类型按正文处理。"""
        if kind == "heading":
            if level <= 1:
                return self.styles["heading1"]
            if level == 2:
                return self.styles["heading2"]
            return self.styles["heading3"]
        return self.styles.get(kind, self.styles["body"])

    def scaled(self, factor: float) -> "StyleSheet":
        """整表等比缩放：小开本/窄版心时调用，仍保持层级关系。"""
        return StyleSheet(
            {
                name: Style(
                    **{
                        **style.__dict__,
                        "size": round(style.size * factor, 2),
                        "gap_before": round(style.gap_before * factor, 2),
                        "gap_after": round(style.gap_after * factor, 2),
                    }
                )
                for name, style in self.styles.items()
            }
        )

    def as_dict(self) -> dict[str, dict]:
        return {
            name: {
                "size": style.size,
                "bold": style.bold,
                "align": style.align,
                "indent_chars": style.indent_chars,
                "gap_before": style.gap_before,
                "gap_after": style.gap_after,
                "justify": style.justify,
                "line_ratio": style.line_ratio,
            }
            for name, style in self.styles.items()
        }


def default_sheet() -> StyleSheet:
    """通用中文论文样式表（不来自任何具体 PDF）。"""
    return StyleSheet(
        {
            # 文档标题与副标题（作者/单位）
            "title": Style(
                "title", 19.0, bold=True, align="center",
                gap_before=8.0, gap_after=10.0, justify=False, line_ratio=1.35,
            ),
            "subtitle": Style(
                "subtitle", 10.5, align="center",
                gap_after=18.0, justify=False, line_ratio=1.40, color=GRAY,
            ),
            # 章节标题：字号按层级递减，间距同步收窄
            "heading1": Style(
                "heading1", 15.0, bold=True,
                gap_before=16.0, gap_after=6.0, justify=False, line_ratio=1.35,
            ),
            "heading2": Style(
                "heading2", 12.5, bold=True,
                gap_before=12.0, gap_after=4.0, justify=False, line_ratio=1.38,
            ),
            "heading3": Style(
                "heading3", 11.0, bold=True,
                gap_before=10.0, gap_after=3.0, justify=False, line_ratio=1.40,
            ),
            # 正文：10.5pt、段首 2 字符、两端对齐；全篇固定 1.70 行距，
            # 避免同一页里不同段落行距忽大忽小。
            "body": Style(
                "body", BODY_SIZE, indent_chars=2.0,
                gap_after=4.0, justify=True, line_ratio=1.62,
            ),
            # 图注在图下，表题在表上，都居中
            "caption": Style(
                "caption", 9.0, align="center",
                gap_before=4.0, gap_after=12.0, justify=False,
                line_ratio=1.45, color=GRAY,
            ),
            "table_caption": Style(
                "table_caption", 9.0, align="center",
                gap_before=6.0, gap_after=4.0, justify=False,
                line_ratio=1.45, color=GRAY,
            ),
            "table_cell": Style(
                "table_cell", 9.0, justify=False, line_ratio=1.40,
            ),
            "equation": Style(
                "equation", BODY_SIZE, align="center",
                gap_before=4.0, gap_after=8.0, justify=False,
                line_ratio=1.55, color=(0.15, 0.15, 0.15),
            ),
            "reference": Style(
                "reference", 9.0, justify=False, gap_after=3.0, line_ratio=1.40,
            ),
            "footnote": Style(
                "footnote", 9.0, justify=False, gap_after=4.0,
                line_ratio=1.40, color=GRAY,
            ),
            "page_number": Style(
                "page_number", 9.0, align="center", justify=False,
                line_ratio=1.40, color=GRAY,
            ),
            "header": Style(
                "header", 8.5, justify=False, line_ratio=1.30, color=LIGHT_GRAY,
            ),
            "footer": Style(
                "footer", 8.5, justify=False, line_ratio=1.30, color=LIGHT_GRAY,
            ),
        }
    )
