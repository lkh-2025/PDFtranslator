"""开发用：把主界面与使用说明面板各截一张图，存到 docs/images（不参与打包）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tkinter as tk

from PIL import ImageGrab

import ui.app as app_module
import ui.theme as theme


def settle(root: tk.Misc, times: int = 10) -> None:
    for _ in range(times):
        root.update()
        time.sleep(0.08)


def grab(widget: tk.Misc, target: Path) -> None:
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    print(f"saved {target} {image.size}")


def main() -> int:
    theme.enable_dpi_awareness()
    root = tk.Tk()
    theme.init_scaling(root)
    app = app_module.PDFTranslatorApp(root)
    root.attributes("-topmost", True)
    settle(root, 12)
    grab(root, ROOT / "docs" / "images" / "ui-home.png")

    app._open_help()
    root.update()
    window = app._help_window
    window.attributes("-topmost", True)
    window.lift()
    settle(root, 12)
    grab(window, ROOT / "docs" / "images" / "ui-help.png")
    window.destroy()
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
