"""专有名词联网检索：中文维基 → Wikidata → 可选 LLM 确认。

只在必要时查询，只发送词条本身，超时/断网静默降级；结果写入可人工编辑的
``terms_cache.json``，用户 glossary.tsv 的优先级永远更高。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

from .terms import (
    ACRONYM_RE,
    ALL_CAPS_STOP,
    BRANDS,
    CAMEL_RE,
    CAP_PHRASE_RE,
    CODE_RE,
    MODEL_RE,
    URL_EMAIL_RE,
)

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
SINGLE_CAP_RE = re.compile(r"\b[A-Z][a-z0-9]{2,}\b")

WIKI_API = "https://zh.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# 常见技术缩写：直接保留原文，不占用查询预算。
COMMON_ACRONYMS = {
    "API", "HTTP", "HTTPS", "CPU", "GPU", "RAM", "ROM", "URL", "URI",
    "HTML", "CSS", "JSON", "XML", "SQL", "PDF", "USB", "AI", "ML", "NLP",
    "LLM", "OS", "UI", "UX", "ID", "OK", "FAQ", "CEO", "CFO", "CTO", "IT",
    "PC", "MB", "GB", "TB", "KB", "MS", "DNS", "TCP", "UDP", "IP", "VPN",
    "SSL", "TLS", "FTP", "SMTP", "IMAP", "IDE", "SDK", "CLI", "GUI", "ORM",
    "JWT", "REST", "CRUD", "CI", "CD", "AWS", "GCP", "DOM", "SEO", "PDFA",
}

COMMON_WORDS = frozenset(
    """
    the a an and or of to in on for with from by as at is are was were be been
    being this that these those it its he she they we you i our your their has
    have had do does did can could should would may might will not no yes also
    more most less least new old first last one two three four five six seven
    eight nine ten year years page pages table tables figure figures section
    sections report reports data total source note notes abstract introduction
    conclusion conclusions appendix summary overview contents chapter company
    companies business market markets product products service services customer
    customers revenue growth increase decrease percent percentage however
    therefore moreover furthermore although because while when where which who
    whom whose what how why all any both each every some such other another
    same different many much few several public private internal external
    annual financial fiscal quarter quarterly driven strong performance demand
    investment technology operations efficiency outlook remain positive risk
    risks include includes supply chain currency regulation based using used
    use high low large small good better best key main major minor
    revenue revenues sales sale market markets marketing finance team teams
    income cost costs operating breakdown reference prepared segment segments
    net gross margin cloud platform platforms partner partners retention
    infrastructure region regional organization share shares earnings profit
    profits expense expenses cash flow balance sheet statement statements
    asset assets liability liabilities equity budget forecast guidance target
    targets metric metrics unit units average rate rates price prices pricing
    volume overview abstract introduction appendix summary contents chapter
    q1 q2 q3 q4
    """.split()
)

CANDIDATE_PATTERNS = (
    ("model", MODEL_RE),
    ("acronym", ACRONYM_RE),
    ("camel", CAMEL_RE),
    ("phrase", CAP_PHRASE_RE),
)

# 短语开头的冠词/介词/代词不属于专名本身。
LEADING_STOP = {
    "the", "a", "an", "this", "that", "these", "those", "our", "their",
    "its", "his", "her", "in", "on", "at", "for", "from", "with", "by",
    "of", "and", "or", "to", "as", "we", "you", "they", "it",
}

_KIND_PRIORITY = {"model": 5, "acronym": 4, "camel": 3, "brand": 3, "phrase": 2, "single": 1}


@dataclass
class TermCandidate:
    term: str
    count: int
    kind: str
    priority: int


@dataclass
class ResolvedTerm:
    term: str
    decision: str  # translate / keep
    target: str
    source: str
    confidence: float
    timestamp: str
    origin: str  # wikipedia / wikidata / llm / cache / not_found / disabled / quota
    count: int = 1


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def _norm(term: str) -> str:
    return " ".join(term.split()).lower()


class TermResolver:
    def __init__(
        self,
        cache_path: Path | str | None = None,
        enabled: bool = True,
        max_queries: int = 35,
        timeout: float = 6.0,
        probe_timeout: float = 8.0,
        wiki_api: str = WIKI_API,
        wikidata_api: str = WIKIDATA_API,
        proxy: str | None = None,
        llm_confirmer: Callable[[str], ResolvedTerm | None] | None = None,
        http_get: Callable[..., dict | None] | None = None,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.enabled = enabled
        self.max_queries = max(0, max_queries)
        self.timeout = max(0.5, timeout)
        self.probe_timeout = max(self.timeout, probe_timeout)
        self.wiki_api = wiki_api
        self.wikidata_api = wikidata_api
        self.proxy = (proxy or "").strip() or None
        self.llm_confirmer = llm_confirmer
        self._http_get = http_get or self._default_get
        self._cache: dict[str, dict] = {}
        self._cache_loaded = False
        self.queries_used = 0
        self.network_errors = 0
        self._network_down = False
        self._probed = False
        self.active_endpoint = ""

    # ---------- 候选筛选 ----------

    @staticmethod
    def _clean(text: str) -> str:
        text = URL_EMAIL_RE.sub(" ", text)
        text = CODE_RE.sub(" ", text)
        text = re.sub(r"\S+=\S+", " ", text)  # 公式片段
        return text

    def collect_candidates(self, texts: list[str]) -> list[TermCandidate]:
        counts: dict[str, TermCandidate] = {}

        def add(term: str, kind: str) -> None:
            term = " ".join(term.split())
            if kind == "phrase":
                words = term.split()
                while words and words[0].lower() in LEADING_STOP:
                    words = words[1:]
                term = " ".join(words)
                if len(words) <= 1:
                    kind = "single"
            if not term or len(term) < 2 or len(term) > 60:
                return
            if _has_cjk(term):
                return
            key = _norm(term)
            if key in COMMON_ACRONYMS or key in COMMON_WORDS:
                return
            priority = _KIND_PRIORITY.get(kind, 1)
            if key in counts:
                counts[key].count += 1
                if priority > counts[key].priority:
                    counts[key].priority = priority
                    counts[key].kind = kind
                    counts[key].term = term
            else:
                counts[key] = TermCandidate(term=term, count=1, kind=kind, priority=priority)

        for text in texts:
            cleaned = self._clean(text)
            for kind, pattern in CANDIDATE_PATTERNS:
                for match in pattern.finditer(cleaned):
                    term = match.group(0).strip()
                    if kind == "acronym" and term.upper() in ALL_CAPS_STOP:
                        continue
                    if kind == "phrase" and all(
                        word.lower() in COMMON_WORDS for word in term.split()
                    ):
                        continue
                    add(term, kind)

            for name in BRANDS:
                if re.search(r"\b" + re.escape(name) + r"\b", cleaned):
                    add(name, "brand")

            for match in SINGLE_CAP_RE.finditer(cleaned):
                term = match.group(0)
                if term.lower() in COMMON_WORDS:
                    continue
                before = cleaned[max(0, match.start() - 2) : match.start()]
                sentence_start = match.start() == 0 or before in {". ", "! ", "? ", "\n"}
                if sentence_start:
                    continue
                add(term, "single")


        # 已被较长词组覆盖的单词不再单独查询，避免 "Machine Learning" 又查
        # "Machine" 和 "Learning"；型号里的 "RTX" 也不再单独查。
        phrase_tokens: set[str] = set()
        model_tokens: set[str] = set()
        for item in counts.values():
            if item.kind == "phrase" and len(item.term.split()) > 1:
                phrase_tokens.update(word.lower() for word in item.term.split())
            if item.kind == "model":
                model_tokens.update(
                    part.lower() for part in re.split(r"[/\s-]", item.term) if part
                )
        for key in list(counts):
            item = counts[key]
            if item.kind in ("single", "acronym") and (
                _norm(item.term) in phrase_tokens or _norm(item.term) in model_tokens
            ):
                del counts[key]

        candidates = list(counts.values())
        # 单次出现的普通首字母大写词优先级最低，避免挤占查询预算。
        candidates.sort(key=lambda item: (-item.priority, -item.count, item.term.lower()))
        return candidates[:80]

    # ---------- 缓存 ----------

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        if not self.cache_path or not self.cache_path.is_file():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
            terms = raw.get("terms", raw) if isinstance(raw, dict) else {}
            if isinstance(terms, dict):
                self._cache = {
                    str(key).lower(): value
                    for key, value in terms.items()
                    if isinstance(value, dict)
                }
        except (OSError, ValueError, TypeError):
            self._cache = {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated": _now(),
            "note": "可人工编辑；用户 glossary.tsv 优先级更高",
            "terms": self._cache,
        }
        temp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.cache_path)

    # ---------- 查询 ----------

    def _default_get(self, url: str, params: dict, timeout: float | None = None) -> dict | None:
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}
        response = requests.get(
            url,
            params=params,
            timeout=timeout or self.timeout,
            proxies=proxies,
            headers={"User-Agent": "PDFTranslator/1.0 (offline term resolver)"},
        )
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None

    def _call(self, url: str, params: dict, timeout: float) -> dict | None:
        try:
            return self._http_get(url, params, timeout)
        except TypeError:
            # 兼容只接受 (url, params) 的自定义 http_get（测试/注入用）。
            return self._http_get(url, params)

    def _probe(self) -> None:
        """首个请求用较长超时探测；一旦失败就认为整条链路不可用，避免卡住翻译。"""
        if self._probed or not self.enabled:
            return
        self._probed = True
        for api in (self.wiki_api, self.wikidata_api):
            try:
                self._call(
                    api,
                    {"action": "query", "format": "json", "meta": "siteinfo"},
                    self.probe_timeout,
                )
                self.active_endpoint = api
                return
            except Exception:
                self.network_errors += 1
        self._network_down = True

    def _get(self, url: str, params: dict) -> dict | None:
        """所有网络读取都经过这里；任何异常都静默降级。"""
        if self._network_down:
            return None
        try:
            return self._call(url, params, self.timeout)
        except Exception:
            self.network_errors += 1
            self._network_down = True
            return None

    def _lookup_wikipedia(self, term: str) -> ResolvedTerm | None:
        exact = self._get(
            self.wiki_api,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
                "titles": term,
                "prop": "pageprops",
                "ppprop": "wikibase_item",
            },
        )
        if exact:
            pages = exact.get("query", {}).get("pages", [])
            if isinstance(pages, dict):
                pages = list(pages.values())
            if pages:
                page = pages[0]
                title = str(page.get("title", "")).strip()
                if title and not page.get("missing") and not page.get("invalid"):
                    if _has_cjk(title):
                        return ResolvedTerm(
                            term=term,
                            decision="translate",
                            target=title,
                            source="https://zh.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                            confidence=0.8,
                            timestamp=_now(),
                            origin="wikipedia",
                        )
                    if _norm(title) == _norm(term):
                        return ResolvedTerm(
                            term=term,
                            decision="keep",
                            target="",
                            source="https://zh.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                            confidence=0.75,
                            timestamp=_now(),
                            origin="wikipedia",
                        )

        for query in (f'intitle:"{term}"', term):
            data = self._get(
                self.wiki_api,
                {
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": "5",
                    "srprop": "snippet",
                },
            )
            results = (data or {}).get("query", {}).get("search", [])
            for item in results:
                title = str(item.get("title", "")).strip()
                if title and _has_cjk(title):
                    return ResolvedTerm(
                        term=term,
                        decision="translate",
                        target=title,
                        source="https://zh.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                        confidence=0.55,
                        timestamp=_now(),
                        origin="wikipedia",
                    )
                if title and _norm(title) == _norm(term):
                    return ResolvedTerm(
                        term=term,
                        decision="keep",
                        target="",
                        source="https://zh.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                        confidence=0.6,
                        timestamp=_now(),
                        origin="wikipedia",
                    )
        return None

    def _lookup_wikidata(self, term: str) -> ResolvedTerm | None:
        search = self._get(
            self.wikidata_api,
            {
                "action": "wbsearchentities",
                "search": term,
                "language": "en",
                "uselang": "zh",
                "format": "json",
                "limit": "3",
            },
        )
        results = (search or {}).get("search", [])
        if not results:
            return None
        qid = results[0].get("id")
        if not qid:
            return None
        entity = self._get(
            self.wikidata_api,
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "labels|descriptions",
                "languages": "zh|zh-hans|en",
                "format": "json",
            },
        )
        data = (entity or {}).get("entities", {}).get(qid, {})
        labels = data.get("labels", {})
        label = (
            (labels.get("zh-hans") or {}).get("value")
            or (labels.get("zh") or {}).get("value")
            or (labels.get("en") or {}).get("value")
        )
        if not label:
            return None
        url = f"https://www.wikidata.org/wiki/{qid}"
        if _norm(label) == _norm(term):
            return ResolvedTerm(
                term=term,
                decision="keep",
                target="",
                source=url,
                confidence=0.5,
                timestamp=_now(),
                origin="wikidata",
            )
        if _has_cjk(label):
            return ResolvedTerm(
                term=term,
                decision="translate",
                target=str(label),
                source=url,
                confidence=0.5,
                timestamp=_now(),
                origin="wikidata",
            )
        return None

    def _lookup(self, term: str) -> ResolvedTerm:
        if self._network_down:
            return ResolvedTerm(
                term=term,
                decision="keep",
                target="",
                source="",
                confidence=0.0,
                timestamp=_now(),
                origin="network",
            )
        result = self._lookup_wikipedia(term)
        if result:
            return result
        result = self._lookup_wikidata(term)
        if result:
            return result
        if self.llm_confirmer:
            try:
                result = self.llm_confirmer(term)
            except Exception:
                result = None
            if result:
                return result
        return ResolvedTerm(
            term=term,
            decision="keep",
            target="",
            source="",
            confidence=0.0,
            timestamp=_now(),
            origin="not_found",
        )

    def resolve(
        self, candidates: list[TermCandidate], skip_terms: set[str] | None = None
    ) -> list[ResolvedTerm]:
        self._load_cache()
        if self.enabled and candidates:
            # 首个请求用 8s 探测，成功后再回到 3s，避免每个词都卡满超时。
            self._probe()
        skip = {item.lower() for item in (skip_terms or set())}
        resolved: list[ResolvedTerm] = []
        for candidate in candidates:
            key = _norm(candidate.term)
            if key in skip:
                continue
            cached = self._cache.get(key)
            if cached:
                resolved.append(
                    ResolvedTerm(
                        term=candidate.term,
                        decision=str(cached.get("decision", "keep")),
                        target=str(cached.get("target", "")),
                        source=str(cached.get("source", "")),
                        confidence=float(cached.get("confidence", 0.5) or 0.5),
                        timestamp=str(cached.get("timestamp", "")),
                        origin="cache",
                        count=candidate.count,
                    )
                )
                continue

            if not self.enabled:
                resolved.append(
                    ResolvedTerm(
                        term=candidate.term,
                        decision="keep",
                        target="",
                        source="",
                        confidence=0.0,
                        timestamp=_now(),
                        origin="disabled",
                        count=candidate.count,
                    )
                )
                continue
            if self.queries_used >= self.max_queries:
                resolved.append(
                    ResolvedTerm(
                        term=candidate.term,
                        decision="keep",
                        target="",
                        source="",
                        confidence=0.0,
                        timestamp=_now(),
                        origin="quota",
                        count=candidate.count,
                    )
                )
                continue
            if self._network_down:
                resolved.append(
                    ResolvedTerm(
                        term=candidate.term,
                        decision="keep",
                        target="",
                        source="",
                        confidence=0.0,
                        timestamp=_now(),
                        origin="network",
                        count=candidate.count,
                    )
                )
                continue

            self.queries_used += 1
            result = self._lookup(candidate.term)
            result.count = candidate.count
            self._cache[key] = {
                "term": candidate.term,
                "decision": result.decision,
                "target": result.target,
                "source": result.source,
                "confidence": result.confidence,
                "timestamp": result.timestamp,
                "origin": result.origin,
            }
            resolved.append(result)

        if self.enabled and self.queries_used:
            self._save_cache()
        return resolved


class LLMConfirmer:
    """可选的 LLM 兜底确认；只在显式开启且提供 API Key 时使用，只发送词条。"""

    PROMPT = (
        "你是术语编辑。用户给出一个英文专有名词，请判断它在简体中文技术文档中"
        "是否应翻译。只输出 JSON："
        '{"decision":"translate"或"keep","translation":"中文译名或空字符串"}。'
        "不确定时返回 keep。"
    )

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 8.0,
    ) -> None:
        from .openai_compat import normalize_base_url

        self.api_key = api_key.strip()
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.timeout = timeout

    def confirm(self, term: str) -> ResolvedTerm | None:
        if not self.api_key or not self.base_url:
            return None
        try:
            response = requests.post(
                self.base_url + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "temperature": 0,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": self.PROMPT},
                        {"role": "user", "content": term},
                    ],
                },
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                return None
            content = response.json()["choices"][0]["message"]["content"]
            content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", str(content).strip())
            data = json.loads(content)
        except Exception:
            return None

        decision = str(data.get("decision", "keep")).lower()
        target = str(data.get("translation", "") or "").strip()
        if decision == "translate" and target:
            return ResolvedTerm(
                term=term,
                decision="translate",
                target=target,
                source="llm",
                confidence=0.6,
                timestamp=_now(),
                origin="llm",
            )
        return ResolvedTerm(
            term=term,
            decision="keep",
            target="",
            source="llm",
            confidence=0.5,
            timestamp=_now(),
            origin="llm",
        )
