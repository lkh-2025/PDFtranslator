"""翻译服务工厂。"""

from __future__ import annotations

from ..config import DEFAULT_BASE_URL, DEFAULT_MODEL
from .base import BaseProvider, TranslationProviderError
from .mock import MockProvider
from .offline import OfflineProvider
from .offline_config import OfflineSettings
from .openai_compat import OpenAICompatProvider


def create_offline_provider(
    settings: OfflineSettings | None = None,
    notify=None,
) -> OfflineProvider:
    return OfflineProvider(settings=settings, notify=notify)


def create_provider(
    name: str = "mock",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 90.0,
) -> BaseProvider:
    key = (name or "mock").strip().lower()
    if key in ("mock", "模拟"):
        return MockProvider()
    if key in ("offline", "local", "离线", "离线翻译", "本地"):
        return OfflineProvider()
    if key in ("openai", "api", "openai-compatible"):
        return OpenAICompatProvider(
            api_key=api_key or "",
            base_url=base_url or DEFAULT_BASE_URL,
            model=model or DEFAULT_MODEL,
            timeout=timeout,
        )
    raise TranslationProviderError(f"未知的翻译服务：{name}")
