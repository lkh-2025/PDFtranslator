"""命令行入口：pdf-translator <input.pdf> -o out/results/xxx_zh.pdf"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .config import DEFAULT_BASE_URL, DEFAULT_MODEL
from . import __version__
from .paths import results_dir
from .providers import create_offline_provider, create_provider
from .providers.base import TranslationProviderError
from .providers.offline_config import OfflineSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="英文 PDF 翻译为中文，按中文版式重新排版。"
    )
    parser.add_argument("input", nargs="?", help="输入的英文 PDF 路径")
    parser.add_argument("-o", "--output", help="输出的中文 PDF 路径")
    parser.add_argument("--version", action="version", version=f"PDFTranslator {__version__}")
    parser.add_argument(
        "--check-models",
        action="store_true",
        help="检查本地模型是否可用（只读磁盘，不联网、不翻译）",
    )
    parser.add_argument(
        "--provider",
        default="mock",
        choices=["mock", "openai", "offline"],
        help="翻译服务：mock（离线模拟，默认）、offline（本地 Qwen 自然档）或 openai（OpenAI 兼容接口）",
    )
    parser.add_argument(
        "--tier",
        default="llm",
        choices=["llm"],
        help="离线档位，仅 --provider offline 时生效（默认 llm 自然档，当前唯一随包档位）",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("PDF_TRANSLATOR_API_KEY", ""),
        help="API Key，也可用环境变量 PDF_TRANSLATOR_API_KEY",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PDF_TRANSLATOR_BASE_URL", DEFAULT_BASE_URL),
        help=f"OpenAI 兼容接口地址，默认 {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("PDF_TRANSLATOR_MODEL", DEFAULT_MODEL),
        help=f"模型名称，默认 {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--layout",
        default="reflow",
        choices=["reflow"],
        help="版式：固定为 reflow（中文重排）；原「fill 回填原坐标」模式已下线",
    )
    parser.add_argument(
        "--font",
        default="",
        help="正文字体文件（默认按 宋体→思源宋体→微软雅黑 回退）",
    )
    parser.add_argument(
        "--stage",
        default="all",
        choices=["structure", "glossary", "translate", "layout", "check", "all"],
        help="分阶段执行：structure/glossary/translate/layout/check/all（默认 all）",
    )
    parser.add_argument(
        "--no-selfcheck",
        action="store_true",
        help="跳过产物自检阶段（默认执行自检，不合格时退出码为 1）",
    )
    return parser


def check_models() -> int:
    """只读磁盘的模型自检：列出 GGUF 文件并试加载一次。0=可用，3=缺失，4=加载失败。"""
    from .providers.offline_config import gguf_model_dir, resolve_model_dir

    gguf_dir = gguf_model_dir(resolve_model_dir())
    files = sorted(gguf_dir.glob("*.gguf"))
    if not files:
        print(f"未找到语言模型：{gguf_dir}", file=sys.stderr)
        print("请把 Qwen2.5-7B-Instruct GGUF（约 4.36 GB）放到该目录后重试。", file=sys.stderr)
        return 3
    total = sum(item.stat().st_size for item in files)
    print(f"模型目录：{gguf_dir}")
    for item in files:
        print(f"  {item.name}：{item.stat().st_size:,} 字节")
    print(f"合计：{total:,} 字节（{total / 1024**3:.2f} GB）")
    try:
        from llama_cpp import Llama

        model = Llama(
            model_path=str(files[0]),
            n_ctx=64,
            n_threads=2,
            n_batch=8,
            verbose=False,
        )
        try:
            model.create_completion("a", max_tokens=1)
        finally:
            model.close()
    except Exception as exc:
        print(f"加载检查：失败（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return 4
    print("加载检查：llama.cpp 可加载")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check_models:
        return check_models()

    if not args.input:
        parser.error("需要输入 PDF 路径（或使用 --check-models）")
    src = Path(args.input)
    dst = Path(args.output) if args.output else results_dir() / f"{src.stem}_zh.pdf"

    try:
        if args.provider == "offline":
            provider = create_offline_provider(
                settings=OfflineSettings(tier=args.tier)
            )
        else:
            provider = create_provider(
                args.provider,
                api_key=args.api_key,
                base_url=args.base_url,
                model=args.model,
            )
    except TranslationProviderError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    def on_progress(done: int, total: int, message: str) -> None:
        percent = int(done / total * 100) if total else 100
        print(f"\r[{percent:3d}%] {message}", end="", flush=True)

    print(f"翻译服务：{provider.describe()}")
    print(f"输入文件：{src}")

    from .reflow.cjk import ReflowFontError
    from .reflow.stages import run_stage
    from .reflow.workspace import Workspace

    print("版式：中文重排")
    stages = (
        ["structure", "glossary", "translate", "layout", "check"]
        if args.stage == "all"
        else [args.stage]
    )
    if args.no_selfcheck:
        stages = [stage for stage in stages if stage != "check"]
        if not stages:
            parser.error("--no-selfcheck 与 --stage check 不能同时使用")
    print("阶段：" + " → ".join(stages))
    workspace = Workspace.for_source(src)
    try:
        for stage in stages:
            result = run_stage(
                stage,
                src,
                provider=provider,
                font_file=args.font or None,
                progress=on_progress,
            )
            print()
            print(result.line())
    except ReflowFontError as exc:
        print(f"\n翻译失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 兜底，避免直接崩溃
        print(f"\n翻译失败：{exc}", file=sys.stderr)
        return 1

    produced = Path(workspace.output_pdf) if workspace.output_pdf else None
    if produced and args.output and produced != dst:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(produced, dst)
        print(f"\n输出文件：{dst}")

    if "check" not in stages:
        print(f"\n产物目录：{workspace.root}")
        return 0
    check = workspace.load("check") or {}
    if not check:
        print(f"\n产物目录：{workspace.root}")
        return 0
    print(f"产物：{workspace.output_pdf}")
    print(f"结构树：{workspace.path('structure')}")
    print(f"术语表：{workspace.path('glossary')}")
    print(f"译文：{workspace.path('translate')}")
    print(f"版式：{workspace.path('layout')}")
    print(f"自检：{workspace.path('check')}")
    for blocker in check.get("blockers", []):
        print(f"  阻断：{blocker}")
    return 0 if (check.get("ok") or args.no_selfcheck) else 1


if __name__ == "__main__":
    raise SystemExit(main())
