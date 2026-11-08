"""生成左右并排的页面预览图，用于对比两种版式的观感。

用法：
    python tools\\reflow_preview.py out\\results\\a_zh.pdf out\\results\\b_zh.pdf out\\archive\\preview.png
    python tools\\reflow_preview.py a.pdf b.pdf preview.png --page 2 --dpi 110
"""

from __future__ import annotations

import sys
from pathlib import Path

import fitz

GAP = 12


def build(left: Path, right: Path, target: Path, page_index: int, dpi: int) -> Path:
    docs = [fitz.open(str(left)), fitz.open(str(right))]
    try:
        images = []
        for doc in docs:
            index = min(page_index, doc.page_count - 1)
            images.append(doc[index].get_pixmap(dpi=dpi))
        width = images[0].width + images[1].width + GAP
        height = max(image.height for image in images)
        canvas = fitz.open()
        page = canvas.new_page(width=width, height=height)
        x = 0.0
        for image in images:
            page.insert_image(fitz.Rect(x, 0, x + image.width, image.height), pixmap=image)
            x += image.width + GAP
        target.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(dpi=72).save(str(target))
        canvas.close()
        return target
    finally:
        for doc in docs:
            doc.close()


def main() -> int:
    args = [item for item in sys.argv[1:] if not item.startswith("--")]
    options = [item for item in sys.argv[1:] if item.startswith("--")]
    if len(args) < 3:
        print(__doc__)
        return 2
    page_index, dpi = 0, 110
    for option in options:
        if option.startswith("--page="):
            page_index = max(0, int(option.split("=", 1)[1]) - 1)
        elif option.startswith("--dpi="):
            dpi = int(option.split("=", 1)[1])
    target = build(Path(args[0]), Path(args[1]), Path(args[2]), page_index, dpi)
    print(f"预览图：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
