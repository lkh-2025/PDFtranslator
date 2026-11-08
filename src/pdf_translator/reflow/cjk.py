"""中文排版基础：宋体加载、字宽度量、断行与两端对齐绘制。

只用系统自带字体；找不到 simsun.ttc 时直接报错并给出替代方案，
不会静默改用黑体，避免版式与预期不一致。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import fitz

from ..textfilter import NO_LINE_START

FONT_NAME = "simsun"
BOLD_FONT_NAME = "simhei"
FALLBACK_FONT_NAME = "symfont"
NO_LINE_END = set("（《「『【“‘([{")
_WORD_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-/@:#&'’")
# 上下标字符：排版上属于前一个字符的附属符号，断行时必须粘在一起。
_SCRIPT_CHARS = set(
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˢᵗᵘᵛʷˣʸᶻᵅᵝᵞᵟᶿᵠᵡ"
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵪᵀ"
)

# 字体回退链：宋体 → 思源宋体 → 微软雅黑。只在系统字体里找，不联网、不内置字体。
SERIF_CANDIDATES = [
    ("simsun.ttc", "宋体"),
    ("SIMSUM.TTC", "宋体"),
    ("SourceHanSerifSC-Regular.otf", "思源宋体"),
    ("SourceHanSerifSC-Regular.ttf", "思源宋体"),
    ("NotoSerifCJKsc-Regular.otf", "思源宋体"),
    ("NotoSerifSC-Regular.otf", "思源宋体"),
    ("msyh.ttc", "微软雅黑"),
    ("MSYH.TTC", "微软雅黑"),
]
SANS_CANDIDATES = [
    ("simhei.ttf", "黑体"),
    ("SIMHEI.TTF", "黑体"),
    ("SourceHanSansSC-Bold.otf", "思源黑体"),
    ("NotoSansCJKsc-Bold.otf", "思源黑体"),
    ("msyhbd.ttc", "微软雅黑 Bold"),
]
# 数学符号回退：宋体缺 ∇、ℝ、∥ 等字形，缺字形会被写成 NUL 并造成叠字。
SYMBOL_CANDIDATES = [
    ("cambria.ttc", "Cambria"),
    ("seguisym.ttf", "Segoe UI Symbol"),
    ("arialuni.ttf", "Arial Unicode MS"),
    ("times.ttf", "Times New Roman"),
]
_warned: set[str] = set()


class ReflowFontError(RuntimeError):
    """缺少可嵌入的中文字体。"""


def _font_dirs() -> list[Path]:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    dirs = [windir / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return dirs


def _first_available(candidates: list[tuple[str, str]]) -> tuple[Path, str] | None:
    for name, label in candidates:
        for folder in _font_dirs():
            target = folder / name
            if target.is_file():
                return target, label
    return None


def resolve_font(preferred: str | Path | None = None) -> tuple[Path, str]:
    """返回（字体文件, 中文字体名）；按 宋体 → 思源宋体 → 微软雅黑 回退。"""
    if preferred:
        target = Path(preferred)
        if not target.is_file():
            raise ReflowFontError(f"指定的字体文件不存在：{target}")
        return target, target.stem
    found = _first_available(SERIF_CANDIDATES)
    if found is None:
        raise ReflowFontError(
            "找不到可嵌入的中文字体（已尝试 宋体 simsun.ttc、思源宋体、微软雅黑）。\n"
            "可选方案：1) 安装宋体；2) 用 --font 指定其它中文 TTF/TTC；"
            "3) 在界面「高级设置」里指定字体文件。"
        )
    path, label = found
    if label != "宋体" and "serif-fallback" not in _warned:
        _warned.add("serif-fallback")
        print(
            f"提示：未找到宋体 simsun.ttc，正文改用系统字体「{label}」（{path.name}）。"
            "如版式不符合预期，请安装宋体或用 --font 指定字体。",
            file=sys.stderr,
            flush=True,
        )
    return path, label


def font_path() -> Path:
    """正文宋体文件路径（缺失时按回退链取系统字体并提示）。"""
    return resolve_font()[0]


def load_font(path: str | Path | None = None) -> fitz.Font:
    target = Path(path) if path else font_path()
    if not target.is_file():
        raise ReflowFontError(f"字体文件不存在：{target}")
    return fitz.Font(fontfile=str(target))


def bold_font_path() -> Path | None:
    """标题用黑体；系统没有时按 思源黑体 → 微软雅黑 Bold 回退。"""
    if "simhei" not in _warned:
        found = _first_available(SANS_CANDIDATES)
        if found is None:
            _warned.add("simhei")
            print(
                "提示：未找到黑体等标题字体，标题将使用正文字体（不加粗）。",
                file=sys.stderr,
                flush=True,
            )
            return None
        _warned.add("simhei")
        return found[0]
    found = _first_available(SANS_CANDIDATES)
    return found[0] if found else None


def symbol_font_path() -> Path | None:
    """数学符号回退字体；系统里一个都没有时返回 None（缺字形会由自检拦截）。"""
    found = _first_available(SYMBOL_CANDIDATES)
    return found[0] if found else None


class FontStack:
    """正文/粗体/符号三套字体的度量与回退。

    宋体缺 ∇（U+2207）、ℝ 等字形时，PyMuPDF 会写入 NUL 且步进与度量不一致，
    既产生空白又造成文字叠压。这里逐字选字体，测量与实际绘制走同一套度量。
    """

    def __init__(
        self,
        primary_path: str | Path,
        bold_path: str | Path | None = None,
        symbol_path: str | Path | None = None,
    ) -> None:
        self.primary_path = Path(primary_path)
        self.primary = fitz.Font(fontfile=str(self.primary_path))
        self.bold_path = Path(bold_path) if bold_path else None
        self.bold = fitz.Font(fontfile=str(self.bold_path)) if self.bold_path else self.primary
        self.symbol_path = Path(symbol_path) if symbol_path else None
        self.symbol = (
            fitz.Font(fontfile=str(self.symbol_path)) if self.symbol_path else None
        )
        self.missing: set[str] = set()
        self._cache: dict[tuple[bool, str], tuple[fitz.Font, str] | None] = {}

    def resolve(self, ch: str, bold: bool = False) -> tuple[fitz.Font, str] | None:
        """返回（字体, 字体名）；两套字体都没有该字形时返回 None（调用方跳过）。"""
        key = (bool(bold), ch)
        if key in self._cache:
            return self._cache[key]
        main, main_name = (
            (self.bold, BOLD_FONT_NAME) if bold and self.bold_path else (self.primary, FONT_NAME)
        )
        code = ord(ch)
        if ch in " \n\t" or main.has_glyph(code):
            result: tuple[fitz.Font, str] | None = (main, main_name)
        elif self.symbol is not None and self.symbol.has_glyph(code):
            result = (self.symbol, FALLBACK_FONT_NAME)
        else:
            # 两套字体都没有：记录后跳过，绝不写空心方块占位（视觉上等于丢字）。
            self.missing.add(ch)
            result = None
        self._cache[key] = result
        return result

    def advance(self, ch: str, size: float, bold: bool = False) -> float:
        hit = self.resolve(ch, bold)
        if hit is None:
            return 0.0
        return hit[0].text_length(ch, fontsize=size)

    def measure(self, text: str, size: float, bold: bool = False) -> float:
        total = 0.0
        for ch in text:
            hit = self.resolve(ch, bold)
            if hit is None:
                continue
            total += hit[0].text_length(ch, fontsize=size)
        return total

    def runs(self, text: str, bold: bool = False) -> list[tuple[str, str]]:
        """把一行文字切成（字体名, 片段）序列，相邻同字体合并。"""
        pieces: list[tuple[str, str]] = []
        for ch in text:
            hit = self.resolve(ch, bold)
            if hit is None:
                continue
            name = hit[1]
            if pieces and pieces[-1][0] == name:
                pieces[-1] = (name, pieces[-1][1] + ch)
            else:
                pieces.append((name, ch))
        return pieces


def font_stack(
    primary: str | Path | None = None,
    bold: str | Path | None = None,
    symbol: str | Path | None = None,
) -> FontStack:
    primary_path = Path(primary) if primary else font_path()
    bold_path = Path(bold) if bold else bold_font_path()
    symbol_path = Path(symbol) if symbol else symbol_font_path()
    return FontStack(primary_path, bold_path, symbol_path)


def width(font: fitz.Font, text: str, size: float) -> float:
    return font.text_length(text, fontsize=size)


def char_width(font: fitz.Font, ch: str, size: float) -> float:
    return font.text_length(ch, fontsize=size)


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            if tokens and tokens[-1] != " ":
                tokens.append(" ")
            i += 1
            continue
        if ch.isascii() and ch in _WORD_CHARS:
            start = i
            i += 1
            while i < len(text) and text[i].isascii() and text[i] in _WORD_CHARS:
                i += 1
            tokens.append(text[start:i])
            continue
        tokens.append(ch)
        i += 1
    return tokens


def _atoms(tokens: list[str]) -> list[str]:
    """把禁则标点与相邻字符粘成不可拆分的原子，天然避免行首标点。"""
    atoms: list[str] = []
    pending = ""
    for token in tokens:
        if token == " ":
            if atoms and atoms[-1] != " ":
                atoms.append(" ")
            continue
        if token in _SCRIPT_CHARS and atoms:
            # 上/下标必须粘在前一个原子上（255² 不能被拆成 "255" + "²" 换行）。
            if atoms[-1] == " " and len(atoms) > 1:
                atoms.pop()
            atoms[-1] += token
            continue
        if token in NO_LINE_END:
            pending += token
            continue
        atom = pending + token
        pending = ""
        if token in NO_LINE_START and atoms:
            if atoms[-1] == " ":
                atoms.pop()
            if atoms:
                atoms[-1] += atom
            else:
                atoms.append(atom)
            continue
        if (
            re.fullmatch(r"\d+(?:[.,]\d+)*[。．、，,；;：:]", atom)
            and len(atoms) > 1
            and atoms[-1] == " "
        ):
            # 「…见算法 2。」：编号与句号粘成一体后，前面的空格仍是断行点，
            # 换行会让下一行以「2。」开头。把编号并进前一个原子。
            atoms.pop()
            atoms[-1] += atom
            continue
        atoms.append(atom)
    if pending:
        atoms.append(pending)
    return atoms


def wrap(
    text: str,
    metrics,
    size: float,
    content_width: float,
    first_indent: float = 0.0,
    bold: bool = False,
) -> list[str]:
    """按内容宽度断行；metrics 可以是 FontStack 或 fitz.Font。"""
    def measure(value: str) -> float:
        if hasattr(metrics, "measure"):
            return metrics.measure(value, size, bold)
        return width(metrics, value, size)

    lines: list[str] = []
    content_width = max(2.0, content_width)
    for raw in text.replace("\r", "").split("\n"):
        paragraph = " ".join(raw.split())
        if not paragraph:
            continue
        limit = max(2.0, content_width - first_indent)
        line = ""
        for atom in _atoms(_tokens(paragraph)):
            if atom == " ":
                if line and not line.endswith(" "):
                    line += " "
                continue
            if measure(line + atom) <= limit:
                line += atom
                continue
            if measure(atom) > content_width:
                for ch in atom:
                    if measure(line + ch) <= limit:
                        line += ch
                    else:
                        if line.strip():
                            lines.append(line.rstrip())
                        line, limit = ch, content_width
                continue
            if line.strip():
                lines.append(line.rstrip())
            line, limit = atom, content_width
        if line.strip():
            lines.append(line.rstrip())
    return _kinsoku(lines)


def _kinsoku(lines: list[str]) -> list[str]:
    """行首/行尾禁则：标点不落在行首，开括号不留在行尾。"""
    for index in range(len(lines) - 1):
        while lines[index] and lines[index + 1] and lines[index + 1][0] in NO_LINE_START:
            lines[index + 1] = lines[index][-1] + lines[index + 1]
            lines[index] = lines[index][:-1]
        while lines[index] and lines[index + 1] and lines[index][-1] in NO_LINE_END:
            lines[index + 1] = lines[index][-1] + lines[index + 1]
            lines[index] = lines[index][:-1]
    return [line for line in lines if line]
