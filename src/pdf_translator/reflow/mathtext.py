"""数学片段归一化：把模型残留的 LaTeX 命令降级成可排版的纯文本。

原则：正文里不允许出现反斜杠命令。能安全降级的（希腊字母、\hat、上下标、
\frac）就地转成 Unicode；无法降级的（矩阵、环境）整段丢弃命令、只留内容；
所有 `\\(`/`\\)`/`$` 定界符一律去掉。排版阶段与自检共用同一套规则。
"""

from __future__ import annotations

import re

# 单个命令 → Unicode 字符
SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "nabla": "∇", "partial": "∂", "sum": "∑", "prod": "∏", "int": "∫",
    "infty": "∞", "cdot": "·", "times": "×", "div": "÷", "pm": "±",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "in": "∈", "notin": "∉", "subset": "⊂",
    "cup": "∪", "cap": "∩", "to": "→", "rightarrow": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "Leftrightarrow": "⇔", "top": "ᵀ", "perp": "⊥",
    "ldots": "…", "dots": "…", "cdots": "⋯", "prime": "′",
    "because": "∵", "therefore": "∴", "forall": "∀", "exists": "∃",
    "lVert": "‖", "rVert": "‖", "Vert": "‖", "vert": "|",
}
# 修饰类命令：\hat{x} → x̂
ACCENTS = {
    "hat": "\u0302", "bar": "\u0304", "tilde": "\u0303", "vec": "\u20d7",
    "dot": "\u0307", "ddot": "\u0308",
}
SUBSCRIPT = dict(
    zip(
        "0123456789+-=()aehijklmnoprstuvxβγρφχ",
        "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵪ",
    )
)
SUPERSCRIPT = dict(
    zip(
        "0123456789+-=()nistuvwxyzαβγδθφχ",
        "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˢᵗᵘᵛʷˣʸᶻᵅᵝᵞᵟᶿᵠᵡ",
    )
)
BLACKBOARD = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "E": "𝔼"}

_SPACE_CMDS = ("quad", "qquad", "enspace", "thinspace")
_DROP_CMDS = {"left", "right", "big", "Big", "bigg", "Bigg", "displaystyle",
              "textstyle", "limits", "nolimits", "nonumber", "notag", "hline"}
_ENV_RE = re.compile(r"\\(?:begin|end)\{[^}]*\}")
_WRAPPER_RE = re.compile(r"\\(?:text|mathrm|mathbf|mathit|mathcal|operatorname)\{([^{}]*)\}")
_MATHBB_RE = re.compile(r"\\mathbb\{([^{}]*)\}")
_FRAC_RE = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\{([^{}]*)\}")
_ACCENT_RE = re.compile(r"\\(hat|bar|tilde|vec|dot|ddot)\s*\{?([A-Za-z0-9])\}?")
# 抽取时重音符号常落在基准字母前面（"˜E"），渲染出来是错位的。只在重音
# 前面不是字母时才搬动，保证重复调用幂等（"Ẽv" 不会被再搬一次）。
_LETTER_BEFORE = r"(?<![A-Za-z\u00c0-\u024f\u0370-\u03ff])"
_LEAD_ACCENT_RE = re.compile(_LETTER_BEFORE + r"([\u0300-\u036f\u20d7])([A-Za-z\u0370-\u03ff])")
# 单独抽出来的「修饰符字母」版本（˜ ˆ ¨ ´）要先换成组合重音再移到字母后面。
_SPACING_ACCENT_RE = re.compile(
    _LETTER_BEFORE + r"([\u02c6\u02dc\u00a8\u00b4\u02d8\u02c7])([A-Za-z\u0370-\u03ff])"
)
_SPACING_TO_COMBINING = {
    "\u02c6": "\u0302",
    "\u02dc": "\u0303",
    "\u00a8": "\u0308",
    "\u00b4": "\u0301",
    "\u02d8": "\u0306",
    "\u02c7": "\u030c",
}


def _fix_leading_accents(text: str) -> str:
    # 先处理本来就带组合重音的 "̃E"，再处理“修饰符字母”版 "˜E"；
    # 顺序反了会把刚移到字母后的组合符号又被搬走一次。
    value = _LEAD_ACCENT_RE.sub(r"\2\1", text or "")
    return _SPACING_ACCENT_RE.sub(
        lambda m: m.group(2) + _SPACING_TO_COMBINING[m.group(1)], value
    )
_SCRIPT_RE = re.compile(r"([_^])\s*\{([^{}]*)\}")
_SCRIPT_ONE_RE = re.compile(r"([_^])\s*([A-Za-z0-9])")
_CMD_RE = re.compile(r"\\([A-Za-z]+)")
_LATEX_RE = re.compile(r"\\[A-Za-z]{2,}|\\[()\[\]]|\$[^$]{1,60}\$|\\[{}]")
# 源 PDF 的数学字体常把括号/根号等拆成私用区码点（U+F8EE 一类），
# 控制字符则来自 Type3/CID 字体的分离字形。两者都无法排版，直接剔除。
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ufffd]")


def has_latex(text: str) -> bool:
    """是否残留 LaTeX 命令/定界符（自检用）。"""
    return bool(_LATEX_RE.search(text or ""))


def sanitize_glyphs(text: str) -> str:
    """剔除私用区字形与控制字符；用于把源 PDF 的字体编码噪声挡在文档模型之外。"""
    if not text:
        return text
    value = _PUA_RE.sub("", text)
    value = _CTRL_RE.sub("", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


_NUMBERING_RE = re.compile(r"^(\s*\d{1,2}(?:\.\d{1,2})*)\s*。\s*")


def normalize_numbering(text: str) -> str:
    """标题编号里的全角句号恢复成半角点：“2。 相关工作” → “2. 相关工作”。"""
    if not text:
        return text
    return _NUMBERING_RE.sub(r"\1. ", text)


def _scripts(text: str) -> str:
    keep = "\uE000"

    def multi(match: re.Match) -> str:
        kind, body = match.group(1), match.group(2)
        table = SUBSCRIPT if kind == "_" else SUPERSCRIPT
        if body and all(ch in table for ch in body):
            return "".join(table[ch] for ch in body)
        if len(body) == 1 and body in table:
            return table[body]
        # 多字符且无法整体降级（source、R,t）：保留下划线但去掉花括号，
        # 用哨兵字符挡住后面的单字符规则，避免 "p_source" 变成 "pₛource"。
        return f"{keep}{kind}{body}{keep}"

    text = _SCRIPT_RE.sub(multi, text)
    text = _SCRIPT_ONE_RE.sub(
        lambda m: (SUBSCRIPT if m.group(1) == "_" else SUPERSCRIPT).get(
            m.group(2), m.group(0)
        ),
        text,
    )
    return text.replace(keep, "")


def _delimiters(text: str) -> str:
    """去掉 \( \) \[ \]（含被全角化后的 \（ \））与 $ 定界符。"""
    text = re.sub(r"\\\s*[（(]", "", text)
    text = re.sub(r"\\\s*[）)]", "", text)
    text = re.sub(r"\\\s*[\[\]]", "", text)
    return text.replace("$", "")


def normalize_math(text: str) -> str:
    """把 LaTeX 片段降级为纯文本；幂等，重复调用不会继续变化。"""
    if not text or ("\\" not in text and "$" not in text):
        # 没有 LaTeX 命令时也要处理「˜E」这类抽取结果：PDF 文字层把重音符号
        # 单独抽成一个前置字符，显示出来像 "˜Ev"，把重音移到基准字母后面才对。
        return _fix_leading_accents(text or "")
    value = text
    value = _ENV_RE.sub("", value)
    value = _delimiters(value)
    value = _FRAC_RE.sub(r"\1/\2", value)
    value = _SQRT_RE.sub(r"√\1", value)
    value = _MATHBB_RE.sub(lambda m: "".join(BLACKBOARD.get(ch, ch) for ch in m.group(1)), value)
    value = _WRAPPER_RE.sub(r"\1", value)
    value = _ACCENT_RE.sub(lambda m: m.group(2) + ACCENTS[m.group(1)], value)
    for command in _SPACE_CMDS:
        value = re.sub(r"\\" + command + r"\b", " ", value)
    for command in _DROP_CMDS:
        value = re.sub(r"\\" + command + r"\b", "", value)
    value = value.replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
    value = value.replace("\\ ", " ").replace("\\|", "‖").replace("\\{", "{").replace("\\}", "}")

    def command(match: re.Match) -> str:
        name = match.group(1)
        return SYMBOLS.get(name, "")

    value = _CMD_RE.sub(command, value)
    value = value.replace("^ᵀ", "ᵀ")
    value = _scripts(value)
    value = _fix_leading_accents(value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\s+([，。、；：！？）】》」』])", r"\1", value)
    return value.strip()
