"""专名保留审计：对照原 PDF 与译文 PDF，统计专名是否原样保留。

候选专名只取可判定的类别（缩写 / 驼峰品牌 / 型号规格 / 单位 / DOI / arXiv /
URL / 邮箱 / 带单位标记的作者名 / 词表词条），避免把普通大写短语当作专名。
参考文献按编号标记存在性判定，作者/单位按整行存在性判定。

示例（Windows PowerShell，单行可复制）：
python tools\term_audit.py --source 论文.pdf --translated out\results\paper_zh.pdf
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import fitz  # noqa: E402

from pdf_translator.paths import config_file, reports_dir  # noqa: E402

ABBREV_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}(?:[/-][A-Z0-9]+)?\b")
CAMEL_RE = re.compile(r"\b[A-Z][a-z]{1,10}[A-Z][A-Za-z0-9]{1,12}\b")
SPEC_RE = re.compile(
    r"\b(?:RTX|GTX|ISO|CUDA|HTTP|USB|PCIe|Wi-?Fi|DDR|GPT|TPU|TensorRT|ONNX)\s?[0-9]+(?:[./][0-9]+)*\b",
    re.IGNORECASE,
)
UNIT_RE = re.compile(
    r"\b[0-9]+(?:\.[0-9]+)?\s?(?:MB|GB|TB|KB|GHz|MHz|ms|fps|FPS|FLOPs|TFLOPS)\b"
)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s,;]+")
ARXIV_RE = re.compile(r"arXiv:\s?\d{4}\.\d{4,5}", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s)）]+|\bwww\.[^\s)）]+")
EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[A-Za-z]{2,}\b")
AUTHOR_RE = re.compile(r"\b[A-Z][a-z]{1,15}\s+[A-Z][a-z]{1,15}\d\b")
AFFIL_RE = re.compile(
    r"[^\n]*(?:University|Institute|College|Laboratory|Inc\.|Ltd\.|Corporation)[^\n]*"
)
REF_MARK_RE = re.compile(r"\[[0-9]{1,3}\]")

STOP_ABBREV = {
    "THE", "AND", "FOR", "WITH", "FROM", "THIS", "THAT", "OUR", "WE", "ALL",
    "PDF", "CPU", "GPU", "RAM", "AI", "URL", "DOI", "II", "III", "IV", "V",
    "TABLE", "FIG", "EQ", "SEC", "APP", "CVPR", "IEEE", "ACM", "OR", "OF",
}
VARIANT_MAP = {
    "ViT": ["视觉变换器", "视觉 Transformer", "视觉Transformer"],
    "ViTs": ["视觉变换器", "视觉 Transformer"],
    "UCT": ["置信上限", "上限置信区间"],
    "SOTA": ["最先进", "领先水平"],
    "DeiT": ["数据高效图像变换器"],
    "CNN": ["卷积神经网络"],
    "MLP": ["多层感知机"],
    "ImageNet": ["图像网"],
}


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _author_needle(term: str) -> str:
    """作者名后的数字是单位上标，排版时可能丢失，比对时去掉。"""
    return term[:-1] if term and term[-1].isdigit() else term


def _load_glossary(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            entries[parts[0].strip()] = parts[1].strip()
    return entries


def extract_terms(text: str) -> dict[str, dict]:
    found: dict[str, dict] = {}

    def add(term: str, category: str) -> None:
        term = term.strip(" .,;:()（）[]")
        if not term or len(term) < 2:
            return
        item = found.setdefault(term, {"term": term, "category": category, "count": 0})
        item["count"] += 1

    for match in ABBREV_RE.finditer(text):
        term = match.group(0).rstrip("-")
        if term.upper() in STOP_ABBREV:
            continue
        add(term, "abbreviation")
    for match in CAMEL_RE.finditer(text):
        add(match.group(0), "brand")
    for match in SPEC_RE.finditer(text):
        add(match.group(0), "spec")
    for match in UNIT_RE.finditer(text):
        add(match.group(0), "unit")
    for match in DOI_RE.finditer(text):
        add(match.group(0).rstrip("."), "doi")
    for match in ARXIV_RE.finditer(text):
        add(re.sub(r"\s+", "", match.group(0)), "arxiv")
    for match in URL_RE.finditer(text):
        add(match.group(0).rstrip(".,;)-"), "url")
    for match in EMAIL_RE.finditer(text):
        add(match.group(0), "email")
    for match in AUTHOR_RE.finditer(text):
        add(match.group(0), "author")
    return found


def page_texts(pdf: Path) -> list[str]:
    doc = fitz.open(str(pdf))
    try:
        return [page.get_text() for page in doc]
    finally:
        doc.close()


def audit(source: Path, translated: Path, tier: str, glossary: Path) -> dict:
    source_pages = page_texts(source)
    output_text = "\n".join(page_texts(translated))

    terms: dict[str, dict] = {}
    for index, text in enumerate(source_pages, start=1):
        for key, item in extract_terms(text).items():
            entry = terms.setdefault(
                key,
                {
                    "term": item["term"],
                    "category": item["category"],
                    "occurrences": 0,
                    "hits": 0,
                    "pages": [],
                },
            )
            entry["occurrences"] += item["count"]
            if index not in entry["pages"]:
                entry["pages"].append(index)

    glossary_entries = _load_glossary(glossary)
    lowered_source = "\n".join(source_pages).lower()
    for term, target in glossary_entries.items():
        # 词表里给了中文译法的条目本来就该被翻译，只审计“保留原文”的条目。
        if target:
            continue
        if term.lower() in lowered_source:
            terms.setdefault(
                term,
                {
                    "term": term,
                    "category": "glossary",
                    "occurrences": lowered_source.count(term.lower()),
                    "hits": 0,
                    "pages": [],
                },
            )

    squashed_output = _squash(output_text)
    boundary_categories = {"abbreviation", "brand", "spec", "doi", "arxiv", "email"}
    for entry in terms.values():
        term = entry["term"]
        if entry["category"] in boundary_categories:
            hits = len(
                re.findall(
                    r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
                    output_text,
                )
            )
        else:
            needle = _squash(term if entry["category"] != "author" else _author_needle(term))
            hits = squashed_output.count(needle) if needle else 0
        entry["hits"] = hits
        entry["kept"] = hits > 0
        entry["mistranslated"] = not entry["kept"]
        if not entry["kept"]:
            entry["suspected_variants"] = [
                variant
                for variant in VARIANT_MAP.get(term, [])
                if _squash(variant) in squashed_output
            ]

    blocks = []
    for index, text in enumerate(source_pages, start=1):
        for match in AFFIL_RE.finditer(text):
            line = " ".join(match.group(0).split())
            if 8 <= len(line) <= 160:
                blocks.append({"kind": "affiliation", "text": line, "pages": [index]})
        for match in AUTHOR_RE.finditer(text):
            blocks.append({"kind": "author", "text": match.group(0), "pages": [index]})
    squashed_output = _squash(output_text)
    for block in blocks:
        needle_text = (
            _author_needle(block["text"]) if block["kind"] == "author" else block["text"]
        )
        block["found"] = _squash(needle_text) in squashed_output

    source_marks = len(REF_MARK_RE.findall("\n".join(source_pages)))
    output_marks = len(REF_MARK_RE.findall(output_text))
    reference_block = {
        "kind": "reference-marks",
        "text": f"编号标记 [n]：原文 {source_marks} 个，译文 {output_marks} 个",
        "pages": [],
        "found": output_marks >= source_marks * 0.95,
        "source_marks": source_marks,
        "output_marks": output_marks,
    }
    blocks.append(reference_block)

    audited = [item for item in terms.values() if item["occurrences"] >= 1]
    kept = [item for item in audited if item["kept"]]
    failed = [item for item in audited if not item["kept"]]
    block_failed = [item for item in blocks if not item["found"]]
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tier": tier,
        "source": str(source),
        "translated": str(translated),
        "summary": {
            "terms": len(audited),
            "kept_terms": len(kept),
            "verbatim_rate": round(len(kept) / len(audited), 4) if audited else 1.0,
            "blocks": len(blocks),
            "blocks_found": len(blocks) - len(block_failed),
            "failed_terms": [item["term"] for item in failed],
            "failed_blocks": [item["text"][:60] for item in block_failed],
        },
        "terms": sorted(audited, key=lambda item: (not item["kept"], -item["occurrences"])),
        "blocks": blocks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="专名保留审计")
    parser.add_argument("--source", required=True, help="原 PDF")
    parser.add_argument("--translated", required=True, help="译文 PDF")
    parser.add_argument("--tier", default="llm")
    parser.add_argument("--glossary", default=str(config_file("glossary.tsv")))
    parser.add_argument("--output", default=None, help="审计结果 JSON 路径")
    args = parser.parse_args(argv)

    source = Path(args.source)
    translated = Path(args.translated)
    if not source.is_file() or not translated.is_file():
        print("输入文件不存在。")
        return 1
    report = audit(source, translated, args.tier, Path(args.glossary))
    target = (
        Path(args.output)
        if args.output
        else reports_dir() / f"term_audit_{args.tier}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = report["summary"]
    print(
        f"专名 {summary['terms']} 个，原样保留 {summary['kept_terms']} 个，"
        f"命中率 {summary['verbatim_rate'] * 100:.1f}%"
    )
    print(f"作者/单位/参考文献块 {summary['blocks']} 个，通过 {summary['blocks_found']} 个")
    if summary["failed_terms"]:
        print("未原样保留：" + "、".join(summary["failed_terms"][:20]))
    print(f"审计结果：{target}")
    ok = summary["verbatim_rate"] >= 1.0 and summary["blocks"] == summary["blocks_found"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
