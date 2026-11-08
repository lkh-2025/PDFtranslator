"""术语保护：占位符哨兵、用户词表、自动识别专有名词。

流水线：先把不该翻的内容替换成哨兵（如 ``@@T7@@``），翻译后按映射还原。
还原失败时整段回退原文，绝不把半成品写进 PDF。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SENTINEL = "@@T{index}@@"
SENTINEL_RE = re.compile(r"@@\s*T\s*(\d+)\s*@@", re.IGNORECASE)
# 模型偶尔输出零宽字符或全角符号，先归一化再匹配。
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
FULLWIDTH_SENTINEL_TRANS = str.maketrans({"＠": "@", "＃": "#", "Ｔ": "T", "ｔ": "t"})
# 兼容常见的占位符写法：模型可能把方括号吃掉或改成中文括号。
ALT_SENTINEL_RE = re.compile(
    r"\[\[\s*T\s*(\d+)\s*\]\]"
    r"|【\s*T\s*(\d+)\s*】"
    r"|⟦\s*T\s*(\d+)\s*⟧"
    r"|<<\s*T\s*(\d+)\s*>>"
    r"|［\s*T\s*(\d+)\s*］"
    r"|\"\s*T\s*(\d+)\s*\""
    r"|“\s*T\s*(\d+)\s*”"
    r"|\(\s*T\s*(\d+)\s*\)"
    # 备用哨兵 ZZT7ZZ 可能被模型写成 ZZM7ZZ（T 被替换成 M）。
    r"|(?<![A-Za-z0-9])ZZ\s*[MT]\s*(\d+)\s*ZZ(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 备用哨兵两侧的 Z 也可能被吃掉一个（ZT7ZZ、ZZT7Z）。
Z_FUZZY_SENTINEL_RE = re.compile(
    r"(?<![A-Za-z])Z{1,3}\s*[MT]\s*(\d+)\s*Z{1,3}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 中文书名号/六角括号/全角圆括号/花括号等变形。
BRACE_SENTINEL_RE = re.compile(
    r"〖\s*T\s*(\d+)\s*〗"
    r"|〔\s*T\s*(\d+)\s*〕"
    r"|（\s*T\s*(\d+)\s*）"
    r"|《\s*T\s*(\d+)\s*》"
    r"|{\s*T\s*(\d+)\s*}",
    re.IGNORECASE,
)
# 模型有时多吃/少吃一个 @（@@@T4@@、@@T11@），宽松匹配可救回。
LOOSE_SENTINEL_RE = re.compile(r"@+\s*T\s*(\d+)\s*@+", re.IGNORECASE)
# 变形更厉害：只有一侧 @ 或 #（#T2@@、@@T3@）。
FUZZY_SENTINEL_RE = re.compile(r"[@#]{1,3}\s*T\s*(\d+)\s*[@#]{1,3}", re.IGNORECASE)
# 只剩 "T2@@"（开头的 @@ 被吞）。
TRAILING_SENTINEL_RE = re.compile(r"(?<![A-Za-z0-9@#])T\s*(\d+)\s*@@", re.IGNORECASE)
# 模型可能在 @ 之间插空格（"@ @ T2@@"）。
SENTINEL_SPACE_RE = re.compile(r"@\s*@")
# 最后兜底：模型可能只留下 T7。只在主/备用写法都失败时启用。
BARE_SENTINEL_RE = re.compile(r"(?<![A-Za-z0-9])T\s*(\d+)(?![A-Za-z0-9])", re.IGNORECASE)

URL_EMAIL_RE = re.compile(
    r"(?:https?://|ftp://|www\.)\S+|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
CODE_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\(\)"
    r"|\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b"
    r"|\b[A-Za-z]+(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
)
MODEL_RE = re.compile(
    r"\b[A-Z]{2,8}[-\s]?\d{1,5}[A-Za-z0-9-]*\b"
    r"|\b[A-Za-z]{1,8}[/-]\d{1,5}(?:[A-Za-z0-9.-]*)\b"
    r"|\b\d{2,5}\s?(?:GB|MB|TB|KB|GHz|MHz|nm|mm|cm|kg|km)\b"
)
ACRONYM_RE = re.compile(r"\b[A-Z]{2,8}(?:[/-][A-Z0-9]{1,4})*\b")
# 含大写的连字符复合名（DeepCompress-ViT、GPT-4、COVID-19），整体保护，
# 避免被拆成两个相邻哨兵后模型把中间的 "-" 和哨兵一起吞掉。
HYPHEN_TERM_RE = re.compile(
    r"\b[A-Za-z0-9]*(?:[A-Z][a-z0-9]*|[A-Z]{2,})[A-Za-z0-9]*"
    r"(?:[-–][A-Za-z0-9]+)+\b"
)
CAMEL_RE = re.compile(
    r"\b(?:[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b"
)
CAP_PHRASE_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+){1,3}\b")
# 引用编号（含 11–13 这类范围）：整段保护，避免模型吞掉逗号/连接号。
CITATION_RE = re.compile(r"\[\d{1,3}(?:\s*(?:,|–|—|-)\s*\d{1,3})*\]")

ALL_CAPS_STOP = {
    "THE", "AND", "FOR", "WITH", "FROM", "THIS", "THAT", "ARE", "WAS",
    "WERE", "HAS", "HAVE", "NOT", "BUT", "ALL", "CAN", "OUR", "YOU", "ITS",
    "MAY", "NEW", "ONE", "TWO", "USE", "FIG", "TABLE", "PAGE", "NOTE",
    "DATA", "TOTAL", "SOURCE", "ABSTRACT", "INTRODUCTION", "CONCLUSION",
    "APPENDIX", "REPORT", "OVERVIEW", "SUMMARY", "CONTENTS", "CHAPTER",
}

BRANDS = (
    "OpenAI", "Anthropic", "Microsoft", "Google", "Apple", "Amazon", "Meta",
    "Nvidia", "NVIDIA", "Intel", "AMD", "GitHub", "GitLab", "Docker",
    "Kubernetes", "PyTorch", "TensorFlow", "MATLAB", "Photoshop", "Windows",
    "Linux", "Android", "iOS", "macOS", "YouTube", "LinkedIn", "Twitter",
    "Facebook", "Instagram", "Office", "Excel", "Word", "PowerPoint",
)

_BRAND_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in sorted(BRANDS, key=len, reverse=True)) + r")\b"
)


@dataclass
class TermEntry:
    """一条术语映射；target 为空表示保留原文。"""

    source: str
    target: str = ""
    kind: str = "glossary"
    source_url: str = ""
    confidence: float = 1.0


@dataclass
class TermRecord:
    term: str
    kind: str
    disposition: str
    target: str = ""
    source_url: str = ""
    confidence: float = 1.0


@dataclass
class ProtectedText:
    source: str
    masked: str
    mapping: dict[int, str]
    records: list[TermRecord] = field(default_factory=list)

    @property
    def expected(self) -> set[int]:
        return set(self.mapping)

    def has_translatable_content(self) -> bool:
        """去掉哨兵后，是否还剩需要交给模型的英文/中文内容。"""
        stripped = SENTINEL_RE.sub("", self.masked)
        return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", stripped))


class Glossary:
    """用户词表：原文<TAB>译法；译法留空表示保留原文。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: list[TermEntry] = []
        if self.path and self.path.is_file():
            self.entries = self._load(self.path)

    @staticmethod
    def _load(path: Path) -> list[TermEntry]:
        entries: list[TermEntry] = []
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "\t" in line:
                source, target = line.split("\t", 1)
            else:
                parts = re.split(r"\s{2,}", line, maxsplit=1)
                if len(parts) != 2:
                    continue
                source, target = parts
            source = source.strip()
            target = target.strip()
            if source:
                entries.append(TermEntry(source=source, target=target, kind="glossary"))
        # 长词优先，避免 "API" 抢先匹配 "API Gateway"。
        entries.sort(key=lambda item: len(item.source), reverse=True)
        return entries

    @property
    def sources(self) -> set[str]:
        return {entry.source.strip().lower() for entry in self.entries}


class TermProtector:
    """把术语替换为哨兵，翻译后还原。"""

    def __init__(
        self,
        glossary_entries: list[TermEntry] | None = None,
        resolved_entries: list[TermEntry] | None = None,
        keep_terms: str = "auto",
    ) -> None:
        self.glossary = list(glossary_entries or [])
        self.resolved = list(resolved_entries or [])
        self.keep_terms = (keep_terms or "auto").lower()
        self.records: list[TermRecord] = []

    def _entry_spans(self, text: str) -> list[_Span]:
        spans: list[_Span] = []
        for entry, priority in [(e, 200) for e in self.glossary] + [
            (e, 190) for e in self.resolved
        ]:
            pattern = re.compile(
                r"(?<![A-Za-z0-9_])" + re.escape(entry.source) + r"(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
            for match in pattern.finditer(text):
                replacement = entry.target or match.group(0) or entry.source
                disposition = "采用词表译名" if entry.target else "保留原文（词表）"
                if entry.kind == "resolved":
                    disposition = "采用检索译名" if entry.target else "保留原文（检索）"
                spans.append(
                    _Span(
                        start=match.start(),
                        end=match.end(),
                        priority=priority,
                        replacement=replacement,
                        record=TermRecord(
                            term=match.group(0),
                            kind=entry.kind,
                            disposition=disposition,
                            target=entry.target,
                            source_url=entry.source_url,
                            confidence=entry.confidence,
                        ),
                    )
                )
        return spans

    def _auto_spans(self, text: str) -> list[_Span]:
        spans: list[_Span] = []

        def add(pattern: re.Pattern[str], priority: int, disposition: str = "保留原文（术语保护）") -> None:
            for match in pattern.finditer(text):
                value = match.group(0)
                spans.append(
                    _Span(
                        start=match.start(),
                        end=match.end(),
                        priority=priority,
                        replacement=value,
                        record=TermRecord(term=value, kind="auto", disposition=disposition),
                    )
                )

        add(URL_EMAIL_RE, 300)
        add(CITATION_RE, 290)
        add(CODE_RE, 260)

        if self.keep_terms == "off":
            return spans

        add(MODEL_RE, 160)
        add(HYPHEN_TERM_RE, 150)
        add(_BRAND_RE, 120)
        add(CAMEL_RE, 110)
        add(ACRONYM_RE, 100)
        if self.keep_terms == "all":
            add(CAP_PHRASE_RE, 50)
        return spans

    def protect(self, text: str) -> ProtectedText:
        spans = self._entry_spans(text) + self._auto_spans(text)
        if not spans:
            return ProtectedText(source=text, masked=text, mapping={}, records=[])

        spans.sort(key=lambda span: (-span.priority, -(span.end - span.start), span.start))
        accepted: list[_Span] = []
        occupied: list[tuple[int, int]] = []
        for span in spans:
            if not span.is_valid(text):
                continue
            if any(not (span.end <= left or span.start >= right) for left, right in occupied):
                continue
            if span.record and span.record.kind == "auto":
                upper = span.record.term.upper()
                if upper in ALL_CAPS_STOP and span.record.term.isupper():
                    continue
            accepted.append(span)
            occupied.append((span.start, span.end))

        accepted.sort(key=lambda span: span.start)
        parts: list[str] = []
        mapping: dict[int, str] = {}
        records: list[TermRecord] = []
        last = 0
        for index, span in enumerate(accepted, start=1):
            parts.append(text[last : span.start])
            parts.append(SENTINEL.format(index=index))
            mapping[index] = span.replacement
            if span.record:
                records.append(span.record)
            last = span.end
        parts.append(text[last:])
        return ProtectedText(
            source=text, masked="".join(parts), mapping=mapping, records=records
        )

    @staticmethod
    def restore(protected: ProtectedText, translated: str) -> tuple[str, bool]:
        """返回 (还原结果, 是否成功)。失败时调用方应整段回退原文。"""
        if not protected.mapping:
            return translated, True

        mapping = protected.mapping
        found: set[int] = set()

        def replace(match: re.Match[str]) -> str:
            raw = next((group for group in match.groups() if group), None)
            if raw is None:
                return match.group(0)
            index = int(raw)
            if index not in mapping:
                return match.group(0)
            found.add(index)
            return mapping[index]

        text = ZERO_WIDTH_RE.sub("", translated).translate(FULLWIDTH_SENTINEL_TRANS)
        # 先合并 "@ @ T1@@" 这类被拆开的哨兵，避免只吃掉后半截、留下孤立 @。
        text = SENTINEL_SPACE_RE.sub("@@", text)
        text = LOOSE_SENTINEL_RE.sub(replace, text)
        text = SENTINEL_RE.sub(replace, text)
        text = ALT_SENTINEL_RE.sub(replace, text)
        text = Z_FUZZY_SENTINEL_RE.sub(replace, text)
        text = BRACE_SENTINEL_RE.sub(replace, text)
        text = FUZZY_SENTINEL_RE.sub(replace, text)
        text = TRAILING_SENTINEL_RE.sub(replace, text)
        text = SENTINEL_RE.sub(replace, text)
        missing = protected.expected - found
        if missing:
            text = BARE_SENTINEL_RE.sub(replace, text)
            missing = protected.expected - found
        if missing:
            # 模型有时不吐哨兵，而是直接把术语原词（或词表译名）写进译文；
            # 只要替换文本已经出现在译文里，就等价于还原成功。
            haystack = re.sub(r"\s+", "", text).lower()
            missing = {
                index
                for index in missing
                if re.sub(r"\s+", "", mapping[index]).lower() not in haystack
            }
        if missing:
            return protected.source, False
        return text, True


@dataclass
class _Span:
    start: int
    end: int
    priority: int
    replacement: str
    record: TermRecord | None = None

    def is_valid(self, text: str) -> bool:
        return 0 <= self.start < self.end <= len(text) and bool(text[self.start : self.end].strip())
