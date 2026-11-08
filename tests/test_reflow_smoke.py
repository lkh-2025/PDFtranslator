"""最小回归：不依赖本地大模型，跑通 reflow 五阶段。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fitz

from pdf_translator.providers.mock import MockProvider
from pdf_translator.reflow.cjk import ReflowFontError, font_path
from pdf_translator.reflow.stages import run_stage
from pdf_translator.reflow.workspace import Workspace


class ReflowSmokeTest(unittest.TestCase):
    def test_five_stages_with_mock_provider(self) -> None:
        try:
            font_path()
        except ReflowFontError as exc:
            self.skipTest(f"本机没有可嵌入的中文字体：{exc}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            os.environ["PDF_TRANSLATOR_OUT_DIR"] = str(tmp_path / "out")
            try:
                source = tmp_path / "demo.pdf"
                document = fitz.open()
                page = document.new_page()
                page.insert_text(
                    (72, 90),
                    "Abstract",
                    fontsize=16,
                    fontname="helv",
                )
                page.insert_textbox(
                    fitz.Rect(72, 110, 520, 240),
                    "This paper studies model compression for vision transformers. "
                    "We evaluate the method on several benchmarks and report the results.",
                    fontsize=11,
                    fontname="helv",
                )
                document.save(source)
                document.close()

                workspace = Workspace.for_source(source)
                provider = MockProvider()
                for stage in ("structure", "glossary", "translate", "layout", "check"):
                    run_stage(stage, source, provider=provider)

                for stage in ("structure", "glossary", "translate", "layout", "check"):
                    self.assertTrue(
                        workspace.has(stage), f"缺少阶段产物：{workspace.path(stage)}"
                    )
                self.assertTrue(workspace.output_pdf.is_file(), "没有生成中文 PDF")
                translation = workspace.load("translate") or {}
                self.assertGreater(len(translation.get("entries", [])), 0)
                check = workspace.load("check") or {}
                self.assertIn("ok", check)
            finally:
                os.environ.pop("PDF_TRANSLATOR_OUT_DIR", None)


if __name__ == "__main__":
    unittest.main()
