"""离线英译中翻译服务：本地 Qwen2.5-7B（GGUF int4）走 llama.cpp。

不依赖 API Key，也不联网：模型需要事先放在模型目录里，运行期只读本地文件。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable

from .base import BaseProvider, TranslationProviderError
from .offline_config import (
    OfflineSettings,
    probe_hardware,
    gguf_model_dir,
)
from .postprocess import postprocess, strip_term_echo
from .term_resolver import TermResolver, ResolvedTerm
from .terms import (
    Glossary,
    TermEntry,
    TermProtector,
    TermRecord,
)

CJK = r"\u3400-\u4dbf\u4e00-\u9fff"
MAX_CHUNK_CHARS = {"llm": 900}
# LLM 档术语内联嵌回用的常见中文变体（重译 2 次仍被翻掉时回填英文原词）。
TERM_VARIANTS = {
    "ViT": ["视觉变换器", "视觉 Transformer", "视觉Transformer", "视觉变压器"],
    "ViTs": ["视觉变换器", "视觉 Transformer"],
    "UCT": ["置信上限", "上限置信区间"],
    "SOTA": ["最先进", "领先水平"],
    "DeiT": ["数据高效图像变换器"],
    "CNN": ["卷积神经网络"],
    "MLP": ["多层感知机"],
    "ImageNet": ["图像网"],
    "IoT": ["物联网"],
    "RAM": ["随机存取存储器", "内存"],
}
# 这些“术语”只是缩写或普通复合词，中文里本来就是译出来的，
# 强制英文原样出现只会制造无意义重试（e.g、i.e、Test-Time、On-chip）。
KEEP_ABBREV_RE = re.compile(r"^(?:e\.g|i\.e|etc|vs|cf|al)\.?$", re.IGNORECASE)


def _worth_keeping(term: str) -> bool:
    """判断后校验是否值得强制该词原样保留英文。"""
    value = (term or "").strip()
    if not value or KEEP_ABBREV_RE.match(value):
        return False
    if "-" in value or "–" in value:
        parts = re.split(r"[-–]", value)
        # 至少一段是缩写/驼峰/含数字（DeepCompress-ViT、ViT-model、GPT-4）
        return any(
            sum(1 for ch in part if ch.isupper()) >= 2
            or any(ch.isdigit() for ch in part)
            for part in parts
        )
    return True


SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"\'(@\[])')
ABBREVIATION_RE = re.compile(
    r"(?:Mr|Mrs|Ms|Dr|Prof|St|Inc|Ltd|Co|Corp|Jr|Sr|vs|etc|No|Fig|Eq|Ref|Sec|Ch|Vol|U\.S|U\.K)\.$",
    re.IGNORECASE,
)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class _Backend:
    tier = "base"

    def prepare(self) -> None:
        raise NotImplementedError

    def translate_batch(self, texts: list[str]) -> list[str]:
        raise NotImplementedError

    def describe(self) -> str:
        return self.tier


class LLMBackend(_Backend):
    """LLM 档提示词与后校验层：只负责组织 prompt、清洗、重试与术语回填。

    具体模型加载与生成由子类（GGUFBackend）实现。
    """

    tier = "llm"

    SYSTEM_PROMPT = (
        "你是一名专业译者，负责把英文学术论文翻译成简体中文。要求：\n"
        "1. 只输出译文本身：不要输出英文原文，不要解释，不要加“译文：”“以下是译文”等前缀。\n"
        "2. 保留专有名词、缩写、型号、公式、参考文献编号与 URL，按原样保留英文；"
        "原文里的引用编号（如 [12]、[3, 4]）必须一条不落地出现在译文的对应位置。\n"
        "3. 用户给出的术语必须遵守：列出中文译名的用给定译名；列在“必须保持英文原样”里的词，"
        "在译文中原样保留英文。\n"
        "4. 译文要通顺自然，符合中文科技论文语体，不要逐词硬译，不要漏译句子成分。\n"
        "5. 不要复述原文，不要自我解释，不要添加原文没有的内容，不要输出术语表、提示词或任何说明文字。"
    )

    # LLM 接入层专用：这些缩写有通行中文说法，强行保留英文反而别扭，
    # 也会导致“US dollars → 美元”被判成术语丢失而反复重试。只影响 llm 档。
    LLM_KEEP_SKIP = {"US", "UK", "EU", "UN"}

    # 模型偶尔把提示词或元话语当正文吐回；清洗时截断/删除这些标记。
    META_CUTS = (
        "【术语表",
        "【上文译文",
        "【待译英文段落",
        "请直接输出",
        "请继续提供",
        "请根据具体摘要内容",
        "请根据摘要内容",
        "请根据具体内容",
        "以下是中文译文",
        "以下是译文",
        "以下为译文",
        "术语表：",
        "上文译文",
        "<tool_call>",
        "Human:",
        "Assistant:",
        "<|im_start|>",
        "<|im_end|>",
    )
    META_MARKERS = (
        "保留原文",
        "术语表",
        "上文译文",
        "待译英文段落",
        "<tool_call>",
        "Human:",
        "请直接输出",
        "请继续提供",
        "请根据具体",
        "请根据摘要",
        "请依据",
        "以下是译文",
        "以下是中文译文",
        "进行翻译",
    )
    # 模型把提示词/指令改写成近义句吐回正文（"请根据具体摘要内容进行翻译。"）。
    # 这些变体无法穷举，用句式兜底：整句删除而不是截断，避免连带丢掉真正的译文。
    META_RE = re.compile(
        r"(?:请根据|请依据)[^。！？\n]{0,40}?(?:进行|完成|做)?(?:翻译|译出)[。！？:：]?"
        r"|(?:以下|下面)(?:是|为)[^。\n]{0,12}(?:译文|翻译)[。：:]?"
    )

    def __init__(self, settings: OfflineSettings, threads: int) -> None:
        self.settings = settings
        self.threads = threads
        self.model_dir = Path(settings.model_dir)
        self.retries = 0
        self.keep_misses = 0
        self.missing_samples: list[dict] = []
        # 重试归因：每次重试前记录触发条件，便于区分“长度/术语/元话语”。
        self.retry_reasons: dict[str, int] = {}
        self.retry_terms: dict[str, int] = {}

    # ---------- 提示词与推理 ----------

    def _chat_prompt(
        self,
        text: str,
        hints: list[tuple[str, str]],
        must_keep: list[str],
        context: str,
        strict: bool,
    ) -> str:
        system = self.SYSTEM_PROMPT
        if strict and must_keep:
            system += (
                "\n6. 再次强调：以下专名必须原样保留英文，不得翻译成中文："
                + "、".join(must_keep[:20])
                + "。"
            )
        parts: list[str] = []
        if hints:
            parts.append(
                "【术语（必须采用）】\n"
                + "\n".join(f"{source} → {target}" for source, target in hints[:30])
            )
        if must_keep:
            parts.append(
                "【必须保持英文原样】\n" + "、".join(must_keep[:20])
            )
        if context:
            parts.append("【上文译文（仅供衔接语气与术语，不要重复翻译）】\n" + context)
        parts.append(
            "【待译英文段落】\n" + text + "\n\n请直接输出这段英文对应的简体中文译文。"
        )
        user = "\n\n".join(parts)
        # Qwen2.5 的 ChatML 模板（运行期不依赖 transformers 的 apply_chat_template）
        return (
            f"<|im_start|>system\n{system}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n"
            "<|im_start|>assistant\n"
        )

    def _generate(self, prompt: str, source_len: int) -> str:
        return self._generate_many([prompt], [source_len])[0]

    def _generate_many(self, prompts: list[str], source_lens: list[int]) -> list[str]:
        raise NotImplementedError("LLM 后端必须实现 _generate_many")

    @staticmethod
    def _squash(text: str) -> str:
        return re.sub(r"\s+", "", text).lower()

    def _kept_terms_ok(self, translated: str, must_keep: list[str]) -> list[str]:
        squashed = self._squash(translated)
        return [
            term
            for term in must_keep
            if self._squash(term) not in squashed
        ]

    @classmethod
    def _has_meta(cls, text: str) -> bool:
        for marker in cls.META_MARKERS:
            start = 0
            while True:
                index = text.find(marker, start)
                if index < 0:
                    break
                # “未进行翻译/不进行翻译”是正文语义，不是提示词泄漏；
                # 旧实现会把整句切掉，导致译文只剩前半句。
                if marker == "进行翻译" and index > 0 and text[index - 1] in "未不":
                    start = index + len(marker)
                    continue
                return True
        return bool(cls.META_RE.search(text))

    def _bad_reasons(
        self, translated: str, text: str, must_keep: list[str]
    ) -> list[str]:
        """后校验失败原因（按优先级分类，供重试归因与日志使用）。"""
        reasons: list[str] = []
        if not translated.strip():
            reasons.append("empty")
        # 提示词/元话语泄漏：模型把“保留原文”“术语表”等指令当正文输出
        if self._has_meta(translated):
            reasons.append("meta")
        cjk = len(re.findall(f"[{CJK}]", translated))
        latin = len(re.findall(r"[A-Za-z]", translated))
        # 复述原文或整段没翻：没有中文，或拉丁字母远多于中文
        if cjk == 0:
            reasons.append("no_cjk")
        elif latin > max(20, cjk * 5):
            reasons.append("latin_heavy")
        # 明显截断：中文只有原文的 1/6 以下（中文字符数天然约为英文的 1/5～1/4）。
        # 原文里被原样保留的专名不计入期望长度，否则专名密集的图注会被误判。
        kept_len = sum(len(term) for term in must_keep if term in text)
        expected = max(0, len(text) - kept_len)
        if expected >= 40 and cjk < max(4.0, expected / 6.0):
            reasons.append("short")
        # 专名未按规则保留
        if self._kept_terms_ok(translated, must_keep):
            reasons.append("terms")
        # 句末回吐「必须保持英文原样」清单：提示词泄漏，重译一次通常就好。
        if strip_term_echo(translated, must_keep) != translated:
            reasons.append("echo")
        return reasons

    def _bad_output(self, translated: str, text: str, must_keep: list[str]) -> bool:
        return bool(self._bad_reasons(translated, text, must_keep))

    def _clean(self, translated: str, text: str) -> str:
        output = translated.strip()
        # 提示词改写式泄漏（"请根据具体摘要内容进行翻译。"）整句删除；
        # 不能直接按标记截断，否则会把紧随其后的真正译文一起丢掉。
        output = self.META_RE.sub("", output).strip()
        # 指令/提示词泄漏：截断到第一个元话语标记之前
        for cut in self.META_CUTS:
            index = output.find(cut)
            if index >= 0:
                output = output[:index]
        output = output.replace("（保留原文）", "").replace("保留原文", "")
        output = re.sub(
            r"^(?:译文|中文译文|简体中文译文|翻译)\s*[:：]\s*", "", output
        )
        output = output.strip().strip("\"'“”‘’")
        # 模型偶尔会把待译段落抄一遍再给译文，去掉开头的整段复述
        first = text.strip()[:60]
        if first and output.startswith(first):
            output = output[len(first) :].lstrip(" ：:>\n")
        # 去掉结尾重复的英文原文（可能重复两三次）
        source = " ".join(text.split())
        for _ in range(3):
            candidate = output.rstrip()
            if source and candidate.lower().endswith(source.lower()):
                output = candidate[: -len(source)]
            else:
                break
        if output.startswith("【"):
            output = re.sub(r"^【[^】]*】\s*", "", output)
        return output.strip().strip("：: \t\n")

    def translate_paragraph(
        self,
        text: str,
        hints: list[tuple[str, str]],
        must_keep: list[str],
        context: str = "",
    ) -> str:
        return self.translate_paragraphs(
            [{"text": text, "hints": hints, "must_keep": must_keep, "context": context}]
        )[0]

    def translate_paragraphs(self, items: list[dict]) -> list[str]:
        """批量翻译多段：items 每项含 text / hints / must_keep / context。

        按原文长度排序后分组，尽量让同组长度接近，避免短段被长段的
        padding 拖慢；生成后逐段做与单段一致的清洗、重试与术语回填。
        """
        if not items:
            return []
        prompts = [
            self._chat_prompt(
                item["text"],
                item.get("hints") or [],
                item.get("must_keep") or [],
                item.get("context") or "",
                strict=False,
            )
            for item in items
        ]
        results = [""] * len(items)
        order = sorted(range(len(items)), key=lambda i: len(items[i]["text"]))
        batch = max(1, int(self.settings.llm_batch_size or 1))
        for start in range(0, len(order), batch):
            group = order[start : start + batch]
            raws = self._generate_many(
                [prompts[index] for index in group],
                [len(items[index]["text"]) for index in group],
            )
            for index, raw in zip(group, raws):
                results[index] = self._clean(raw, items[index]["text"])
        for index, item in enumerate(items):
            results[index] = self._finalize(
                item["text"],
                item.get("hints") or [],
                item.get("must_keep") or [],
                item.get("context") or "",
                results[index],
            )
        return results

    def _finalize(
        self,
        text: str,
        hints: list[tuple[str, str]],
        must_keep: list[str],
        context: str,
        translated: str,
    ) -> str:
        # 术语类失败先就地回填：被翻成“视觉 Transformer”的 ViT 按已知变体换回英文，
        # 后校验仍然要求该词原样出现（没有放宽），只是不再为它白跑两次生成。
        missing = self._kept_terms_ok(translated, must_keep)
        if missing:
            translated = self._inline_terms(translated, missing)

        # 后校验不过最多用强指令重译 2 次（元话语/复述原文/明显截断/术语仍缺）。
        attempt = 0
        while self._bad_output(translated, text, must_keep) and attempt < 2:
            for reason in self._bad_reasons(translated, text, must_keep):
                self.retry_reasons[reason] = self.retry_reasons.get(reason, 0) + 1
            for term in self._kept_terms_ok(translated, must_keep):
                self.retry_terms[term] = self.retry_terms.get(term, 0) + 1
            strict_prompt = self._chat_prompt(text, hints, must_keep, context, strict=True)
            retried = self._clean(self._generate(strict_prompt, len(text)), text)
            attempt += 1
            self.retries += 1
            if retried.strip() and (
                not self._bad_output(retried, text, must_keep)
                or len(self._kept_terms_ok(retried, must_keep))
                < len(self._kept_terms_ok(translated, must_keep))
            ):
                translated = retried
        if self._has_meta(translated):
            # 两次重译仍把指令当正文：删除元话语句；删不干净则截断到标记前。
            translated = self._clean(translated, text)
            if self._has_meta(translated):
                cut = min(
                    (translated.find(marker) for marker in self.META_MARKERS if marker in translated),
                    default=-1,
                )
                if cut >= 0:
                    translated = translated[:cut].strip()
        # 重试后仍残留的术语清单（“代码可在此获取：A、B、C…”）整段砍掉。
        translated = strip_term_echo(translated, must_keep)
        missing = self._kept_terms_ok(translated, must_keep)
        if missing:
            # 两次重译仍失败：按术语内联嵌回，保证保留词原样出现，并记入审计。
            translated = self._inline_terms(translated, missing)
            still = self._kept_terms_ok(translated, must_keep)
            self.keep_misses += len(still)
            if len(self.missing_samples) < 20:
                self.missing_samples.append(
                    {
                        "source": " ".join(text.split())[:200],
                        "missing": missing[:8],
                        "result": " ".join(translated.split())[:200],
                        "action": "inline" if not still else "unresolved",
                    }
                )
        return translated

    @staticmethod
    def _inline_terms(translated: str, missing: list[str]) -> str:
        """把被翻掉的保留词按已知中文变体原地换回英文；无变体时不再句末硬补。"""
        output = translated
        for term in missing:
            for variant in TERM_VARIANTS.get(term, []):
                if variant in output:
                    output = output.replace(variant, term, 1)
                    break
        return output

    def describe(self) -> str:
        return f"离线LLM档（{self.threads} 线程）"


class GGUFBackend(LLMBackend):
    """LLM 档 GGUF int4 后端（llama.cpp）。发行版默认走这里。"""

    tier = "llm"

    def __init__(self, settings: OfflineSettings, threads: int) -> None:
        super().__init__(settings, threads)
        self.gguf_dir = gguf_model_dir(self.model_dir)
        self._llm = None

    def prepare(self) -> None:
        if self._llm is not None:
            return
        shards = sorted(self.gguf_dir.glob("*.gguf"))
        if not shards:
            raise TranslationProviderError(
                f"未找到语言模型：{self.gguf_dir}\n"
                "请把 Qwen2.5-7B-Instruct GGUF（约 4.36 GB）放到该目录后重试。"
            )
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # 打包漏带依赖时给出明确指引
            hint = (
                "请重新运行安装程序修复缺失组件。"
                if getattr(sys, "frozen", False)
                else "开发者请执行 pip install -r requirements-runtime.txt。"
            )
            raise TranslationProviderError(
                f"当前环境缺少 llama-cpp-python，无法加载 GGUF 模型。{hint}"
            ) from exc
        # 分片模型只需传第一片，llama.cpp 会按命名自动找后续分片。
        first = shards[0]
        try:
            self._llm = Llama(
                model_path=str(first),
                n_ctx=4096,
                n_threads=max(1, self.threads),
                n_batch=256,
                verbose=False,
            )
        except Exception as exc:
            raise TranslationProviderError(f"GGUF 模型加载失败：{exc}") from exc
        _stderr(f"LLM 档就绪：{self.describe()}")

    def _generate_many(self, prompts: list[str], source_lens: list[int]) -> list[str]:
        results: list[str] = []
        for prompt, source_len in zip(prompts, source_lens):
            limit = min(
                self.settings.llm_max_output_tokens, max(64, int(source_len * 2.0) + 32)
            )
            payload = self._llm(
                prompt,
                max_tokens=limit,
                temperature=0.0,
                top_p=1.0,
                repeat_penalty=1.05,
                stop=["<|im_end|>", "<|im_start|>"],
                echo=False,
            )
            results.append((payload["choices"][0]["text"] or "").strip())
        return results

    def describe(self) -> str:
        return (
            "离线LLM档 Qwen2.5-7B-Instruct（GGUF Q4_K_M int4，"
            f"{self.threads} 线程）"
        )


class OfflineProvider(BaseProvider):
    name = "离线翻译"

    def __init__(
        self,
        settings: OfflineSettings | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings or OfflineSettings()
        self._notify = notify or _stderr
        self._hardware = probe_hardware(self.settings.device)
        self.settings.resolved(self._hardware)
        # 让后续 describe/日志显示真实设备（cpu / cuda），而不是 AUTO。
        self.settings.device = self._hardware.device
        self._backend: _Backend | None = None
        self._protector = TermProtector()
        self._resolver: TermResolver | None = None
        self._resolved: list[ResolvedTerm] = []
        self._prepared = False
        self._cache: dict[str, str] = {}
        self._records: dict[tuple[str, str], TermRecord] = {}
        self._warnings: list[str] = []
        self._glossary_entries: list[TermEntry] = []
        self._llm_context = ""

    # ---------- 准备 ----------

    def prepare_document(self, texts: list[str]) -> None:
        """整篇文档开始前：术语检索、词表加载、模型加载。"""
        if self._prepared:
            return
        if self.settings.tier != "llm":
            raise TranslationProviderError(
                f"未知离线档位：{self.settings.tier}。当前版本只提供自然档（llm）。\n"
                "请确认 --tier llm，并把 GGUF 模型放到模型目录后重试。"
            )
        self._prepared = True
        self.settings.resolved(self._hardware)
        self._notify(f"硬件探测：{self._hardware.summary()}")

        glossary = Glossary(self.settings.glossary_path)
        self._glossary_entries = glossary.entries
        if glossary.entries:
            self._notify(f"已加载用户词表 {len(glossary.entries)} 条：{self.settings.glossary_path}")

        self._resolver = TermResolver(
            cache_path=self.settings.terms_cache,
            enabled=self.settings.web_terms,
            max_queries=self.settings.max_web_queries,
            timeout=self.settings.web_timeout,
            probe_timeout=self.settings.web_probe_timeout,
            wiki_api=self.settings.wiki_api,
            wikidata_api=self.settings.wikidata_api,
            proxy=self.settings.proxy,
            llm_confirmer=self.settings.llm_confirmer,
        )
        candidates = self._resolver.collect_candidates(texts)
        self._resolved = self._resolver.resolve(candidates, skip_terms=glossary.sources)

        resolved_entries: list[TermEntry] = []
        for item in self._resolved:
            if item.origin in ("quota", "disabled", "network"):
                continue
            resolved_entries.append(
                TermEntry(
                    source=item.term,
                    target=item.target if item.decision == "translate" else "",
                    kind="resolved",
                    source_url=item.source,
                    confidence=item.confidence,
                )
            )
        self._protector = TermProtector(
            glossary_entries=glossary.entries,
            resolved_entries=resolved_entries,
            keep_terms=self.settings.keep_terms,
        )

        self._backend = GGUFBackend(self.settings, self.settings.threads or 1)
        self._backend.prepare()

    # ---------- 翻译 ----------

    def translate(self, texts: list[str], target_lang: str = "zh") -> list[str]:
        """LLM 档批量翻译：术语走提示词注入，生成后做后校验。"""
        if not self._prepared:
            self.prepare_document(texts)
        if self._backend is None:
            raise TranslationProviderError("离线引擎尚未准备好。")

        outputs: list[str | None] = [None] * len(texts)
        jobs: list[tuple[int, str]] = []
        for index, text in enumerate(texts):
            if not text or not text.strip():
                outputs[index] = ""
                continue
            cached = self._cache.get(text)
            if cached is not None:
                outputs[index] = cached
                continue
            if (self.settings.llm_batch_size or 1) > 1:
                jobs.append((index, text))
            else:
                outputs[index] = self._translate_llm_text(text)
        if jobs:
            self._translate_llm_jobs(jobs, outputs)
        return [value if value is not None else "" for value in outputs]

    def _llm_hints(
        self, text: str, records: list[TermRecord]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """从词表/检索结果生成提示词注入项，并复用自动保留规则得到后校验清单。"""
        lowered = text.lower()
        hints: list[tuple[str, str]] = []
        must_keep: list[str] = []
        seen: set[str] = set()
        for entry in self._glossary_entries:
            source = entry.source.strip()
            if not source or source.lower() not in lowered:
                continue
            key = source.lower()
            if key in seen:
                continue
            seen.add(key)
            target = entry.target.strip()
            if target:
                hints.append((source, target))
            else:
                must_keep.append(source)
        for item in self._resolved:
            if item.decision != "translate" or not item.target:
                continue
            key = item.term.lower()
            if key in seen or key not in lowered:
                continue
            seen.add(key)
            hints.append((item.term, item.target))
        for record in records:
            term = record.term
            if "保留原文" not in record.disposition:
                continue
            if not _worth_keeping(term):
                continue
            if term.upper() in LLMBackend.LLM_KEEP_SKIP:
                continue
            if any(term.lower() == kept.lower() for kept in must_keep):
                continue
            must_keep.append(term)
        return hints, must_keep

    def _translate_llm_text(self, text: str) -> str:
        pieces: list[str] = []
        for chunk in self._chunk(text):
            # 术语与后校验必须按“实际发送给模型的块”生成，
            # 否则整段里的术语会被要求出现在不含它的分块里，造成必然重试。
            protected = self._protector.protect(chunk)
            self._remember(protected.records)
            hints, must_keep = self._llm_hints(chunk, protected.records)
            raw = self._backend.translate_paragraph(
                chunk, hints, must_keep, self._llm_context
            )
            if raw.strip():
                cleaned = postprocess(raw)
                pieces.append(cleaned)
                limit = self.settings.llm_context_chars
                self._llm_context = cleaned[-limit:] if limit > 0 else ""
        if not pieces:
            self._warnings.append(f"LLM 未产出译文，保留原文：{text[:40]}...")
            return text
        result = self._join(pieces)
        self._cache[text] = result
        return result

    def _translate_llm_jobs(
        self, jobs: list[tuple[int, str]], outputs: list[str | None]
    ) -> None:
        """LLM 档批量翻译：按文档顺序分波，波内共享波开始时的上文译文。

        批处理会打断「上一段译文」的逐段依赖，因此上文按波更新；
        这是批处理与逐段翻译唯一的语义差异。
        """
        batch = max(1, int(self.settings.llm_batch_size or 1))
        for start in range(0, len(jobs), batch):
            wave = jobs[start : start + batch]
            items: list[dict] = []
            owners: list[int] = []
            for index, text in wave:
                for chunk in self._chunk(text):
                    protected = self._protector.protect(chunk)
                    self._remember(protected.records)
                    hints, must_keep = self._llm_hints(chunk, protected.records)
                    items.append(
                        {
                            "text": chunk,
                            "hints": hints,
                            "must_keep": must_keep,
                            "context": self._llm_context,
                        }
                    )
                    owners.append(index)
            translations = self._backend.translate_paragraphs(items)
            grouped: dict[int, list[str]] = {}
            for owner, piece in zip(owners, translations):
                grouped.setdefault(owner, []).append(piece)
            for index, text in wave:
                pieces = [
                    postprocess(piece)
                    for piece in grouped.get(index, [])
                    if piece.strip()
                ]
                if not pieces:
                    self._warnings.append(f"LLM 未产出译文，保留原文：{text[:40]}...")
                    outputs[index] = text
                    self._cache[text] = text
                    continue
                result = self._join(pieces)
                outputs[index] = result
                self._cache[text] = result
                limit = self.settings.llm_context_chars
                self._llm_context = pieces[-1][-limit:] if limit > 0 else ""

    def _chunk(self, text: str) -> list[str]:
        max_chars = MAX_CHUNK_CHARS.get(self.settings.tier, 400)
        normalized = re.sub(r"\s+", " ", text).strip()
        sentences = SENTENCE_SPLIT_RE.split(normalized)
        merged: list[str] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if merged:
                previous = merged[-1]
                if (
                    len(previous) + 1 + len(sentence) <= max_chars
                    and not ABBREVIATION_RE.search(previous)
                ):
                    merged[-1] = previous + " " + sentence
                    continue
            merged.append(sentence)
        return merged or [normalized]

    @staticmethod
    def _join(parts: list[str]) -> str:
        output = ""
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if not output:
                output = part
                continue
            if re.search(f"[{CJK}]$", output) or re.match(f"[{CJK}]", part):
                output += part
            else:
                output += " " + part
        return output

    # ---------- 记录与报告 ----------

    def _remember(self, records: list[TermRecord]) -> None:
        for record in records:
            key = (record.term.lower(), record.kind)
            self._records.setdefault(key, record)

    def term_report(self) -> dict:
        resolved = [
            {
                "term": item.term,
                "decision": item.decision,
                "target": item.target,
                "source": item.source,
                "confidence": round(item.confidence, 2),
                "origin": item.origin,
                "count": item.count,
            }
            for item in self._resolved
        ]
        protected = [
            {
                "term": record.term,
                "kind": record.kind,
                "disposition": record.disposition,
                "target": record.target,
                "source": record.source_url,
                "confidence": round(record.confidence, 2),
            }
            for record in self._records.values()
        ]
        report = {
            "provider": self.describe(),
            "hardware": self._hardware.as_dict(),
            "tier": self.settings.tier,
            "web_terms": self.settings.web_terms,
            "queries_used": self._resolver.queries_used if self._resolver else 0,
            "max_queries": self.settings.max_web_queries,
            "network_errors": self._resolver.network_errors if self._resolver else 0,
            "resolved": resolved,
            "protected": protected,
            "warnings": self._warnings,
        }
        if isinstance(self._backend, LLMBackend):
            report["llm"] = {
                "model": "Qwen2.5-7B-Instruct",
                "retries": self._backend.retries,
                "keep_term_misses": self._backend.keep_misses,
                "missing_samples": self._backend.missing_samples,
            }
        return report

    def translate_segments(self, items: list[dict]) -> list[str]:
        """reflow 阶段③：按段落翻译，显式携带上下文与术语。

        items 每项含 text / hints / must_keep / context。
        """
        if not items:
            return []
        texts = [str(item.get("text") or "") for item in items]
        if not self._prepared:
            self.prepare_document(texts)
        backend = self._backend
        if backend is not None and hasattr(backend, "translate_paragraphs"):
            return backend.translate_paragraphs(items)
        return self.translate(texts)

    def describe(self) -> str:
        if self._backend:
            return self._backend.describe()
        return "离线LLM档（尚未加载模型）"
