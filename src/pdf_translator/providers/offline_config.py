"""离线引擎的硬件探测、模型登记与默认参数。"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import (
    config_file,
    config_json_path,
    models_dir,
)


def config_path() -> Path:
    """用户配置文件位置：%APPDATA%\\PDFTranslator\\config.json。"""
    return config_json_path()


def read_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(updates: dict) -> Path:
    """合并写回 config.json；界面、命令行与脚本共用同一份配置。"""
    target = config_path()
    data = read_config()
    data.update(updates)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def resolve_model_dir() -> Path:
    """唯一的模型目录解析入口（优先级：环境变量 > config.json > 默认值）。

    --check-models、--list-tiers、offline.py 与 ui/app.py 都必须走这里，
    不允许再各自拼路径。
    """
    return models_dir()


def bundled_model_dir() -> Path:
    """兼容旧调用点：等价于 resolve_model_dir()。"""
    return resolve_model_dir()


DEFAULT_GLOSSARY = config_file("glossary.tsv")
DEFAULT_TERMS_CACHE = config_file("terms_cache.json")


def gguf_model_dir(model_dir: Path) -> Path:
    """GGUF int4 模型目录：发行默认路径。"""
    return Path(model_dir) / "llm-gguf"


def _windows_ram_gb() -> float:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys / 1024**3


def _ram_gb() -> float:
    try:
        if sys.platform.startswith("win"):
            return _windows_ram_gb()
        if sys.platform == "darwin":
            import subprocess

            output = subprocess.check_output(["sysctl", "-n", "hw.memsize"])
            return int(output.strip()) / 1024**3
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024**2
    except Exception:
        pass
    return 0.0


def _physical_cores(logical: int) -> int:
    try:
        import psutil

        physical = psutil.cpu_count(logical=False)
        if physical:
            return int(physical)
    except Exception:
        pass
    return max(1, logical // 2)


def _cuda_available() -> bool:
    if importlib.util.find_spec("torch") is None:
        return False
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


@dataclass
class HardwareInfo:
    logical_cores: int
    physical_cores: int
    ram_gb: float
    cuda: bool
    device: str
    threads: int

    @property
    def physical_note(self) -> str:
        return "实测" if _psutil_installed() else "按 SMT 估算"

    def summary(self) -> str:
        gpu = "有 CUDA" if self.cuda else "无 CUDA"
        return (
            f"CPU 逻辑核 {self.logical_cores}（物理核 {self.physical_cores}，{self.physical_note}），"
            f"内存 {self.ram_gb:.1f} GB，{gpu}；默认线程 {self.threads}"
        )

    def as_dict(self) -> dict:
        return {
            "logical_cores": self.logical_cores,
            "physical_cores": self.physical_cores,
            "physical_cores_source": self.physical_note,
            "ram_gb": round(self.ram_gb, 1),
            "cuda": self.cuda,
            "device": self.device,
            "threads": self.threads,
        }


def _psutil_installed() -> bool:
    return importlib.util.find_spec("psutil") is not None


def probe_hardware(device: str = "auto") -> HardwareInfo:
    logical = os.cpu_count() or 1
    physical = _physical_cores(logical)
    ram = _ram_gb()
    cuda = _cuda_available()
    if device == "auto":
        device = "cuda" if cuda else "cpu"
    threads = 4 if device == "cuda" else max(1, min(logical, 16))
    return HardwareInfo(
        logical_cores=logical,
        physical_cores=physical,
        ram_gb=ram,
        cuda=cuda,
        device=device,
        threads=threads,
    )


def default_hf_endpoint() -> str:
    return (
        os.environ.get("PDF_TRANSLATOR_HF_ENDPOINT")
        or os.environ.get("HF_ENDPOINT")
        or "https://hf-mirror.com"
    )


@dataclass
class OfflineSettings:
    tier: str = "llm"
    model_dir: Path = field(default_factory=bundled_model_dir)
    glossary_path: Path | None = field(default_factory=lambda: DEFAULT_GLOSSARY)
    terms_cache: Path = field(default_factory=lambda: DEFAULT_TERMS_CACHE)
    keep_terms: str = "auto"
    # 离线优先：默认不发起任何外部网络请求（维基/Wikidata 查询需显式开启）。
    web_terms: bool = False
    max_web_queries: int = 35
    web_timeout: float = 6.0
    web_probe_timeout: float = 8.0
    wiki_api: str = "https://zh.wikipedia.org/w/api.php"
    wikidata_api: str = "https://www.wikidata.org/w/api.php"
    proxy: str | None = field(default_factory=lambda: os.environ.get("PDF_TRANSLATOR_PROXY"))
    hf_endpoint: str = field(default_factory=default_hf_endpoint)
    device: str = "auto"
    threads: int | None = None
    llm_confirmer: object | None = None
    # LLM 档专用：提示词注入 + 后校验，不走哨兵
    llm_repo: str | None = None
    llm_max_output_tokens: int = 1024
    llm_context_chars: int = 240
    # LLM 档批量解码：1 = 逐段生成（默认）；>1 时按长度分组批处理。
    llm_batch_size: int = 1

    def resolved(self, hardware: HardwareInfo) -> "OfflineSettings":
        self.threads = self.threads or hardware.threads
        return self
