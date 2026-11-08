"""WPS 风格单窗口界面：导入 PDF → 显示页数/进度 → 翻译 → 打开结果。"""

from __future__ import annotations

import argparse
import gc
import json
import os
import queue
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# src 布局：直接 `python src\ui\app.py` 或 `python ui\app.py` 时，把 src 放进
# sys.path，保证不装包也能 import pdf_translator。装包后这行是无害的空操作。
SRC_DIR = Path(__file__).resolve().parents[1]
if (SRC_DIR / "pdf_translator").is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import fitz

from pdf_translator import __version__
from pdf_translator.paths import config_file, config_target, project_root, results_dir, out_dir
from pdf_translator.config import DEFAULT_BASE_URL, DEFAULT_MODEL
from pdf_translator.providers import create_offline_provider, create_provider
from pdf_translator.providers.base import TranslationProviderError
from pdf_translator.providers.offline_config import (
    OfflineSettings,
    bundled_model_dir,
    gguf_model_dir,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except Exception:  # tkinterdnd2 未安装时退化为点击选择
    DND_FILES = None
    TkinterDnD = None
    DND_AVAILABLE = False

try:  # 包内运行（python -m ui.app）与直接运行（python src\ui\app.py）都要能找到主题模块
    from . import theme
except ImportError:  # pragma: no cover - 直接运行脚本时的兜底
    import theme  # type: ignore[no-redef]

BG = theme.BG
CARD = theme.CARD
TEXT = theme.TEXT
MUTED = theme.MUTED
ACCENT = theme.ACCENT
ACCENT_DARK = theme.ACCENT_DARK
BORDER = theme.BORDER
BORDER_SOFT = theme.BORDER_SOFT
SOFT = theme.ACCENT_SOFT
FONT = theme.FONT

ENGINE_API = "api"
ENGINE_MOCK = "mock"
TIER_ORDER = ("llm",)
TIER_NAMES = {"llm": "自然档"}
TIER_LABELS = {"llm": "Qwen2.5-7B-Instruct"}
TIER_NOTES = {"llm": "GGUF int4 模型约 4.36 GB"}
DEFAULT_TIER = "llm"
SETTINGS_FILE = "ui_settings.json"


@dataclass
class TranslationResult:
    """界面展示用的结果摘要（由五阶段工作区的产物汇总而来）。"""

    input_path: Path
    output_path: Path
    provider: str
    pages: int
    total_units: int
    translated_units: int
    skipped_units: int
    elapsed: float
    cache_hits: int = 0
    warnings: list[str] = field(default_factory=list)
    selfcheck_ok: bool = True
    selfcheck_path: str = ""
    selfcheck_pages: list = field(default_factory=list)
    selfcheck_rounds: list = field(default_factory=list)


def tier_title(tier: str) -> str:
    return f"{TIER_NAMES.get(tier, tier)}（{TIER_LABELS.get(tier, '')}）"



def load_last_engine() -> str:
    try:
        data = json.loads(config_file(SETTINGS_FILE).read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_TIER
    value = str(data.get("engine", ""))
    if value in TIER_ORDER or value in (ENGINE_API, ENGINE_MOCK):
        return value
    return DEFAULT_TIER


def save_last_engine(value: str) -> None:
    try:
        path = config_target(SETTINGS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["engine"] = value
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def process_memory_mb() -> float:
    """当前进程工作集，用于切档后检查内存是否叠加。"""
    if not sys.platform.startswith("win"):
        return 0.0
    import ctypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Counters),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_bool
        psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        return counters.WorkingSetSize / 1024 / 1024
    except Exception:
        return 0.0


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def elide(text: str, limit: int = 64) -> str:
    if len(text) <= limit:
        return text
    return text[:22] + "…" + text[-(limit - 23) :]


def app_data_path(name: str) -> Path:
    """数据文件（词表、专名缓存）：优先 config/，兼容根目录旧位置。"""
    return config_file(name)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(project_root()))
    except ValueError:
        return elide(str(path), 58)


class PDFTranslatorApp:
    def __init__(self, root: tk.Tk, initial_file: str | None = None) -> None:
        self.root = root
        self.pdf_path: Path | None = None
        self.page_count = 0
        self.output_path: Path | None = None
        self.result: TranslationResult | None = None
        self.running = False
        self.queue: queue.Queue = queue.Queue()
        self.queue_job: str | None = None

        self.api_key_var = tk.StringVar(value=os.environ.get("PDF_TRANSLATOR_API_KEY", ""))
        # 离线优先：默认关闭一切联网术语查询，全程不发起外部请求。
        self.web_terms_var = tk.BooleanVar(value=False)
        # 排版方式：只保留中文重排（fill 回填原坐标的模式已下线）。
        self.layout_var = tk.StringVar(value="reflow")
        # 启动时做一次本地档位自检：只认磁盘上已有模型（GGUF 或 int8），绝不联网下载。
        self.tier_sizes = {tier: self._tier_size(tier) for tier in TIER_ORDER}
        self.available_tiers = [tier for tier in TIER_ORDER if self._tier_available(tier)]
        saved = load_last_engine()
        if saved in TIER_ORDER and saved not in self.available_tiers:
            saved = DEFAULT_TIER if DEFAULT_TIER in self.available_tiers else (
                self.available_tiers[0] if self.available_tiers else saved
            )
        self.engine_var = tk.StringVar(value=saved)
        self.base_url_var = tk.StringVar(value=DEFAULT_BASE_URL)
        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.auto_open_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="准备就绪，请选择或拖入英文 PDF。")
        self.percent_var = tk.StringVar(value="0%")
        self.detail_var = tk.StringVar(value="")
        self.model_status_var = tk.StringVar(value="")
        self.term_report_path: Path | None = None
        self._run_started = 0.0
        self._page_index = 0
        self._page_total = 0
        # 自动自检时不弹模态框，避免无人值守卡住
        self.headless = False

        self._configure_window()
        self._build_styles()
        self._build_ui()
        self._on_engine_change()
        if self.available_tiers:
            ready = "、".join(TIER_NAMES.get(tier, tier) for tier in self.available_tiers)
            missing = [
                TIER_NAMES.get(tier, tier)
                for tier in TIER_ORDER
                if tier not in self.available_tiers
            ]
            text = f"本地档位自检：可用 {ready}"
            if missing:
                text += "；" + "、".join(missing) + "未找到模型文件"
            self.status_var.set(text + "。")
        if initial_file:
            self.load_pdf(initial_file)
        self.root.after_idle(self._fit_window)

    def _fit_window(self) -> None:
        self.root.update_idletasks()
        px = theme.px
        width = max(px(900), self.root.winfo_width())
        header = getattr(self, "_header", None)
        body = getattr(self, "_body", None)
        if header is not None and body is not None:
            natural = header.winfo_reqheight() + body.winfo_reqheight() + px(30)
        else:
            natural = self.root.winfo_reqheight()
        height = max(px(560), min(px(900), natural))
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(px(24), (self.root.winfo_screenheight() - height) // 2 - px(12))
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        # 首轮 layout 时字体还没量完，reqheight 偏大；再校一次才稳。
        passes = getattr(self, "_fit_passes", 0)
        if passes < 2:
            self._fit_passes = passes + 1
            self.root.after(120, self._fit_window)

    def _configure_window(self) -> None:
        px = theme.px
        self.root.title("PDF 文档翻译")
        self.root.configure(bg=BG)
        self.root.geometry(f"{px(960)}x{px(800)}")
        self.root.minsize(px(680), px(500))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F1>", lambda _event: self._open_help())

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "App.TCombobox",
            fieldbackground=CARD,
            background=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowsize=14,
            padding=6,
            font=(FONT, 10),
        )
        style.map(
            "App.TCombobox",
            fieldbackground=[("readonly", CARD)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", CARD)],
            selectforeground=[("readonly", TEXT)],
        )

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=CARD, height=theme.px(96))
        header.pack(fill="x")
        header.pack_propagate(False)

        title_box = tk.Frame(header, bg=CARD)
        title_box.pack(side="left", padx=theme.px(34), pady=(theme.px(16), theme.px(12)))
        tk.Label(
            title_box,
            text="PDF 文档翻译",
            bg=CARD,
            fg=TEXT,
            font=(FONT, 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="英文 PDF · 简体中文 · 公式与图表原样保留 · 全程本地",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 9),
        ).pack(anchor="w", pady=(3, 0))

        badge = tk.Frame(header, bg=SOFT)
        badge.pack(side="right", padx=(0, theme.px(34)))
        tk.Label(
            badge,
            text="●",
            bg=SOFT,
            fg=theme.OK,
            font=(FONT, 8),
            padx=0,
        ).pack(side="left", padx=(theme.px(11), theme.px(4)))
        tk.Label(
            badge,
            text="本地离线",
            bg=SOFT,
            fg=ACCENT,
            font=(FONT, 9),
            pady=theme.px(6),
            padx=0,
        ).pack(side="left", padx=(0, theme.px(11)))

        self.help_button = theme.Button(
            header,
            text="使用说明",
            command=self._open_help,
            variant="soft",
            parent_bg=CARD,
            height=34,
            padx=14,
            size=9,
            weight="normal",
        )
        self.help_button.pack(side="right", padx=(0, theme.px(12)))

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        body = tk.Frame(self.root, bg=BG)
        self._header = header
        self._body = body
        body.pack(
            fill="both",
            expand=True,
            padx=theme.px(34),
            pady=(theme.px(20), theme.px(16)),
        )

        self._build_drop_card(body)
        self._build_settings(body)
        self._build_actions(body)
        self._build_footer(body)

        if DND_AVAILABLE:
            self._register_drop_tree(self.drop_card)

    def _build_drop_card(self, parent: tk.Frame) -> None:
        card = theme.Card(
            parent,
            pad=20,
            radius=16,
            dashed=True,
            border=theme.ACCENT_LINE,
        )
        card.pack(fill="x")
        self.drop_card = card
        card.body.configure(cursor="hand2")
        card.body.bind("<Button-1>", lambda _event: self._choose_file())

        inner = tk.Frame(card.body, bg=CARD, cursor="hand2")
        inner.pack(fill="x")
        inner.bind("<Button-1>", lambda _event: self._choose_file())

        self.icon = tk.Canvas(
            inner,
            width=theme.px(52),
            height=theme.px(60),
            bg=CARD,
            highlightthickness=0,
            cursor="hand2",
        )
        self.icon.pack(side="left", padx=(theme.px(4), theme.px(18)))
        theme.round_rect(
            self.icon, 1, 1, theme.px(51), theme.px(59), theme.px(12), fill=SOFT, outline=""
        )
        self.icon.create_text(theme.px(26), theme.px(26), text="PDF", fill=ACCENT, font=(FONT, 12, "bold"))
        self.icon.create_text(theme.px(26), theme.px(44), text="EN → 中", fill=ACCENT, font=(FONT, 7))
        self.icon.bind("<Button-1>", lambda _event: self._choose_file())

        text_box = tk.Frame(inner, bg=CARD, cursor="hand2")
        text_box.pack(side="left", fill="x", expand=True)
        self.file_name_label = tk.Label(
            text_box,
            text="拖入 PDF 文件，或点击此处选择",
            bg=CARD,
            fg=TEXT,
            font=(FONT, 11, "bold"),
            anchor="w",
            cursor="hand2",
        )
        self.file_name_label.pack(anchor="w")
        self.file_meta_label = tk.Label(
            text_box,
            text="支持文本型 PDF，自动识别页数",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 9),
            anchor="w",
            cursor="hand2",
        )
        self.file_meta_label.pack(anchor="w", pady=(4, 0))
        for widget in (text_box, self.file_name_label, self.file_meta_label):
            widget.bind("<Button-1>", lambda _event: self._choose_file())

        self.choose_label = theme.Button(
            inner,
            text="选择文件",
            command=self._choose_file,
            variant="soft",
            parent_bg=CARD,
            height=36,
            padx=18,
            size=9,
            weight="bold",
        )
        self.choose_label.pack(side="right")

        if DND_AVAILABLE:
            self.file_meta_label.configure(text="拖入或点击选择 · 支持文本型 PDF")

    def _pill(
        self,
        parent: tk.Widget,
        text: str,
        variable: tk.StringVar,
        value: str,
        command=None,
        *,
        enabled: bool = True,
    ) -> tk.Radiobutton:
        """扁平化的单选按钮，看着像分段控件。"""
        return tk.Radiobutton(
            parent,
            text=text,
            variable=variable,
            value=value,
            command=command or self._on_choice,
            indicatoron=False,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=CARD,
            fg=TEXT if enabled else theme.FAINT,
            activebackground=SOFT,
            activeforeground=ACCENT,
            selectcolor=SOFT,
            font=(FONT, 9),
            padx=13,
            pady=6,
            anchor="w",
            cursor="hand2" if enabled else "arrow",
            state="normal" if enabled else "disabled",
        )

    def _section_label(self, parent: tk.Widget, text: str) -> tk.Label:
        return tk.Label(
            parent, text=text, bg=CARD, fg=theme.TEXT_SOFT, font=(FONT, 9, "bold")
        )

    def _on_choice(self) -> None:
        self._style_choices()
        self._on_engine_change()

    def _style_choices(self) -> None:
        for widgets, variable in (
            (self.tier_radio, self.engine_var),
            (self.other_radio, self.engine_var),
        ):
            current = variable.get()
            for key, widget in widgets.items():
                selected = key == current
                enabled = str(widget["state"]) != "disabled"
                widget.configure(
                    bg=SOFT if selected else CARD,
                    fg=ACCENT if selected else (TEXT if enabled else theme.FAINT),
                    selectcolor=SOFT if selected else CARD,
                )
        self.root.after_idle(self._fit_window)

    def _build_settings(self, parent: tk.Frame) -> None:
        card = theme.Card(parent, pad=22)
        card.pack(fill="x", pady=(theme.px(14), 0))
        self.settings_card = card
        body = card.body

        # 排版方式（固定中文重排）--------------------------------------
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x")
        self._section_label(row, "排版方式").pack(side="left", padx=(2, 16))
        tk.Label(
            row,
            text="中文重排",
            bg=SOFT,
            fg=ACCENT,
            font=(FONT, 9),
            padx=theme.px(12),
            pady=theme.px(5),
        ).pack(side="left")
        tk.Label(
            row,
            text="解析章节与段落后按中文版式重排，页数会变化",
            bg=CARD,
            fg=theme.FAINT,
            font=(FONT, 8),
        ).pack(side="right")

        tk.Frame(body, bg=BORDER_SOFT, height=1).pack(fill="x", pady=(14, 12))

        # 翻译服务 ------------------------------------------------------
        row2 = tk.Frame(body, bg=CARD)
        row2.pack(fill="x")
        self._section_label(row2, "翻译服务").pack(side="left", padx=(2, 16))

        self.tier_radio: dict[str, tk.Radiobutton] = {}
        for tier in TIER_ORDER:
            available = tier in self.available_tiers
            button = self._pill(
                row2,
                TIER_NAMES.get(tier, tier),
                self.engine_var,
                tier,
                enabled=available,
            )
            button.pack(side="left", padx=(0, 6))
            self.tier_radio[tier] = button

        self.other_radio: dict[str, tk.Radiobutton] = {}
        for key, label in ((ENGINE_API, "在线 API"), (ENGINE_MOCK, "模拟")):
            button = self._pill(row2, label, self.engine_var, key)
            button.pack(side="left", padx=(0, 6))
            self.other_radio[key] = button

        self.engine_hint = tk.Label(
            body,
            text="",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 9),
            anchor="w",
            justify="left",
        )
        self.engine_hint.pack(fill="x", padx=2, pady=(10, 0))

        self.model_status_frame = tk.Frame(body, bg=CARD)
        self.model_status_label = tk.Label(
            self.model_status_frame,
            textvariable=self.model_status_var,
            bg=CARD,
            fg=theme.WARN,
            font=(FONT, 9),
            anchor="w",
            justify="left",
        )
        self.model_status_label.pack(side="left", fill="x", expand=True, padx=2)
        self.model_status_visible = False

        self.api_frame = tk.Frame(body, bg=CARD)
        tk.Label(
            self.api_frame,
            text="API Key",
            bg=CARD,
            fg=theme.TEXT_SOFT,
            font=(FONT, 9),
            width=7,
            anchor="w",
        ).pack(side="left")
        self.api_entry = self._entry(self.api_frame, self.api_key_var, show=None)
        self.api_entry.pack(side="left", fill="x", expand=True, ipady=3)
        tk.Label(
            self.api_frame,
            text="仅本次运行使用",
            bg=CARD,
            fg=theme.FAINT,
            font=(FONT, 8),
        ).pack(side="left", padx=(10, 0))

        self.advanced_toggle = tk.Label(
            body,
            text="高级设置  ▾",
            bg=CARD,
            fg=theme.FAINT,
            font=(FONT, 9),
            cursor="hand2",
        )
        self.advanced_toggle.pack(anchor="w", padx=2, pady=(12, 0))
        self.advanced_toggle.bind("<Button-1>", self._toggle_advanced)

        self.advanced_frame = tk.Frame(body, bg=CARD)
        self._build_advanced_rows()
        self.advanced_visible = False

    def _build_advanced_rows(self) -> None:
        frame = self.advanced_frame
        tk.Label(frame, text="接口地址", bg=CARD, fg=MUTED, font=(FONT, 9), width=8, anchor="w").grid(
            row=0, column=0, sticky="w", pady=4
        )
        self.base_url_entry = self._entry(frame, self.base_url_var)
        self.base_url_entry.grid(row=0, column=1, sticky="ew", padx=(8, 24), pady=4)

        tk.Label(frame, text="模型名称", bg=CARD, fg=MUTED, font=(FONT, 9), width=8, anchor="w").grid(
            row=1, column=0, sticky="w", pady=4
        )
        self.model_entry = self._entry(frame, self.model_var)
        self.model_entry.grid(row=1, column=1, sticky="ew", padx=(8, 24), pady=4)
        frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            frame,
            text="DeepSeek 填 https://api.deepseek.com 即可，带不带 /v1 都行",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 8),
            anchor="w",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 2))

        tk.Checkbutton(
            frame,
            text="联网查询专有名词（需要联网；默认关闭）",
            variable=self.web_terms_var,
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            activeforeground=TEXT,
            selectcolor=CARD,
            font=(FONT, 9),
            anchor="w",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        tk.Label(
            frame,
            text="关闭时离线档位全程不联网，模型只从本机 models\\ 读取。",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 8),
            anchor="w",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 4))

    def _entry(self, parent: tk.Widget, variable: tk.StringVar, show: str | None = None) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            font=(FONT, 10),
            bg=CARD,
            fg=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            insertbackground=TEXT,
            show=show or "",
        )

    def _build_actions(self, parent: tk.Frame) -> None:
        actions = tk.Frame(parent, bg=BG)
        actions.pack(fill="x", pady=(theme.px(16), 0))

        self.start_button = theme.Button(
            actions,
            text="开始翻译",
            command=self._start,
            variant="primary",
            parent_bg=BG,
            height=44,
            min_width=150,
            padx=26,
            size=10,
        )
        self.start_button.pack(side="left")

        progress_box = tk.Frame(actions, bg=BG)
        progress_box.pack(side="left", fill="x", expand=True, padx=(20, 2))

        top_line = tk.Frame(progress_box, bg=BG)
        top_line.pack(fill="x", pady=(4, 0))
        tk.Label(
            top_line,
            text="进度",
            bg=BG,
            fg=theme.FAINT,
            font=(FONT, 8),
        ).pack(side="left")
        self.percent_label = tk.Label(
            top_line, textvariable=self.percent_var, bg=BG, fg=theme.TEXT_SOFT, font=(FONT, 9)
        )
        self.percent_label.pack(side="right")

        self.progress = theme.Bar(progress_box, parent_bg=BG, height=6)
        self.progress.pack(fill="x", pady=(6, 0))

        tk.Label(
            progress_box,
            textvariable=self.detail_var,
            bg=BG,
            fg=theme.FAINT,
            font=(FONT, 8),
            anchor="w",
        ).pack(fill="x", pady=(5, 0))

        tk.Label(
            parent,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            font=(FONT, 9),
            anchor="w",
            justify="left",
            wraplength=820,
        ).pack(fill="x", pady=(12, 0))

    def _build_footer(self, parent: tk.Frame) -> None:
        footer = tk.Frame(parent, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(theme.px(12), 0))
        tk.Frame(parent, bg=BORDER, height=1).pack(
            side="bottom", fill="x", pady=(theme.px(16), 0)
        )

        self.output_label = tk.Label(
            footer,
            text="输出位置：out\\results\\<文件名>_zh.pdf",
            bg=BG,
            fg=theme.FAINT,
            font=(FONT, 8),
            anchor="w",
        )

        self.open_button = theme.Button(
            footer,
            text="打开输出文件",
            command=self._open_output,
            variant="ghost",
            parent_bg=BG,
            height=34,
            padx=14,
            size=9,
            weight="normal",
        )
        self.open_button.configure(state="disabled")
        self.open_button.pack(side="right")

        self.term_button = theme.Button(
            footer,
            text="自检报告",
            command=self._open_term_report,
            variant="ghost",
            parent_bg=BG,
            height=34,
            padx=14,
            size=9,
            weight="normal",
        )
        self.term_button.configure(state="disabled")
        self.term_button.pack(side="right", padx=(0, 8))

        self.auto_open_toggle = tk.Label(
            footer,
            text="",
            bg=BG,
            fg=MUTED,
            font=(FONT, 9),
            cursor="hand2",
        )
        self._refresh_toggle()
        self.auto_open_toggle.bind("<Button-1>", lambda _event: self._toggle_auto_open())
        self.auto_open_toggle.pack(side="right", padx=(0, theme.px(16)))
        self.output_label.pack(
            side="left", fill="x", expand=True, padx=(theme.px(2), theme.px(14))
        )

    def _refresh_toggle(self) -> None:
        box = "☑" if self.auto_open_var.get() else "☐"
        self.auto_open_toggle.configure(text=f"{box}  完成后自动打开")

    def _toggle_auto_open(self) -> None:
        self.auto_open_var.set(not self.auto_open_var.get())
        self._refresh_toggle()

    # ------------------------------------------------------------------ 使用说明

    HELP_SECTIONS = (
        (
            "1 · 选择文件",
            (
                "把 PDF 拖进虚线框，或点右侧「选择文件」。",
                "只支持文本型 PDF；扫描件没有文字层，需要先做 OCR。",
            ),
        ),
        (
            "2 · 输出中文重排版",
            (
                "先解析章节、段落、图表与公式，再按中文版式重排。",
                "输出是一份新文档，页数与原文件不同。",
                "图、表、公式以原图截取保留，图注与表题翻成中文。",
            ),
        ),
        (
            "3 · 选择翻译服务",
            (
                "自然档（默认）：本地 GGUF 模型，全程离线，不需要联网。",
                "在线 API / 模拟：需要填接口地址与 Key；模拟只用于试跑界面。",
            ),
        ),
        (
            "4 · 开始与结果",
            (
                "点「开始翻译」，进度条与状态行会显示当前进度和预计剩余时间。",
                "完成后产物在 out\\results\\<文件名>_zh.pdf，可用「打开输出文件」查看。",
                "「自检报告」会打开 selfcheck.json，含中文覆盖率、术语/数字一致性与排版问题清单。",
            ),
        ),
        (
            "小提示",
            (
                "首次加载模型约 10–20 秒；之后逐段生成，速度取决于 CPU 与段落长度。",
                "参考文献、作者、DOI 等按出版惯例保留英文。",
                "自检不通过时状态行会标红并给出具体原因。",
                "随时按 F1，或点右上角「使用说明」都能打开这一页。",
            ),
        ),
    )

    def _open_help(self) -> None:
        existing = getattr(self, "_help_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        px = theme.px
        win = tk.Toplevel(self.root)
        self._help_window = win
        win.title("使用说明")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)
        win.bind("<Escape>", lambda _event: win.destroy())

        card = tk.Frame(win, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=px(18), pady=(px(18), px(10)))
        holder = tk.Frame(card, bg=CARD)
        holder.pack(fill="both", expand=True, padx=px(24), pady=px(22))

        tk.Label(
            holder,
            text="三步把一篇英文 PDF 翻成中文",
            bg=CARD,
            fg=TEXT,
            font=(FONT, 13, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            holder,
            text="所有步骤都在本机完成，不上传文件；结果会写入 out 目录。",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 9),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(px(4), px(2)))

        for title, lines in self.HELP_SECTIONS:
            tk.Label(
                holder,
                text=title,
                bg=CARD,
                fg=theme.TEXT_SOFT,
                font=(FONT, 9, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(px(14), px(3)))
            for line in lines:
                tk.Label(
                    holder,
                    text="· " + line,
                    bg=CARD,
                    fg=MUTED,
                    font=(FONT, 9),
                    anchor="w",
                    justify="left",
                    wraplength=px(470),
                ).pack(fill="x", pady=(px(1), 0))

        footer = tk.Frame(win, bg=BG)
        footer.pack(fill="x", padx=px(18), pady=(0, px(18)))
        theme.Button(
            footer,
            text="知道了",
            command=win.destroy,
            variant="primary",
            parent_bg=BG,
            height=38,
            padx=22,
            size=9,
            weight="bold",
        ).pack(side="right")
        theme.Button(
            footer,
            text="打开输出目录",
            command=self._open_output_dir,
            variant="ghost",
            parent_bg=BG,
            height=38,
            padx=18,
            size=9,
            weight="normal",
        ).pack(side="right", padx=(0, px(8)))

        win.update_idletasks()
        width = win.winfo_reqwidth()
        height = win.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 3
        win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        win.grab_set()
        win.focus_force()
    def _enable_drop(self, widget: tk.Widget) -> None:
        if not DND_AVAILABLE or DND_FILES is None:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _register_drop_tree(self, widget: tk.Widget) -> None:
        self._enable_drop(widget)
        for child in widget.winfo_children():
            self._register_drop_tree(child)

    def _on_drop(self, event) -> None:
        if self.running:
            return
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        for item in paths:
            if str(item).lower().endswith(".pdf"):
                self.load_pdf(str(item))
                return
        messagebox.showwarning("格式不支持", "请拖入 .pdf 文件。")

    def _toggle_advanced(self, _event: tk.Event | None = None) -> None:
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.advanced_frame.pack(fill="x", padx=2, pady=(10, 2), after=self.advanced_toggle)
            self.advanced_toggle.configure(text="高级设置  ▴")
        else:
            self.advanced_frame.pack_forget()
            self.advanced_toggle.configure(text="高级设置  ▾")
        self.root.after_idle(self._fit_window)

    def _on_engine_change(self) -> None:
        value = self.engine_var.get()
        save_last_engine(value)
        if value == ENGINE_API:
            self.api_frame.pack(fill="x", padx=2, pady=(10, 0), after=self.engine_hint)
            self.engine_hint.configure(text="在线接口 · 需要联网与 API Key（可选通道）")
        else:
            self.api_frame.pack_forget()
            if value in TIER_ORDER:
                hint = f"本地离线 · {tier_title(value)} · 整篇论文约 20–40 分钟"
                hint += f"\n{TIER_NOTES.get(value, '')}"
                self.engine_hint.configure(text=hint)
            elif value == ENGINE_MOCK:
                self.engine_hint.configure(text="调试占位译文，不调用模型")

        if value in TIER_ORDER:
            if not self.model_status_visible:
                self.model_status_frame.pack(
                    fill="x", padx=2, pady=(6, 0), after=self.engine_hint
                )
                self.model_status_visible = True
            self._refresh_model_status(value)
        elif self.model_status_visible:
            self.model_status_frame.pack_forget()
            self.model_status_visible = False
        self._style_choices()
        self.root.after_idle(self._fit_window)

    def _set_engines_enabled(self, enabled: bool) -> None:
        for tier, button in self.tier_radio.items():
            state = "normal" if enabled and tier in self.available_tiers else "disabled"
            button.configure(state=state)
        for button in self.other_radio.values():
            button.configure(state="normal" if enabled else "disabled")

    def _tier_model_dir(self, tier: str) -> Path:
        """运行期模型目录：GGUF int4（models/llm-gguf/）。"""
        model_dir = bundled_model_dir()
        return gguf_model_dir(model_dir) if tier == "llm" else model_dir / tier

    def _tier_available(self, tier: str) -> bool:
        if tier != "llm":
            return False
        return any(gguf_model_dir(bundled_model_dir()).glob("*.gguf"))

    def _tier_size(self, tier: str) -> int:
        model_dir = self._tier_model_dir(tier)
        total = 0
        if not model_dir.exists():
            return 0
        for item in model_dir.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    def _offline_model_state(self, tier: str) -> str:
        return "ready" if self._tier_available(tier) else "missing"

    def _tier_radio_text(self, tier: str, available: bool) -> str:
        title = tier_title(tier)
        if not available:
            label = f"{title} · 未找到模型文件"
            if tier in TIER_NOTES:
                label += f"（{TIER_NOTES[tier]}）"
            return label
        label = f"{title} · {human_size(self.tier_sizes.get(tier, 0))}"
        if tier in TIER_NOTES:
            label += f" · {TIER_NOTES[tier]}"
        return label

    def _refresh_model_status(self, tier: str | None = None) -> None:
        value = self.engine_var.get()
        tier = tier or (value if value in TIER_ORDER else None)
        if not tier:
            return
        state = self._offline_model_state(tier)
        if state == "ready":
            self.model_status_var.set("模型已就绪，可直接开始翻译。")
            return
        self.model_status_var.set(
            f"未找到语言模型：请把 GGUF 文件放到 {gguf_model_dir(bundled_model_dir())}（约 4.36 GB）。"
        )

    def _update_elapsed(self) -> None:
        elapsed = time.perf_counter() - self._run_started if self._run_started else 0.0
        if self._page_index and self._page_total:
            self.detail_var.set(
                f"第 {self._page_index}/{self._page_total} 页 · 已用时 {elapsed:.1f} 秒"
            )
        else:
            total = self._page_total or self.page_count
            self.detail_var.set(f"共 {total} 页 · 已用时 {elapsed:.1f} 秒")

    def _choose_file(self) -> None:
        if self.running:
            return
        path = filedialog.askopenfilename(
            title="选择英文 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if path:
            self.load_pdf(path)

    def load_pdf(self, path: str) -> None:
        if self.running:
            return
        candidate = Path(path)
        if candidate.suffix.lower() != ".pdf":
            messagebox.showwarning("格式不支持", "请选择 .pdf 文件。")
            return
        try:
            with fitz.open(str(candidate)) as doc:
                pages = doc.page_count
                encrypted = doc.needs_pass
            size = candidate.stat().st_size
        except Exception as exc:
            messagebox.showerror("无法打开 PDF", f"文件无法解析：{exc}")
            return
        if encrypted:
            messagebox.showwarning("PDF 已加密", "请先解除密码后再导入。")
            return

        self.pdf_path = candidate.resolve()
        self.page_count = pages
        self.result = None
        self.output_path = results_dir() / f"{candidate.stem}_zh.pdf"
        self.file_name_label.configure(text=elide(candidate.name, 42))
        self.file_meta_label.configure(
            text=f"{pages} 页 · {human_size(size)} · 文本型 PDF"
        )
        self.output_label.configure(text=f"输出位置：{display_path(self.output_path)}")
        self.status_var.set(f"已载入 {candidate.name}，共 {pages} 页。点击「开始翻译」。")
        self.percent_var.set("0%")
        self.progress["value"] = 0
        self.detail_var.set("")
        self.term_report_path = None
        self.term_button.configure(state="disabled")
        self.open_button.configure(state="disabled", fg=MUTED)
        self.running = False

    def _start(self) -> None:
        if self.running:
            return
        if self.pdf_path is None:
            messagebox.showinfo("请选择文件", "请先拖入或选择一个英文 PDF 文件。")
            return

        value = self.engine_var.get()
        tier = value if value in TIER_ORDER else None
        is_api = value == ENGINE_API
        if tier and tier not in self.available_tiers:
            message = (
                f"{tier_title(tier)} 的语言模型还没到位：请把 GGUF 文件放到 "
                f"{gguf_model_dir(bundled_model_dir())}（约 4.36 GB）。"
            )
            self.status_var.set(message)
            if not self.headless:
                messagebox.showinfo("模型未就绪", message)
            return
        try:
            if is_api:
                key = self.api_key_var.get().strip()
                if not key:
                    self.api_entry.focus_set()
                    messagebox.showwarning("缺少 API Key", "在线翻译需要填写 API Key。")
                    return
                provider = create_provider(
                    "openai",
                    api_key=key,
                    base_url=self.base_url_var.get().strip() or DEFAULT_BASE_URL,
                    model=self.model_var.get().strip() or DEFAULT_MODEL,
                )
            elif tier:
                provider = create_offline_provider(
                    settings=OfflineSettings(
                        tier=tier,
                        model_dir=bundled_model_dir(),
                        glossary_path=app_data_path("glossary.tsv"),
                        terms_cache=app_data_path("terms_cache.json"),
                        web_terms=self.web_terms_var.get(),
                    ),
                    notify=lambda message: self.queue.put(("notice", message)),
                )
            else:
                provider = create_provider("mock")
        except TranslationProviderError as exc:
            self.status_var.set(f"配置错误：{exc}")
            if not self.headless:
                messagebox.showerror("配置错误", str(exc))
            return
        except Exception as exc:
            self.status_var.set(f"无法启动引擎：{exc}")
            if not self.headless:
                messagebox.showerror("无法启动引擎", str(exc))
            return

        self.running = True
        self._run_started = time.perf_counter()
        self._page_index = 0
        self._page_total = self.page_count
        self.start_button.configure(state="disabled", bg="#c7ccd4", text="准备中…")
        self._set_engines_enabled(False)
        self.term_button.configure(state="disabled")
        self.open_button.configure(state="disabled", fg=MUTED)
        self.progress["value"] = 0
        self.percent_var.set("0%")
        self.detail_var.set("准备中 · 已用时 0.0 秒")
        self.status_var.set("正在解析 PDF 并准备翻译…")
        self.result = None
        self.term_report_path = None
        if tier:
            self._refresh_model_status(tier)

        output = self.output_path or (results_dir() / f"{self.pdf_path.stem}_zh.pdf")
        thread = threading.Thread(
            target=self._worker,
            args=(provider, self.pdf_path, output),
            daemon=True,
        )
        thread.start()
        self._poll_queue()

    def _worker(self, provider, src: Path, dst: Path) -> None:
        try:
            from pdf_translator.reflow.stages import STAGE_LABELS, run_stage
            from pdf_translator.reflow.workspace import Workspace

            workspace = Workspace.for_source(src)
            started = time.perf_counter()
            for stage in ("structure", "glossary", "translate", "layout", "check"):
                self.queue.put(
                    ("notice", f"正在{STAGE_LABELS.get(stage, stage)}…")
                )
                run_stage(
                    stage,
                    src,
                    provider=provider,
                    progress=lambda done, total, message: self.queue.put(
                        ("progress", (done, total, message))
                    ),
                )

            # 用户若在界面里指定了别的输出路径，把工作区产物复制过去。
            output = workspace.output_pdf
            if dst and Path(dst) != output:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(output, dst)
                output = Path(dst)

            translation = workspace.load("translate") or {}
            layout = workspace.load("layout") or {}
            check = workspace.load("check") or {}
            entries = translation.get("entries", [])
            translated = sum(1 for item in entries if item.get("status") == "ok")
            result = TranslationResult(
                input_path=workspace.source,
                output_path=output,
                provider=provider.describe(),
                pages=int(layout.get("pages") or 0),
                total_units=len(entries),
                translated_units=translated,
                skipped_units=max(0, len(entries) - translated),
                warnings=list(check.get("warnings", [])) + list(check.get("blockers", [])),
                elapsed=time.perf_counter() - started,
                selfcheck_rounds=[{"ok": bool(check.get("ok"))}] if check else [],
                selfcheck_ok=bool(check.get("ok")),
                selfcheck_path=str(workspace.path("check")),
                selfcheck_pages=[],
            )
            report = workspace.path("check") if check else None
            self.queue.put(("done", (result, report)))
        except Exception as exc:  # 展示给用户，不打印堆栈
            self.queue.put(("error", str(exc)))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    done, total, message = payload
                    percent = (done / total * 100) if total else 0
                    self.progress["value"] = percent
                    self.percent_var.set(f"{percent:.0f}%")
                    self.status_var.set(message)
                elif kind == "page":
                    self._page_index, self._page_total = payload
                elif kind == "notice":
                    self.status_var.set(str(payload))
                elif kind == "done":
                    result, term_path = payload
                    self._on_done(result, term_path)
                elif kind == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass
        if self.running:
            self._update_elapsed()
            self.queue_job = self.root.after(100, self._poll_queue)

    def _reset_controls(self) -> None:
        self.running = False
        self.start_button.configure(state="normal", bg=ACCENT, text="开始翻译")
        self._set_engines_enabled(True)

    def _on_done(self, result: TranslationResult, term_path: Path | None = None) -> None:
        self._reset_controls()
        self.result = result
        self.output_path = Path(result.output_path)
        self.progress["value"] = 100
        self.percent_var.set("100%")
        self.detail_var.set(
            f"共 {result.pages} 页 · 已用时 {result.elapsed:.1f} 秒"
        )
        warning_note = f"，{len(result.warnings)} 条排版提示" if result.warnings else ""
        check_note = ""
        if result.selfcheck_rounds:
            rounds = len(result.selfcheck_rounds)
            if result.selfcheck_ok:
                check_note = f" · 自检通过（{rounds} 轮）"
            else:
                page_note = (
                    f"，问题页 {result.selfcheck_pages}" if result.selfcheck_pages else ""
                )
                check_note = f" · 自检未通过（{rounds} 轮{page_note}）"
        self.status_var.set(
            f"翻译完成 · {result.pages} 页 · 翻译 {result.translated_units} 段 · "
            f"跳过 {result.skipped_units} 段 · 用时 {result.elapsed:.1f} 秒{warning_note}{check_note}"
        )
        if result.selfcheck_rounds and not result.selfcheck_ok:
            messagebox.showwarning(
                "产物未通过自检",
                f"{'问题页：' + str(result.selfcheck_pages) if result.selfcheck_pages else '见报告中的阻断项'}\n"
                f"报告：{result.selfcheck_path}\n\n"
                "文件已生成，但排版/行文仍有问题，请按报告复核。",
            )
        self.output_label.configure(text=f"输出文件：{display_path(self.output_path)}")
        self.open_button.configure(state="normal", fg=TEXT, bg=CARD)
        self.term_report_path = term_path if term_path and term_path.exists() else None
        if self.term_report_path:
            self.term_button.configure(state="normal", fg=TEXT, bg=CARD)
        else:
            self.term_button.configure(state="disabled", fg=MUTED)
        if self.engine_var.get() in TIER_ORDER:
            self._refresh_model_status()
        if self.auto_open_var.get():
            self._open_output_dir()

    def _on_error(self, message: str) -> None:
        self._reset_controls()
        self.progress["value"] = 0
        self.percent_var.set("0%")
        self.detail_var.set("")
        self.status_var.set(f"翻译失败：{message}")
        if self.engine_var.get() in TIER_ORDER:
            self._refresh_model_status()
        if not self.headless:
            messagebox.showerror("翻译失败", message)

    def _open_output_dir(self) -> None:
        if not self.output_path:
            return
        directory = self.output_path.parent
        if not directory.exists():
            messagebox.showinfo("目录不存在", "尚未生成输出目录。")
            return
        try:
            os.startfile(str(directory))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def _open_term_report(self) -> None:
        if not self.term_report_path or not self.term_report_path.exists():
            messagebox.showinfo("报告不存在", "本次翻译没有生成自检报告。")
            return
        try:
            os.startfile(str(self.term_report_path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开自检报告", str(exc))

    def _open_output(self) -> None:
        if not self.output_path or not self.output_path.exists():
            messagebox.showinfo("文件不存在", "尚未生成输出文件。")
            return
        try:
            os.startfile(str(self.output_path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开文件", str(exc))

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("正在翻译", "翻译尚未完成，确定要退出吗？"):
                return
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 PDF 翻译界面。")
    parser.add_argument("pdf", nargs="?", help="可选：启动时直接载入的 PDF")
    parser.add_argument("--smoke", action="store_true", help="自检：打开界面后自动关闭")
    parser.add_argument(
        "--auto-translate",
        action="store_true",
        help="自检：载入 PDF 后自动开始翻译，完成后写入报告并退出",
    )
    parser.add_argument("--report", help="自动自检报告的输出路径")
    parser.add_argument(
        "--tiers",
        help="自动自检依次切换的档位，逗号分隔，例如 llm",
    )
    parser.add_argument("--version", action="version", version=f"PDFTranslator {__version__}")
    args = parser.parse_args(argv)

    theme.enable_dpi_awareness()
    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    theme.init_scaling(root)
    app = PDFTranslatorApp(root, args.pdf)
    if args.smoke:
        root.after(1200, root.destroy)
        root.mainloop()
        # 打包后是 windowed 程序，没有 stdout；把结果写文件才可验证。
        report = {
            "ok": True,
            "smoke": True,
            "version": __version__,
            "frozen": bool(getattr(sys, "frozen", False)),
            "app_dir": str(project_root()),
            "available_tiers": list(app.available_tiers),
            "engine": app.engine_var.get(),
            "dnd": DND_AVAILABLE,
        }
        target = (
            Path(args.report)
            if args.report
            else (out_dir() / "reports" / "_smoke.json")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("界面自检通过。")
        return 0
    if args.auto_translate:
        app.headless = True
        app.auto_open_var.set(False)
        tiers = [item.strip() for item in (args.tiers or "").split(",") if item.strip()]
        if not tiers:
            tiers = [app.engine_var.get()]
        runs: list[dict] = []
        index = [0]
        run_started = [time.perf_counter()]
        peak_mb = [0.0]
        exit_code = [0]

        def write_report() -> None:
            report = {
                "ok": bool(runs) and all(run.get("ok") for run in runs),
                "frozen": bool(getattr(sys, "frozen", False)),
                "app_dir": str(project_root()),
                "available_tiers": list(app.available_tiers),
                "tiers_requested": tiers,
                "runs": runs,
            }
            target = (
                Path(args.report)
                if args.report
                else (out_dir() / "reports" / "_selftest.json")
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False))
            exit_code[0] = 0 if report["ok"] else 1
            root.destroy()

        def start_next() -> None:
            if index[0] >= len(tiers):
                write_report()
                return
            tier = tiers[index[0]]
            app.engine_var.set(tier)
            app._on_engine_change()
            app.result = None
            app.term_report_path = None
            stem = Path(app.pdf_path).stem if app.pdf_path else "document"
            app.output_path = results_dir() / f"{stem}_zh_{tier}.pdf"
            run_started[0] = time.perf_counter()
            peak_mb[0] = 0.0
            app._start()
            root.after(300, run_done)

        def run_done() -> None:
            if app.running:
                if time.perf_counter() - run_started[0] > 1800:
                    app.running = False
                    app.status_var.set("自动自检超时：30 分钟内未完成翻译")
                else:
                    # 轮询期间顺带采样工作集，记录该档位的运行峰值
                    peak_mb[0] = max(peak_mb[0], process_memory_mb())
                    root.after(200, run_done)
                    return
            tier = tiers[index[0]]
            result = app.result
            output = app.output_path
            record = {
                "tier": tier,
                "engine": app.engine_var.get(),
                "available": tier in app.available_tiers,
                "model_dir": str(app._tier_model_dir(tier))
                if tier in TIER_ORDER
                else "",
                "input": str(app.pdf_path) if app.pdf_path else "",
                "output": str(output) if output else "",
                "output_exists": bool(output and output.is_file()),
                "terms_exists": bool(
                    app.term_report_path and app.term_report_path.exists()
                ),
                "pages": result.pages if result else 0,
                "translated_units": result.translated_units if result else 0,
                "skipped_units": result.skipped_units if result else 0,
                "elapsed": round(result.elapsed, 2) if result else 0,
                "warnings": len(result.warnings) if result else 0,
                "memory_mb_peak": round(peak_mb[0], 1),
                "memory_mb_after": round(process_memory_mb(), 1),
                "status": app.status_var.get(),
                "ok": bool(result and output and output.is_file()),
            }
            gc.collect()
            root.after(400, lambda: release_done(record))

        def release_done(record: dict) -> None:
            record["memory_mb_after_release"] = round(process_memory_mb(), 1)
            runs.append(record)
            index[0] += 1
            start_next()

        app.engine_var.set(tiers[0])
        app._on_engine_change()
        app.result = None
        app.term_report_path = None
        app.output_path = (
            results_dir() / f"{Path(app.pdf_path).stem}_zh_{tiers[0]}.pdf"
            if app.pdf_path
            else None
        )
        run_started[0] = time.perf_counter()
        root.after(300, lambda: (app._start(), root.after(300, run_done)))
        root.mainloop()
        return exit_code[0]
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
