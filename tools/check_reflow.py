"""重排产物的独立自检入口：不启动翻译，只检查已有 PDF。

用法：
    python tools\\check_reflow.py 原文.pdf out\\results\\xxx_zh.pdf
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pdf_translator.reflow.check import check_output  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    source, output = Path(sys.argv[1]), Path(sys.argv[2])
    for path in (source, output):
        if not path.is_file():
            print(f"文件不存在：{path}")
            return 2
    report = check_output(source, output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("自检通过" if report["ok"] else "自检未通过")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
