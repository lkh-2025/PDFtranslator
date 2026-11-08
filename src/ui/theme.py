"""界面主题：配色、字体与几个自绘控件（圆角卡片 / 按钮 / 进度条）。

Tk 原生控件在 Windows 上观感偏旧，这里用 Canvas 自绘圆角矩形与描边，
再在其上放普通控件，得到一个接近现代桌面应用的观感，同时不引入第三方依赖。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

# --- 配色 ---------------------------------------------------------------
BG = "#F5F6F8"
CARD = "#FFFFFF"
TEXT = "#0F172A"
TEXT_SOFT = "#334155"
MUTED = "#6B7280"
FAINT = "#9CA3AF"
BORDER = "#E4E7EC"
BORDER_SOFT = "#EEF0F3"
ACCENT = "#4F46E5"
ACCENT_DARK = "#4338CA"
ACCENT_SOFT = "#EEF0FF"
ACCENT_LINE = "#C7CBFF"
OK = "#059669"
WARN = "#B45309"
DANGER = "#DC2626"

FONT = "Microsoft YaHei UI"
MONO = "Cascadia Mono"

# 高 DPI 缩放：字体（pt）由 Tk 自动放大，像素尺寸需要自己乘同一个系数，
# 否则 200% 缩放下文字会明显挤到边框上。
_SF = 1.0


def init_scaling(root: tk.Misc) -> float:
    """在创建任何控件之前调用：让像素尺寸跟随系统缩放。"""
    global _SF
    try:
        _SF = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except Exception:
        _SF = 1.0
    return _SF


def enable_dpi_awareness() -> None:
    """让 Windows 直接按物理像素渲染：高分屏上文字才锐利。"""
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 系统级 DPI 感知
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def px(value: float) -> int:
    return int(round(value * _SF))


def font(size: int = 10, weight: str = "normal") -> tuple:
    return (FONT, size, weight)


# --- 基础绘制 -----------------------------------------------------------
def round_rect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **kwargs,
) -> int:
    """在 canvas 上画一个圆角矩形，返回 item id。"""
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class Card(tk.Canvas):
    """圆角白卡片：内容放进 `.body`，高度随内容自动增长。"""

    def __init__(
        self,
        master: tk.Misc,
        *,
        pad: int = 22,
        radius: int = 14,
        bg: str = CARD,
        border: str = BORDER,
        parent_bg: str = BG,
        dashed: bool = False,
    ) -> None:
        pad = px(pad)
        radius = px(radius)
        super().__init__(
            master,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            height=pad * 2 + px(24),
        )
        self._pad = pad
        self._radius = radius
        self._bg = bg
        self._border = border
        self._dashed = dashed
        self._shape: int | None = None
        self._last_height = -1
        self.body = tk.Frame(self, bg=bg)
        self._window = self.create_window(pad, pad, anchor="nw", window=self.body)
        self.bind("<Configure>", self._on_resize)
        self.body.bind("<Configure>", self._on_body)

    def _on_body(self, _event: tk.Event | None = None) -> None:
        need = self.body.winfo_reqheight() + self._pad * 2
        if need != self._last_height and need > 0:
            self._last_height = need
            self.configure(height=need)

    def _on_resize(self, event: tk.Event) -> None:
        width = max(event.width, 40)
        inner = max(width - self._pad * 2, 40)
        self.itemconfigure(self._window, width=inner)
        if self._shape is not None:
            self.delete(self._shape)
        ring = 1.0
        self._shape = round_rect(
            self,
            ring,
            ring,
            width - ring,
            max(event.height, 30) - ring,
            self._radius,
            fill=self._bg,
            outline=self._border,
            width=1,
            dash=(4, 3) if self._dashed else None,
        )
        self.tag_lower(self._shape)


class Button(tk.Canvas):
    """圆角按钮：primary（实心）/ ghost（描边）/ soft（浅底）。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command=None,
        *,
        variant: str = "primary",
        parent_bg: str = BG,
        height: int = 42,
        min_width: int = 0,
        padx: int = 22,
        radius: int = 10,
        size: int = 10,
        weight: str = "bold",
    ) -> None:
        super().__init__(
            master,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            height=px(height),
            cursor="hand2",
        )
        self._text = text
        self._command = command
        self._variant = variant
        self._height = px(height)
        self._padx = px(padx)
        self._radius = px(radius)
        self._font = tkfont.Font(family=FONT, size=size, weight=weight)
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self._min_width = px(min_width)
        self._shape: int | None = None
        self._label: int | None = None

        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._fit_width()

    # 与 tk.Button 对齐的一点点兼容层，方便原有调用点直接复用
    _IGNORED = {
        "bg", "fg", "activebackground", "activeforeground", "disabledforeground",
        "relief", "bd", "highlightthickness", "highlightbackground", "padx", "pady",
        "font", "cursor", "indicatoron", "selectcolor",
    }

    def configure(self, **kwargs):  # type: ignore[override]
        if "text" in kwargs:
            self._text = str(kwargs.pop("text"))
            self._fit_width()
        if "state" in kwargs:
            self._state = str(kwargs.pop("state"))
            self.configure_cursor()
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        for key in list(kwargs):
            if key in self._IGNORED:
                kwargs.pop(key)
        if kwargs:
            super().configure(**kwargs)
        self._redraw()

    config = configure

    def __getitem__(self, key: str):  # type: ignore[override]
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().__getitem__(key)

    def cget(self, key: str):  # type: ignore[override]
        return self[key]

    def configure_cursor(self) -> None:
        self.configure(cursor="hand2" if self._state != "disabled" else "arrow")

    # --- 绘制 ---
    def _fit_width(self) -> None:
        width = self._font.measure(self._text) + self._padx * 2
        self.configure(width=max(width, self._min_width))

    def _colors(self) -> tuple[str, str, str]:
        disabled = self._state == "disabled"
        if self._variant == "primary":
            if disabled:
                return "#E5E7EB", FAINT, "#E5E7EB"
            fill = ACCENT_DARK if (self._hover or self._pressed) else ACCENT
            return fill, "#FFFFFF", fill
        if self._variant == "soft":
            if disabled:
                return BORDER_SOFT, FAINT, BORDER_SOFT
            fill = ACCENT_SOFT if self._hover else "#F7F7FD"
            return fill, ACCENT, fill
        if disabled:
            return CARD, FAINT, BORDER_SOFT
        fill = "#F8FAFC" if self._hover else CARD
        return fill, TEXT_SOFT, BORDER

    def _redraw(self) -> None:
        width = max(self.winfo_width(), int(self["width"]))
        height = self._height
        fill, fg, outline = self._colors()
        if self._shape is not None:
            self.delete(self._shape)
            self._shape = None
        if self._label is not None:
            self.delete(self._label)
            self._label = None
        offset = 1 if self._pressed else 0
        self._shape = round_rect(
            self,
            0.5,
            0.5 + offset,
            width - 0.5,
            height - 0.5 + offset,
            self._radius,
            fill=fill,
            outline=outline,
            width=1,
        )
        self._label = self.create_text(
            width / 2,
            height / 2 + offset,
            text=self._text,
            fill=fg,
            font=self._font,
        )

    # --- 交互 ---
    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event=None) -> None:
        if self._state == "disabled":
            return
        self._pressed = True
        self._redraw()

    def _on_release(self, _event=None) -> None:
        if self._state == "disabled":
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if was_pressed and self._command is not None:
            self._command()


class Bar(tk.Canvas):
    """细进度条：`bar["value"] = 42`。"""

    def __init__(
        self,
        master: tk.Misc,
        *,
        parent_bg: str = BG,
        height: int = 6,
        track: str = "#E7E9EE",
        fill: str = ACCENT,
    ) -> None:
        super().__init__(
            master, bg=parent_bg, highlightthickness=0, bd=0, height=px(height)
        )
        self._height = px(height)
        self._track = track
        self._fill = fill
        self._value = 0.0
        self.bind("<Configure>", lambda _e: self._redraw())

    def __setitem__(self, key: str, value) -> None:  # type: ignore[override]
        if key == "value":
            try:
                self._value = max(0.0, min(100.0, float(value)))
            except (TypeError, ValueError):
                self._value = 0.0
            self._redraw()
            return
        super().__setitem__(key, value)

    def __getitem__(self, key: str):  # type: ignore[override]
        if key == "value":
            return self._value
        return super().__getitem__(key)

    def _redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 10)
        height = self._height
        round_rect(self, 0, 0, width, height, height / 2, fill=self._track, outline="")
        filled = width * self._value / 100.0
        if filled > 0.5:
            round_rect(
                self,
                0,
                0,
                max(filled, height),
                height,
                height / 2,
                fill=self._fill,
                outline="",
            )


class Dot(tk.Canvas):
    """状态小圆点 + 文字。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str = "",
        *,
        color: str = OK,
        parent_bg: str = CARD,
        fg: str = MUTED,
        size: int = 9,
    ) -> None:
        super().__init__(
            master, bg=parent_bg, highlightthickness=0, bd=0, height=18, width=200
        )
        self._color = color
        self._fg = fg
        self._size = size
        self._dot = self.create_oval(1, 7, 9, 15, fill=color, outline="")
        self._label = self.create_text(15, 11, text=text, fill=fg, font=font(size), anchor="w")
        self.bind("<Configure>", lambda _e: self._layout())

    def _layout(self) -> None:
        self.coords(self._label, 15, self.winfo_height() / 2)

    def set_text(self, text: str, color: str | None = None) -> None:
        self.itemconfigure(self._label, text=text)
        if color:
            self._color = color
            self.itemconfigure(self._dot, fill=color)
