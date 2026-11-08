"""reflow 五阶段的工作区：中间产物的落点与读写。

每篇文档一个目录 `out/reports/<doc>/`，五个阶段各自一份 JSON：
structure / glossary / translation / layout / selfcheck。
单阶段重跑只覆盖自己的产物，其余阶段可继续复用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..paths import reports_dir, results_dir

STAGES = ("structure", "glossary", "translate", "layout", "check")
STAGE_FILES = {
    "structure": "structure.json",
    "glossary": "glossary.json",
    "translate": "translation.json",
    "layout": "layout.json",
    "check": "selfcheck.json",
}
_SLUG_RE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


def slug_for(src: str | Path) -> str:
    stem = Path(src).stem.strip()
    slug = _SLUG_RE.sub("_", stem).strip("_")
    return (slug or "document")[:80]


@dataclass
class Workspace:
    source: Path
    root: Path
    output_pdf: Path

    @classmethod
    def for_source(cls, src: str | Path) -> "Workspace":
        source = Path(src)
        slug = slug_for(source)
        root = reports_dir() / slug
        return cls(source=source, root=root, output_pdf=results_dir() / f"{slug}_zh.pdf")

    @property
    def slug(self) -> str:
        return self.root.name

    def path(self, stage: str) -> Path:
        return self.root / STAGE_FILES[stage]

    def has(self, stage: str) -> bool:
        return self.path(stage).is_file()

    def load(self, stage: str) -> dict | None:
        target = self.path(stage)
        if not target.is_file():
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, stage: str, data: dict) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path(stage)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        return target
