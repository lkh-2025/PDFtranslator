"""项目路径的唯一来源：仓库根、配置、模型与产物目录。

源码原型一律以仓库根为基准（本文件位于 src/pdf_translator/paths.py，
向上两级就是仓库根）。其它模块需要路径时都从这里取，不再各自拼。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    return PROJECT_ROOT


def config_dir() -> Path:
    return PROJECT_ROOT / "config"


def out_dir() -> Path:
    """产物根目录。

    默认是仓库下的 `out/`；设置 `PDF_TRANSLATOR_OUT_DIR` 可整体改道，
    便于把一次试验的中间 JSON 与 PDF 全部落到临时目录，不覆盖既有产物。
    """
    override = os.environ.get("PDF_TRANSLATOR_OUT_DIR")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    return PROJECT_ROOT / "out"


def results_dir() -> Path:
    return out_dir() / "results"


def reports_dir() -> Path:
    return out_dir() / "reports"


def archive_dir() -> Path:
    return out_dir() / "archive"


def config_file(name: str) -> Path:
    """读取配置文件的路径。

    新位置是 `config/<name>`；若新位置没有、仓库根旧位置还有同名文件，
    就继续读旧文件，避免用户已有的词表 / 缓存 / 界面设置失效。
    写入请用 `config_target()`，统一落到 `config/`。
    """
    preferred = config_dir() / name
    legacy = PROJECT_ROOT / name
    if not preferred.exists() and legacy.exists():
        return legacy
    return preferred


def config_target(name: str) -> Path:
    return config_dir() / name


def config_json_path() -> Path:
    """用户配置文件：%APPDATA%\\PDFTranslator\\config.json（模型目录等持久化设置）。"""
    roaming = os.environ.get("APPDATA")
    base = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
    return base / "PDFTranslator" / "config.json"


def _read_user_config() -> dict:
    try:
        data = json.loads(config_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def default_model_dir() -> Path:
    """默认模型目录：有 D 盘就放 D:\\PDFTranslator\\models，否则退回用户目录。"""
    if sys.platform.startswith("win") and Path("D:/").exists():
        return Path("D:/PDFTranslator/models")
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "PDFTranslator" / "models"


def models_dir() -> Path:
    """模型目录解析：环境变量 > config.json 的 model_dir > 默认值。"""
    override = os.environ.get("PDF_TRANSLATOR_MODEL_DIR")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    configured = str(_read_user_config().get("model_dir") or "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return default_model_dir()


def report_for(dst: Path, suffix: str) -> Path:
    """报告文件的落点：产物在 out/ 下就统一放 out/reports/，否则跟产物同目录。"""
    dst = Path(dst)
    try:
        dst.resolve().relative_to(out_dir().resolve())
    except ValueError:
        return dst.with_name(f"{dst.stem}{suffix}")
    target = reports_dir() / f"{dst.stem}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
