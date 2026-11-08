"""reflow 五阶段的独立执行器。

① structure  解析元素树            → structure.json
② glossary   全文术语抽取与合并    → glossary.json
③ translate  按段落重写（可续跑）  → translation.json
④ layout     按样式表生成中文 PDF  → layout.json + out/results/<doc>_zh.pdf
⑤ check      自检通读              → selfcheck.json

每个阶段只读上游 JSON，不共享内存状态，因此可以单独重跑。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import fitz

from ..paths import config_file
from ..providers.postprocess import (
    collapse_tail_repeat,
    drop_lead_in_tail,
    fix_numbering_dots,
    strip_instruction_echo,
    strip_term_echo,
)
from .check import check_output
from .mathtext import normalize_math, normalize_numbering
from .structure import Document, Element, build_document
from .typeset import render
from .workspace import Workspace

LETTER_RE = re.compile(r"[A-Za-z]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
TRANSLATE_KINDS = {"title", "subtitle", "heading", "body", "caption", "footnote"}
# 只从正文类元素抽术语：页眉页脚、图内刻度、参考文献、代码不参与，
# 否则 "Page"、页码、作者名会污染候选表。
TERM_KINDS = {"title", "subtitle", "heading", "body", "caption", "footnote", "table"}
ABBREV_RE = re.compile(
    r"\b(?:[A-Z]{2,8}s?|[A-Z][a-z]{0,6}[A-Z][A-Za-z0-9]*|[A-Za-z]{2,}-?\d+[A-Za-z0-9]*)\b"
)
PHRASE_RE = re.compile(r"\b[A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,4}\b")
STOP_PHRASES = {
    "The", "This", "These", "Those", "Our", "We", "In", "On", "For", "With",
    "Figure", "Table", "Section", "Algorithm", "Eq", "Proceedings", "Conference",
    "However", "Moreover", "Furthermore", "Additionally", "Therefore", "Although",
    "Since", "Because", "While", "When", "Note", "First", "Second", "Third",
}
MIN_TERM_COUNT = 2
# 这几个缩写在中文里有通行译名（美元/英镑式表达、欧盟、联合国），
# 强行要求原样保留只会造出“四舍五入至 nearest thousand US dollars”这种半句英文。
KEEP_AS_IS_SKIP = {"US", "UK", "EU", "UN"}
STAGE_LABELS = {
    "structure": "解析文档结构",
    "glossary": "抽取全文术语",
    "translate": "翻译段落",
    "layout": "生成中文版式",
    "check": "自检产物",
}


# ---------- ① 结构 ----------

def run_structure(src: str | Path, workspace: Workspace | None = None) -> Document:
    workspace = workspace or Workspace.for_source(src)
    started = time.time()
    pdf = fitz.open(str(workspace.source))
    try:
        document = build_document(pdf)
    finally:
        pdf.close()

    image_dir = workspace.root / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    for element in document.elements:
        for node in element.walk():
            if not node.image or node.kind not in {"figure", "equation", "table"}:
                continue
            target = image_dir / f"{node.id or 'figure'}.img"
            target.write_bytes(node.image)

    payload = document.to_dict()
    _attach_image_paths(payload, "images")
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload["elapsed"] = round(time.time() - started, 2)
    payload["low_confidence"] = [
        {
            "id": node.id,
            "kind": node.kind,
            "page": node.page + 1,
            "text": node.text[:80],
            "confidence": round(node.confidence, 2),
            "notes": node.confidence_notes,
        }
        for node in document.low_confidence()
    ]
    workspace.save("structure", payload)
    return document


def _attach_image_paths(payload: dict, folder: str) -> None:
    for element in payload.get("elements", []):
        for node in [element] + element.get("children", []):
            if node.get("kind") in ("figure", "equation", "table") and node.get("image_size"):
                node["image"] = f"{folder}/{node.get('id') or 'figure'}.img"


def load_document(workspace: Workspace) -> Document | None:
    """从 structure.json 还原元素树（图片按相对路径回读）。"""
    payload = workspace.load("structure")
    if not payload:
        return None
    document = Document(
        source=payload.get("source", str(workspace.source)),
        page_size=tuple(payload.get("page_size", (595.28, 841.89))),
        elements=[],
        body_size=float(payload.get("document", {}).get("body_size", 10.5)),
        dropped_headers=int(payload.get("document", {}).get("dropped_headers", 0)),
        page_columns=[page.get("columns", 1) for page in payload.get("pages", [])],
        page_gutters=[
            [tuple(gutter) for gutter in page.get("gutters", [])]
            for page in payload.get("pages", [])
        ],
        degraded=payload.get("document", {}).get("degraded", ""),
    )
    document.elements = [
        _element_from_dict(item, workspace.root) for item in payload.get("elements", [])
    ]
    return document


def _element_from_dict(data: dict, root: Path) -> Element:
    element = Element(
        kind=data.get("kind", "body"),
        text=data.get("text", ""),
        page=max(0, int(data.get("page", 1)) - 1),
        bbox=tuple(data.get("bbox", (0, 0, 0, 0))),
        size=float(data.get("size", 10.5)),
        bold=bool(data.get("bold", False)),
        level=int(data.get("level", 0)),
        label=data.get("label", ""),
        rows=[list(row) for row in data.get("rows", [])],
        image_size=tuple(data.get("image_size", (0, 0))),
        skip=bool(data.get("skip", False)),
        translatable=bool(data.get("translate", True)),
        id=data.get("id", ""),
        order=int(data.get("order", 0)),
        column=int(data.get("column", 0)),
        confidence=float(data.get("confidence", 1.0)),
        confidence_notes=list(data.get("confidence_notes", [])),
    )
    image = data.get("image")
    if image:
        target = root / image
        if target.is_file():
            element.image = target.read_bytes()
    element.children = [_element_from_dict(child, root) for child in data.get("children", [])]
    return element


# ---------- ② 术语 ----------

def _is_abbreviation(term: str) -> bool:
    """判断是否为缩写/型号（CNN、ViT、GPT-4、DeepCompress）；普通首字母大写词不算。"""
    if len(term.split()) > 1:
        return False
    if re.fullmatch(r"[A-Z]{2,8}s?", term):
        return True
    if any(ch.isdigit() for ch in term):
        return True
    return sum(1 for ch in term if ch.isupper()) >= 2


def _iter_term_texts(payload: dict):
    """按元素产出 (文本, 页码, 元素类型)，表格逐单元格而不是拼成一整行。"""
    for element in payload.get("elements", []):
        kind = element.get("kind", "")
        if kind not in TERM_KINDS:
            continue
        page = element.get("page", 1)
        if kind == "table":
            for row in element.get("rows", []):
                for cell in row:
                    text = " ".join(str(cell).split())
                    if text:
                        yield text, page, kind
            continue
        text = " ".join(str(element.get("text", "")).split())
        if text:
            yield text, page, kind


def _contains_phrase(longer: str, shorter: str) -> bool:
    """shorter 是否以词边界形式出现在 longer 中。"""
    start = 0
    while True:
        index = longer.find(shorter, start)
        if index < 0:
            return False
        before = longer[index - 1] if index else " "
        tail = index + len(shorter)
        after = longer[tail] if tail < len(longer) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        start = index + 1


def _drop_subphrases(counts: dict[str, dict], user_lower: set[str]) -> None:
    """丢掉被更长短语包含的碎片（"Optimized Test" ⊂ "Optimized Test-Time Decoding"）。

    只处理多词短语；TFLOP/TFLOPs 这类单 token 保留，否则会误删真实缩写。
    """
    terms = list(counts)
    dropped: list[str] = []
    for short in terms:
        if len(short.split()) < 2 or short.lower() in user_lower:
            continue
        low = short.lower()
        if any(
            len(long) > len(short) and _contains_phrase(long.lower(), low)
            for long in terms
            if long != short
        ):
            dropped.append(short)
    for term in dropped:
        counts.pop(term, None)


def run_glossary(workspace: Workspace) -> dict:
    payload = workspace.load("structure")
    if not payload:
        run_structure(workspace.source, workspace)
        payload = workspace.load("structure") or {}

    user_terms, duplicates = _read_user_glossary()
    user_lower = {term.lower() for term in user_terms}
    user_patterns = {}
    for term in user_terms:
        # 词表按词条单词形式书写（visual token），原文里常是复数（visual tokens）。
        suffix = "" if term.endswith(("s", "S")) else r"(?:s|es)?"
        user_patterns[term] = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(term) + suffix + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
    counts: dict[str, dict] = {}

    def add(term: str, page: int, kind: str, times: int = 1) -> None:
        entry = counts.setdefault(term, {"count": 0, "pages": set(), "kinds": set()})
        entry["count"] += times
        entry["pages"].add(page)
        entry["kinds"].add(kind)

    for text, page, kind in _iter_term_texts(payload):
        # 词表里的术语可能是全小写（inference/quantization），必须单独扫，
        # 不能只靠大写正则，否则会被误判成“词表未使用”。
        for term, pattern in user_patterns.items():
            hits = len(pattern.findall(text))
            if hits:
                add(term, page, kind, hits)
        for match in ABBREV_RE.finditer(text):
            term = match.group(0)
            if term.lower() in user_lower:
                continue
            add(term, page, kind)
        for match in PHRASE_RE.finditer(text):
            phrase = match.group(0)
            if phrase.split()[0] in STOP_PHRASES or phrase.lower() in user_lower:
                continue
            add(phrase, page, kind)

    _drop_subphrases(counts, user_lower)

    entries: list[dict] = []
    conflicts: list[dict] = []
    for term, info in sorted(counts.items(), key=lambda item: (-item[1]["count"], item[0])):
        hit = next((key for key in user_terms if key.lower() == term.lower()), None)
        if info["count"] < MIN_TERM_COUNT and not hit:
            continue
        if hit:
            target = user_terms[hit]
            if target:
                decision, basis = "translate", "词表命中"
                if _is_abbreviation(term):
                    conflicts.append(
                        {
                            "term": term,
                            "auto": "keep",
                            "glossary": target,
                            "reason": "缩写默认保留原文与词表译名冲突，按词表执行",
                        }
                    )
            else:
                target, decision, basis = term, "keep", "词表指定保留原文"
            origin = "user"
        elif _is_abbreviation(term):
            target, decision, basis, origin = term, "keep", "缩写/型号，默认保留原文", "auto"
        else:
            target, decision, basis, origin = (
                term,
                "candidate",
                "未命中词表，建议保留原文，待上下文确认",
                "auto",
            )
        entries.append(
            {
                "term": term,
                "count": info["count"],
                "pages": sorted(info["pages"]),
                "kinds": sorted(info["kinds"]),
                "target": target,
                "decision": decision,
                "basis": basis,
                "source": origin,
            }
        )

    used = {entry["term"].lower() for entry in entries}
    unused = [term for term in user_terms if term.lower() not in used]
    result = {
        "source": payload.get("source", str(workspace.source)),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "min_count": MIN_TERM_COUNT,
        "entries": entries,
        "user_terms": len(user_terms),
        "unused_user_terms": unused,
        "conflicts": conflicts + duplicates,
        "counts": {
            "keep": sum(1 for e in entries if e["decision"] == "keep"),
            "translate": sum(1 for e in entries if e["decision"] == "translate"),
            "candidate": sum(1 for e in entries if e["decision"] == "candidate"),
            "conflicts": len(conflicts) + len(duplicates),
        },
    }
    workspace.save("glossary", result)
    return result


def _read_user_glossary() -> tuple[dict[str, str], list[dict]]:
    """读 config/glossary.tsv（两列：原文<TAB>译文），返回词表与重复译名冲突。"""
    path = config_file("glossary.tsv")
    result: dict[str, str] = {}
    duplicates: list[dict] = []
    if not path.is_file():
        return result, duplicates
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        source, target = parts[0].strip(), parts[1].strip()
        if not source:
            continue
        if source in result and result[source] != target:
            duplicates.append(
                {
                    "term": source,
                    "auto": "keep",
                    "glossary": target,
                    "reason": f"词表内重复条目：已有译名「{result[source]}」，后一条被忽略",
                }
            )
            continue
        result[source] = target
    return result, duplicates


# ---------- ③ 翻译 ----------

def run_translate(workspace: Workspace, provider, progress=None) -> dict:
    structure = workspace.load("structure")
    if not structure:
        run_structure(workspace.source, workspace)
        structure = workspace.load("structure")
    document = load_document(workspace)
    if not structure or document is None:
        raise RuntimeError("缺少 structure.json，请先运行 --stage structure")
    glossary = workspace.load("glossary") or run_glossary(workspace)
    hints, must_keep = _hints_from_glossary(glossary)

    previous = workspace.load("translate") or {"entries": []}
    # 缓存按内容指纹索引：结构重跑导致 id 位移时，译文仍能复用。
    cache = {
        item["sha1"]: item
        for item in previous.get("entries", [])
        if item.get("status") == "ok" and item.get("translation") and item.get("sha1")
    }

    model = getattr(provider, "describe", lambda: "unknown")()
    entries: list[dict] = list(cache.values())
    _translate_tables(document, provider, entries, hints, must_keep, model)

    items = _translatable_items(document)
    summary = _summary(document)
    table_headers = _table_headers(document)
    pending: list[tuple[dict, Element]] = []
    for index, element in enumerate(items):
        source_text = _source_text(element)
        digest = hashlib.sha1(source_text.encode("utf-8")).hexdigest()[:12]
        cached = cache.get(digest)
        if cached and _cache_truncated(cached.get("translation", ""), source_text):
            # 旧缓存里的截断译文不会被任何校验覆盖（缓存命中直接复用），
            # 必须在这里判废重译，否则“摘要写到一半没了”会永久固化。
            cached = None
        if cached:
            cached["id"] = element.id
            cached["page"] = element.page + 1
            cached["kind"] = element.kind
            element.translation = cached["translation"]
            continue
        if _url_only(source_text):
            # 纯链接脚注没有可译内容，模型只会截断/改写它，直接保留原文。
            element.translation = source_text
            entries[:] = _replace_entry(
                entries,
                {
                    "id": element.id,
                    "kind": element.kind,
                    "page": element.page + 1,
                    "source": source_text,
                    "sha1": digest,
                    "translation": source_text,
                    "status": "ok",
                    "model": model,
                    "terms": [],
                    "note": "含链接，保留原文",
                    "context": {"summary": "", "previous": "", "next": "", "table_header": ""},
                },
            )
            continue
        prev_text = _source_text(items[index - 1]) if index else ""
        next_text = _source_text(items[index + 1]) if index + 1 < len(items) else ""
        # 短碎片（不到 8 个词）不给上下文：模型看到下一段会顺手把它也译进来，
        # 排版后同一段内容出现两次。
        if len(source_text.split()) < 8:
            prev_text = next_text = ""
        record = {
            "id": element.id,
            "kind": element.kind,
            "page": element.page + 1,
            "source": source_text,
            "sha1": digest,
            "translation": "",
            "status": "pending",
            "model": model,
            "terms": _terms_in(source_text, hints, must_keep),
            "context": {
                "summary": summary,
                "previous": prev_text[:400],
                "next": next_text[:400],
                "table_header": table_headers.get(element.id, ""),
            },
        }
        pending.append((record, element))

    if progress:
        progress(0, len(pending), f"待翻译 {len(pending)} 段，已缓存 {len(cache)} 段")

    batch_size = 4
    done = 0

    def _payload(record: dict) -> dict:
        return {
            "text": record["source"],
            "hints": [(term, hints[term]) for term in record["terms"] if term in hints],
            "must_keep": [term for term in record["terms"] if term in must_keep],
            "context": "\n".join(
                part
                for part in (
                    record["context"]["summary"],
                    "该表列名：" + record["context"]["table_header"]
                    if record["context"]["table_header"]
                    else "",
                    "上一段：" + record["context"]["previous"],
                    "下一段：" + record["context"]["next"],
                )
                if part
            ),
        }

    def _translate(payload: list[dict]) -> list[str]:
        if hasattr(provider, "translate_segments"):
            return provider.translate_segments(payload)
        return provider.translate([item["text"] for item in payload])

    for start in range(0, len(pending), batch_size):
        group = pending[start : start + batch_size]
        payload = [_payload(record) for record, _element in group]
        try:
            outputs = _translate(payload)
        except Exception as exc:  # 单批失败不阻塞整篇
            for record, _element in group:
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                entries = _replace_entry(entries, record)
            workspace.save("translate", _translate_payload(workspace, entries))
            continue
        for (record, element), value in zip(group, outputs):
            record["translation"] = collapse_tail_repeat(
                drop_lead_in_tail(
                    strip_instruction_echo(
                        strip_term_echo(normalize_math((value or "").strip()), record["terms"])
                    )
                )
            )
            record["translation"] = _trim_heading(
                record["kind"], record["source"], record["translation"]
            )
            if not record["translation"]:
                record["status"] = "failed"
            elif _looks_truncated(record["source"], record["translation"]):
                record["status"] = "short"
                record["note"] = "译文明显短于原文，疑似截断，重跑该段"
            else:
                record["status"] = "ok"
            element.translation = record["translation"]
            entries = _replace_entry(entries, record)

        # 截断/失败的段重译一次：输出上限是本次生成的硬约束，换个批次重跑通常能补全。
        short = [
            (record, element)
            for record, element in group
            if record["status"] in ("short", "failed")
        ]
        for record, element in short:
            try:
                retried = _translate([_payload(record)])[0]
            except Exception as exc:  # 重译失败保留原文，状态仍是 short
                record.setdefault("errors", []).append(f"{type(exc).__name__}: {exc}")
                continue
            value = _trim_heading(
                record["kind"],
                record["source"],
                collapse_tail_repeat(
                    drop_lead_in_tail(
                        strip_instruction_echo(
                            strip_term_echo(
                                normalize_math((retried or "").strip()), record["terms"]
                            )
                        )
                    )
                ),
            )
            if value and (
                not _looks_truncated(record["source"], value)
                or len(value) > len(record["translation"])
            ):
                record["translation"] = value
                record["status"] = "ok" if not _looks_truncated(record["source"], value) else "short"
                record["note"] = "" if record["status"] == "ok" else record.get("note", "")
                element.translation = value
                entries = _replace_entry(entries, record)

        done += len(group)
        workspace.save("translate", _translate_payload(workspace, entries))
        if progress:
            progress(done, len(pending), f"已重写 {done}/{len(pending)} 段")

    entries = [entry for entry in entries if entry.get("id")]
    entries = _enforce_terms(workspace, document, entries, glossary, _translate, _payload)
    # describe() 在模型加载前只会返回“尚未加载模型”，落盘前用它刷新真实模型名。
    final_model = getattr(provider, "describe", lambda: model)()
    if final_model:
        for entry in entries:
            entry["model"] = final_model
    _apply_table_translations(document, entries)
    payload = _translate_payload(workspace, entries)
    workspace.save("translate", payload)
    return payload


def _looks_truncated(source: str, translated: str) -> bool:
    """译文中文量明显少于原文长度时保守标记（不阻塞，留给阶段⑤复核）。"""
    if len(source) < 40:
        return False
    expected = len(source) / 6.0
    return len(CJK_RE.findall(translated)) < max(4.0, expected * 0.5)


def _table_headers(document: Document) -> dict[str, str]:
    """表题元素 → 所属表格的列名，供图注/表题翻译时补语境。"""
    result: dict[str, str] = {}
    for element in document.elements:
        if element.kind != "table" or not element.rows:
            continue
        header = " | ".join(str(cell) for cell in element.rows[0])[:200]
        for child in element.children:
            if child.kind == "caption" and child.id:
                result[child.id] = header
    return result


def _translate_tables(
    document: Document,
    provider,
    entries: list[dict],
    hints: dict[str, str],
    must_keep: set[str],
    model: str,
) -> None:
    """表格整表批量翻译：单元格命中术语时注入提示，失败只标该表。"""
    for element in document.elements:
        if element.kind != "table" or not element.rows:
            continue
        if element.image:
            # 行列抽取不可靠的宽表在排版阶段按原版截图贴入，单元格译文根本不会出现，
            # 这里直接跳过，避免每轮重跑都把几十个碎片单元格喂给模型。
            from .typeset import _table_rows_usable

            if not _table_rows_usable(element.rows):
                continue
        existing = next((entry for entry in entries if entry.get("id") == element.id), None)
        if existing and existing.get("rows"):
            element.rows = [list(row) for row in existing["rows"]]
            continue
        targets: list[tuple[int, int, str]] = []
        for row_index, row in enumerate(element.rows):
            for column_index, cell in enumerate(row):
                # 数值表里的单元格是「0.94」「VTW [21]」这种碎片，派给模型既慢又没用
                # （40 分钟才译完两张表），只译真正成句的单元格。
                text = (cell or "").strip()
                if len(text) < 24 or len(text.split()) < 4:
                    continue
                if LETTER_RE.search(text):
                    targets.append((row_index, column_index, text))
        if not targets:
            continue
        targets = targets[:12]
        # 单元格单看没有语境（Segment 会被译成"段落"、Q1 被写成 01），
        # 把表题与表头一起给它，编号与业务术语才译得对。
        caption = next(
            (child.text for child in element.children if child.kind == "caption"),
            element.label,
        )
        header = " | ".join(element.rows[0]) if element.rows else ""
        table_context = (
            f"表格单元格。表题：{caption or '无'}。表头：{header[:200]}。"
            "保持数字、编号与单位不变"
        )
        payload = [
            {
                "text": cell,
                "hints": [(term, hints[term]) for term in hints if term in cell],
                "must_keep": [term for term in must_keep if term in cell],
                "context": table_context,
            }
            for _r, _c, cell in targets
        ]
        try:
            if hasattr(provider, "translate_segments"):
                outputs = provider.translate_segments(payload)
            else:
                outputs = provider.translate([item["text"] for item in payload])
            for (row_index, column_index, _), value in zip(targets, outputs):
                element.rows[row_index][column_index] = normalize_math((value or "").strip())
            status, error = "ok", ""
        except Exception as exc:  # 单表失败不阻塞整篇
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        record = {
            "id": element.id,
            "kind": "table",
            "page": element.page + 1,
            "source": " | ".join(cell for _r, _c, cell in targets)[:400],
            "sha1": hashlib.sha1(
                " | ".join(cell for _r, _c, cell in targets).encode("utf-8")
            ).hexdigest()[:12],
            "translation": "",
            "rows": [list(row) for row in element.rows],
            "status": status,
            "model": model,
            "terms": [],
            "context": {"summary": "", "previous": "", "next": ""},
        }
        if error:
            record["error"] = error
        entries[:] = _replace_entry(entries, record)


def _translate_payload(workspace: Workspace, entries: list[dict]) -> dict:
    return {
        "source": str(workspace.source),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": entries[0].get("model", "") if entries else "",
        "entries": entries,
        "failed": [entry["id"] for entry in entries if entry.get("status") == "failed"],
    }


def _replace_entry(entries: list[dict], record: dict) -> list[dict]:
    for index, entry in enumerate(entries):
        if entry.get("id") == record.get("id"):
            entries[index] = record
            return entries
    entries.append(record)
    return entries


def _translatable_items(document: Document) -> list[Element]:
    result: list[Element] = []
    for element in document.elements:
        if element.kind in TRANSLATE_KINDS and element.translatable and element.text.strip():
            result.append(element)
        for child in element.children:
            if child.kind in TRANSLATE_KINDS and child.translatable and child.text.strip():
                result.append(child)
    return result


def _summary(document: Document) -> str:
    parts = [document.title] if document.title else []
    for element in document.elements:
        if element.kind == "body" and element.text.strip():
            parts.append(element.text)
            if len(" ".join(parts)) > 600:
                break
    return " ".join(parts)[:600]


def _source_text(element: Element) -> str:
    if element.kind == "caption":
        from .structure import split_caption

        _label, body = split_caption(element.text)
        return body or element.text
    return element.text


HEADING_KINDS = frozenset({"title", "heading", "subtitle"})
_NUMBERED_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+[A-Za-z\u4e00-\u9fff]")
UNTRANSLATABLE_KINDS = frozenset({"figure", "equation", "table", "header", "footer", "page_number"})

_TERM_PATTERNS: dict[str, re.Pattern[str]] = {}


def _term_pattern(term: str) -> re.Pattern[str]:
    """大小写不敏感、ASCII 词边界对齐的术语匹配器。

    图注这类短行常以 ``Proposed Token Pruning Method`` 的大写形式出现，
    原来的 ``term in source`` 严格按大小写比对，导致沿用整篇词条全部落空。
    """
    pattern = _TERM_PATTERNS.get(term)
    if pattern is None:
        body = r"[\s\-]+".join(re.escape(word) for word in term.split())
        if term[:1].isalnum():
            body = r"(?<![A-Za-z0-9])" + body
        if term[-1:].isalnum():
            # 英文复数与缩写复数是同一术语（visual tokens / visual token、LMMs / LMM）。
            body += r"(?:s|es)?(?![A-Za-z0-9])"
        pattern = re.compile(body, re.IGNORECASE)
        _TERM_PATTERNS[term] = pattern
    return pattern


def _terms_in(text: str, hints: dict[str, str], must_keep: set[str]) -> list[str]:
    found: list[str] = []
    for term in list(hints) + list(must_keep):
        if term in found:
            continue
        if _term_pattern(term).search(text):
            found.append(term)
    return found


def _trim_heading(kind: str, source: str, translation: str) -> str:
    """标题只保留标题本身。

    短标题配上【上下文】后，模型常顺手把下一段正文也译出来（同一段译文里
    出现「4.3. 图像-语言理解」+ 整段摘要），排版后与真正的正文重复。这里
    按换行与长度上限截回标题。图注同样会被模型续写，只做换行截断。
    """
    if kind not in HEADING_KINDS and kind != "caption":
        # 「4.4.2. Weight decay」这类小标题常被结构层判成 body，
        # 但它的样子就是编号标题：照样按标题截断。
        if not (kind == "body" and _NUMBERED_HEADING_RE.match(source or "")
                and len(source.strip()) <= 60):
            return translation
    if not translation:
        return translation
    value = translation.strip()
    for marker in ("\n\n", "\n"):
        if marker in value:
            value = value.split(marker, 1)[0].strip()
    if _NUMBERED_HEADING_RE.match(source or ""):
        # 模型把下一段直接接在标题后面，中间用两个空格隔开。
        value = re.split(r"\s{2,}", value)[0].strip()
    if kind == "caption":
        return value
    limit = max(30, int(len(source) * 1.8) + 10)
    if len(value) > limit:
        head = value[:limit]
        cut = max(head.rfind("。"), head.rfind("；"), head.rfind("，"))
        value = head[: cut + 1].strip() if cut > 8 else head.strip()
    return value


def _hints_from_glossary(glossary: dict) -> tuple[dict[str, str], set[str]]:
    hints: dict[str, str] = {}
    must_keep: set[str] = set()
    for entry in glossary.get("entries", []):
        if entry.get("decision") == "translate" and entry.get("target"):
            hints[entry["term"]] = entry["target"]
        elif entry.get("decision") == "keep":
            if entry["term"] not in KEEP_AS_IS_SKIP:
                must_keep.add(entry["term"])
    # 单复数在词表里是两条（visual token / visual tokens），提示词里重复一遍
    # 只会挤占上下文，合并成一条单词形式。
    merged: dict[str, str] = {}
    for term, target in sorted(hints.items(), key=lambda item: -len(item[0])):
        base = term
        if base.endswith(("s", "es")) and base[:-1] in hints and hints[base[:-1]] == target:
            base = base[:-1]
        merged.setdefault(base, target)
    return merged, must_keep


def _apply_translations(document: Document, translation: dict) -> int:
    """把 translation.json 的译文与表格行灌回元素树（排版与自检共用）。

    结构一旦重跑，元素 id 会整体位移；因此不能只按 id 取译文，必须校验
    sha1（内容指纹），不一致时按指纹回查，否则会把别段译文套到本段上。
    返回「该译却没译」的节点数，供排版统计与自检报噪。
    """
    by_id = {entry.get("id"): entry for entry in translation.get("entries", [])}
    by_hash = {
        entry.get("sha1"): entry
        for entry in translation.get("entries", [])
        if entry.get("sha1") and entry.get("status") == "ok"
    }
    # 图注的 sha1 会随「图 1」这类标签的切分规则变化，指纹对不上时再按原文
    # 全等回查一次，避免整段图注退回英文。
    by_text = {
        (entry.get("source") or "").strip(): entry
        for entry in translation.get("entries", [])
        if (entry.get("source") or "").strip() and entry.get("translation")
    }
    untranslated = 0
    for element in document.elements:
        # 行内子节点没有独立译文（跟着父元素走），只统计顶层元素。
        for node in [element] + element.children:
            is_toplevel = node is element
            # 不可译的块（参考文献、图内短标注、代码）保留原文：旧缓存里可能还留着
            # 它们早先的译文，不能照搬。
            if node.kind in TRANSLATE_KINDS and not node.translatable:
                continue
            digest = _node_digest(node)
            entry = by_id.get(node.id)
            if entry and entry.get("sha1") != digest:
                entry = None
            if entry is None:
                entry = by_hash.get(digest)
            if entry is None:
                entry = by_text.get(_source_text(node).strip())
            if entry and entry.get("translation"):
                # 旧缓存里可能仍有 LaTeX 残留与句末术语清单，排版前统一清理，
                # 避免反斜杠或“代码可在此获取：A、B、C”进正文。
                value = normalize_math(entry["translation"])
                value = collapse_tail_repeat(
                    drop_lead_in_tail(strip_instruction_echo(strip_term_echo(value)))
                )
                value = _trim_heading(node.kind, _source_text(node), value)
                # 编号后的半角点被全角化成 “。” 不只出现在标题里：正文列表项
                # （“4。我们使用 ReduceLROnPlateau”）同样需要复原。
                value = fix_numbering_dots(normalize_numbering(value))
                node.translation = value
            elif is_toplevel and node.id in by_id and node.text.strip():
                # 阶段③ 认为这段该译（translation.json 里有记录）却没拿到译文。
                untranslated += 1
            rows = (entry or {}).get("rows")
            if rows:
                node.rows = [[normalize_math(str(cell)) for cell in row] for row in rows]
    return untranslated


def _cache_truncated(translation: str, source: str) -> bool:
    """命中缓存的译文是否明显被截断。

    原文以句末标点收尾、译文却停在中途，且中文字数不到原文的 1/3，
    说明生成时被输出上限切断，必须重译。
    """
    value = (translation or "").strip()
    if len(source) < 120:
        return False
    if not value:
        return True
    # 模型对作者/单位这类“不翻译”的段落会原样回抄；回抄被截断时译文就是
    # 原文的严格前缀（如 "@Purdue.edu" 整段丢失），与句末标点无关。
    if len(value) < len(source) and source.startswith(value):
        return True
    if source.rstrip()[-1:] not in ".!?。！？":
        return False
    if value[-1] in "。！？…”’）)!?.:：":
        return False
    # 原文以句末标点收尾、译文却停在连接词/逗号/连字符/拉丁字母上，
    # 说明生成被切断（如 “对于‘Standard’和”“…il-”），不能只看字数比例。
    if value[-1] in "和与及、，；,;（(“‘—-" or re.match(r"[A-Za-z0-9]", value[-1]):
        return True
    cjk = len(CJK_RE.findall(value))
    return cjk < len(source) / 3


def _node_digest(node: Element) -> str:
    if node.kind == "table":
        cells = [str(cell) for row in node.rows for cell in row]
        return hashlib.sha1(" | ".join(cells).encode("utf-8")).hexdigest()[:12]
    return hashlib.sha1(_source_text(node).encode("utf-8")).hexdigest()[:12]


def _apply_table_translations(document: Document, entries: list[dict]) -> None:
    for item in entries:
        if item.get("kind") != "table":
            continue
        for element in document.elements:
            if element.id == item.get("id"):
                element.rows = [list(row) for row in item.get("rows", element.rows)]


# ---------- ④ 排版 ----------

def run_layout(workspace: Workspace, font_file: str | Path | None = None) -> dict:
    document = load_document(workspace)
    if document is None:
        raise RuntimeError("缺少 structure.json，请先运行 --stage structure")
    translation = workspace.load("translate")
    if not translation:
        raise RuntimeError("缺少 translation.json，请先运行 --stage translate")
    untranslated = _apply_translations(document, translation)

    output = workspace.output_pdf
    started = time.time()
    stats = render(document, output, font_file)
    payload = {
        "source": document.source,
        "output": str(output),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pages": stats.pages,
        "lines": stats.lines,
        "columns": document.columns,
        "degraded": document.degraded,
        "body_size": document.body_size,
        "elapsed": round(time.time() - started, 2),
        "missing_glyphs": stats.missing or [],
        "untranslated": untranslated,
        "style_counts": _style_counts(document),
    }
    workspace.save("layout", payload)
    return payload


def _style_counts(document: Document) -> dict[str, int]:
    counts: dict[str, int] = {}
    for element in document.elements:
        for node in element.walk():
            style = node.kind if node.kind != "heading" else f"heading{min(3, node.level or 1)}"
            counts[style] = counts.get(style, 0) + 1
    return counts


# ---------- ⑤ 自检 ----------

def run_check(workspace: Workspace) -> dict:
    layout = workspace.load("layout")
    if not layout:
        layout = run_layout(workspace)
    output = Path(layout["output"])
    if not output.is_file():
        raise RuntimeError(f"产物不存在：{output}")
    document = load_document(workspace) or Document("", (595.28, 841.89), [])
    translation = workspace.load("translate") or {}
    _apply_translations(document, translation)
    glossary = workspace.load("glossary") or {}
    missed = _missed(document)
    check = check_output(workspace.source, output, missed=missed)
    check["readability"] = _readability(document)
    check["consistency"] = _consistency(document, glossary)
    check["numbers"] = _numbers(document)
    for label, items in (
        ("术语未按词表落译", check["consistency"]["term_misses"]),
        ("数字/编号丢失", check["numbers"]["number_loss"]),
    ):
        if items:
            check["warnings"].append(f"{label} {len(items)} 处")
    untranslated = int(layout.get("untranslated") or 0)
    if untranslated:
        check["warnings"].append(f"未匹配到译文、回退显示原文 {untranslated} 段")
    workspace.save("check", check)
    return check


NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_SCALED_NUMBER_RE = re.compile(
    r"(?<![\d.])(\d+(?:,\d{3})*(?:\.\d+)?)(?:\s*(million|billion|thousand|万|亿|千))?",
    re.IGNORECASE,
)
_NUMBER_UNITS = {
    "thousand": 1e3,
    "million": 1e6,
    "billion": 1e9,
    "千": 1e3,
    "万": 1e4,
    "亿": 1e8,
}


_SCRIPT_DIGITS = str.maketrans("₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹", "01234567890123456789")


def _number_raw(text: str) -> list[tuple[str, float]]:
    """把文本里的数字连同量级词一起折算成数值：“1.2 million” 与 “120 万” 都等于 1.2e6。"""
    pairs: list[tuple[str, float]] = []
    for raw, unit in _SCALED_NUMBER_RE.findall(text):
        try:
            base = float(raw.replace(",", ""))
        except ValueError:
            continue
        pairs.append((raw, base * _NUMBER_UNITS.get((unit or "").lower(), 1.0)))
    return pairs


def _number_values(text: str) -> set[float]:
    # 上标/下标数字（xᵢ₊₁）折回 ASCII，否则 “1” 会被当成丢失
    text = text.translate(_SCRIPT_DIGITS)
    return {round(value, 6) for _, value in _number_raw(text)}
_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+\.\S+")


def _url_only(text: str) -> bool:
    """整段基本是链接（脚注里的仓库地址列表），没有可译的文字。"""
    urls = _URL_RE.findall(text or "")
    if len(urls) < 2:
        return False
    rest = _URL_RE.sub(" ", text or "")
    return len(re.findall(r"[A-Za-z]{2,}", rest)) <= 4
# 抽取时上标脚注编号会粘在括号/字母后（`[22]1`、`7B3`），排版后这个 1 自然
# 消失，属于抽取噪声而不是译文丢数字。
_GLUED_DIGIT_RE = re.compile(r"[\]\)）】]\d")


def _standalone_number(number: str, source: str) -> bool:
    """这个数字在原文里是否作为独立数值出现过（不是 `[22]1` 这类粘连的脚注号）。"""
    pattern = rf"(?<![A-Za-z0-9\]\)）】]){re.escape(number)}(?![\d.A-Za-z])"
    return re.search(pattern, source) is not None


_TARGET_VARIANTS = {
    "大语言模型": ("大型语言模型",),
    "大型语言模型": ("大语言模型",),
    "消融实验": ("消融研究", "消融分析"),
    "消融研究": ("消融实验", "消融分析"),
    "消融分析": ("消融实验", "消融研究"),
}


def _target_present(target: str, translation: str) -> bool:
    """词表译名是否真的落到了译文里。

    词表里的译名常带主语或定语形式（`proposed → 本文提出的`），直接做子串
    比对会把“提出了几种技术”判成没按词表。这里只放宽到“去掉本文/我们与句末的”，
    术语本体仍必须原样出现；少数两种写法都通行的译名（大/大型语言模型、消融实验/
    消融研究）用 `_TARGET_VARIANTS` 互认，避免为此白跑一次重译。
    """
    if not target:
        return True
    if target in translation:
        return True
    for variant in _TARGET_VARIANTS.get(target, ()):
        if variant in translation:
            return True
    core = target.strip()
    for prefix in ("本文提出的", "本文提出", "本文的", "本文", "我们", "本"):
        if core.startswith(prefix):
            core = core[len(prefix) :]
            break
    core = core.strip().rstrip("的")
    if len(core) < 2:
        return True
    return core in translation


def _missing_terms(text: str, translation: str, glossary: dict) -> list[tuple[str, str]]:
    """段里出现了词表要求译出的术语，但译文没用既定译名。"""
    missed: list[tuple[str, str]] = []
    source = text or ""
    for entry in glossary.get("entries", []):
        term = entry.get("term") or ""
        target = entry.get("target") or ""
        if entry.get("decision") != "translate" or not term or not target:
            continue
        if not any("\u4e00" <= char <= "\u9fff" for char in target):
            continue
        if not _term_pattern(term).search(source):
            continue
        if _target_present(target, translation or ""):
            continue
        missed.append((term, target))
    return missed


_MAX_TERM_RETRY = 40


def _enforce_terms(workspace, document, entries, glossary, translate, payload_for) -> list:
    """把漏译的术语补回来：命中词表却没按译名输出的段落单独重译一次。

    只重译「有既定中文译名、原文确实出现、译文里没有」的段，最多 40 段，
    重译后仍不达标就保留原样（阶段⑤ 会把它记为 warning，不会假装通过）。
    """
    targets = {entry.get("id"): entry for entry in entries}
    fixed = 0
    for element in document.elements:
        if fixed >= _MAX_TERM_RETRY:
            break
        if element.kind not in TRANSLATE_KINDS:
            continue
        entry = targets.get(element.id)
        if not entry or entry.get("status") != "ok":
            continue
        translation = entry.get("translation") or ""
        if not translation:
            continue
        source = entry.get("source") or _source_text(element)
        missing = _missing_terms(source, translation, glossary)
        if not missing:
            continue
        payload = dict(payload_for(entry))
        requirement = "【硬性要求】译文中必须逐字使用下列术语：" + "；".join(
            f"{term} → {target}" for term, target in missing
        )
        payload["context"] = f"{requirement}\n{payload.get('context') or ''}".strip()
        try:
            retried = translate([payload])[0]
        except Exception as exc:
            entry.setdefault("errors", []).append(f"{type(exc).__name__}: {exc}")
            continue
        value = _trim_heading(
            entry.get("kind") or element.kind,
            source,
            collapse_tail_repeat(
                drop_lead_in_tail(
                    strip_instruction_echo(
                        strip_term_echo(
                            normalize_math((retried or "").strip()), entry.get("terms") or []
                        )
                    )
                )
            ),
        )
        if not value or _looks_truncated(source, value):
            continue
        if len([1 for _t, t in missing if not _target_present(t, value)]) >= len(missing):
            continue
        entry["translation"] = value
        entry["note"] = "术语强制重译"
        element.translation = value
        fixed += 1
        workspace.save("translate", _translate_payload(workspace, entries))
    if fixed:
        print(f"术语强制重译：{fixed} 段", file=sys.stderr)
    return entries


def _consistency(document: Document, glossary: dict) -> dict:
    """术语一致性：词表要求译出的词，在含该词的段落里是否真的出现了既定译名。"""
    terms = [
        entry
        for entry in glossary.get("entries", [])
        if entry.get("decision") == "translate"
        and entry.get("target")
        # 「target 就是原词」表示保留英文，译成中文不算错，这里不追究
        and any("\u4e00" <= char <= "\u9fff" for char in entry["target"])
    ]
    misses: list[dict] = []
    for element in document.elements:
        for node in element.walk():
            if node.kind not in TRANSLATE_KINDS or node.skip or not node.translation:
                continue
            for entry in terms:
                if not _term_pattern(entry["term"]).search(node.text):
                    continue
                if not _target_present(entry["target"], node.translation):
                    misses.append(
                        {
                            "term": entry["term"],
                            "target": entry["target"],
                            "id": node.id,
                            "page": node.page + 1,
                            "text": node.translation[:60],
                        }
                    )
    return {"term_misses": misses[:40], "term_misses_count": len(misses)}


def _numbers(document: Document) -> dict:
    """数字与引用编号守恒：原文里出现的数值应在译文中保留。"""
    losses: list[dict] = []
    for element in document.elements:
        for node in element.walk():
            if node.kind not in TRANSLATE_KINDS or node.skip or not node.translation:
                continue
            source = _source_text(node)
            source_values = _number_values(source)
            translated_values = _number_values(node.translation)
            # 「1,000」与「1000」、「1.2 million」与「120 万」是同一个数，按数值比较
            lost_values = source_values - translated_values
            if not lost_values:
                continue
            lost = [
                raw
                for raw, value in _number_raw(source)
                if value in lost_values
            ]
            glued = {match.group(0)[1] for match in _GLUED_DIGIT_RE.finditer(source)}
            lost = [
                number
                for number in dict.fromkeys(lost)
                if not (len(number) == 1 and number in glued)
                and _standalone_number(number, source)
            ]
            if lost:
                losses.append(
                    {
                        "id": node.id,
                        "page": node.page + 1,
                        "lost": lost[:6],
                        "text": node.translation[:60],
                    }
                )
    return {"number_loss": losses[:40], "count": len(losses)}


def _missed(document: Document) -> list[str]:
    missed: list[str] = []
    for element in document.elements:
        for node in element.walk():
            if node.kind not in TRANSLATE_KINDS or node.skip:
                continue
            # 只有 URL/邮箱的脚注不需要翻译，统计字母数时先摘掉链接。
            letters = len(LETTER_RE.findall(_URL_RE.sub(" ", node.text)))
            cjk = len(CJK_RE.findall(node.translation))
            if letters >= 40 and cjk < max(6, letters / 20):
                missed.append(f"p{node.page + 1} {node.kind}: {node.text[:60]}")
    return missed


def _readability(document: Document) -> dict:
    long_paragraphs = []
    for element in document.elements:
        if element.kind != "body":
            continue
        length = len(element.translation or element.text)
        if length > 900:
            long_paragraphs.append({"page": element.page + 1, "id": element.id, "chars": length})
    residual = []
    for element in document.elements:
        if element.kind != "body":
            continue
        text = element.translation or ""
        if not text:
            continue
        latin = len(LETTER_RE.findall(text))
        if latin / max(1, len(text)) > 0.5:
            residual.append({"page": element.page + 1, "id": element.id, "text": text[:60]})
    return {
        "long_paragraphs": long_paragraphs,
        "residual_english": residual,
        "tiny_pages": [],
    }


# ---------- 调度 ----------

@dataclass
class StageResult:
    stage: str
    path: str
    elapsed: float
    summary: dict

    def line(self) -> str:
        detail = ", ".join(f"{key}={value}" for key, value in self.summary.items())
        return f"{self.stage}: {detail} → {self.path}"


def run_stage(
    stage: str,
    src: str | Path,
    provider=None,
    font_file: str | Path | None = None,
    progress=None,
) -> StageResult:
    workspace = Workspace.for_source(src)
    started = time.time()
    if stage == "structure":
        document = run_structure(workspace.source, workspace)
        target = workspace.path("structure")
        summary = {
            "pages": len(document.page_columns),
            "columns": document.columns,
            "elements": len(document.elements),
            "low_confidence": len(document.low_confidence()),
        }
    elif stage == "glossary":
        glossary = run_glossary(workspace)
        target = workspace.path("glossary")
        summary = {
            "terms": len(glossary["entries"]),
            "keep": glossary["counts"]["keep"],
            "translate": glossary["counts"]["translate"],
            "candidate": glossary["counts"]["candidate"],
            "conflicts": glossary["counts"]["conflicts"],
            "unused": len(glossary["unused_user_terms"]),
        }
    elif stage == "translate":
        if provider is None:
            raise RuntimeError("translate 阶段需要翻译服务（--provider）")
        translation = run_translate(workspace, provider, progress=progress)
        target = workspace.path("translate")
        summary = {
            "segments": len(translation["entries"]),
            "failed": len(translation["failed"]),
        }
    elif stage == "layout":
        layout = run_layout(workspace, font_file=font_file)
        target = workspace.path("layout")
        summary = {"pages": layout["pages"], "output": layout["output"]}
    elif stage == "check":
        check = run_check(workspace)
        target = workspace.path("check")
        summary = {"ok": check.get("ok"), "blockers": len(check.get("blockers", []))}
    else:
        raise ValueError(f"未知阶段：{stage}")
    return StageResult(stage, str(target), round(time.time() - started, 2), summary)


def run_all(
    src: str | Path,
    provider,
    font_file: str | Path | None = None,
    progress=None,
) -> list[StageResult]:
    results = []
    for stage in ("structure", "glossary", "translate", "layout", "check"):
        results.append(run_stage(stage, src, provider, font_file, progress))
    return results
