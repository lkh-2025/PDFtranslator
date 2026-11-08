"""结构树报告：跑阶段 ① 并把页面、栏数、元素统计与低置信项打印出来。

用法：python tools\\structure_report.py 论文.pdf [...]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdf_translator.reflow.stages import run_structure  # noqa: E402
from pdf_translator.reflow.workspace import Workspace  # noqa: E402

KIND_LABEL = {
    "title": "标题",
    "subtitle": "副标题",
    "heading": "章节标题",
    "body": "正文",
    "caption": "题注",
    "figure": "图",
    "figure_text": "图内标注",
    "table": "表",
    "equation": "公式",
    "code": "代码",
    "reference": "参考文献",
    "footnote": "脚注",
    "header": "页眉",
    "footer": "页脚",
}


def report(path: str) -> None:
    workspace = Workspace.for_source(path)
    document = run_structure(path, workspace)
    data = workspace.load("structure") or {}
    print("=" * 78)
    print(f"文件：{path}")
    print(f"页数：{len(document.page_columns)}  页面：{document.page_size[0]:.1f} x {document.page_size[1]:.1f}")
    print(f"主导栏数：{document.columns}  逐页栏数：{document.page_columns}")
    print(f"正文基准字号：{document.body_size}pt  页眉页脚丢弃：{document.dropped_headers}")
    if document.degraded:
        print(f"降级：{document.degraded}")

    counts = data.get("counts", {})
    total = sum(counts.values())
    print(f"元素合计：{total}（含图注/表题子节点）")
    for kind, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {KIND_LABEL.get(kind, kind):8s} {kind:10s} {count}")

    print("\n元素树（前 40 项，缩进表示子节点）：")
    shown = 0
    for element in data.get("elements", []):
        if shown >= 40:
            print("  ...")
            break
        _print_element(element, "")
        shown += 1

    low = data.get("low_confidence", [])
    print(f"\n低置信项：{len(low)}")
    for item in low[:15]:
        notes = "；".join(item.get("notes", []))
        print(
            f"  p{item['page']} {item['kind']} conf={item['confidence']}: "
            f"{item['text'][:60]}  [{notes}]"
        )
    if len(low) > 15:
        print(f"  ... 其余 {len(low) - 15} 项见 structure.json")


def _print_element(element: dict, indent: str) -> None:
    kind = element.get("kind", "")
    label = KIND_LABEL.get(kind, kind)
    prefix = f"p{element.get('page')} col{element.get('column')} "
    if kind == "heading":
        label = f"标题 L{element.get('level', 1)}"
    text = (element.get("text") or "").replace("\n", " ")
    if kind in ("figure", "table"):
        text = element.get("label") or f"{len(element.get('rows', []))} 行"
    extra = f" conf={element.get('confidence')}"
    print(f"  {indent}- {prefix}{label}: {text[:70]}{extra}")
    for child in element.get("children", []):
        _print_element(child, indent + "  ")


if __name__ == "__main__":
    raw = sys.argv[1:]
    wants_help = bool({"-h", "--help"} & set(raw))
    files = [item for item in raw if not item.startswith("-")]
    if wants_help or not files:
        print(__doc__)
        raise SystemExit(0 if wants_help else 2)
    for item in files:
        report(item)
