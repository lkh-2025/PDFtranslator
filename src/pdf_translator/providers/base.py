"""翻译服务提供方抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TranslationProviderError(RuntimeError):
    """翻译服务调用失败。"""


class BaseProvider(ABC):
    name = "base"

    def describe(self) -> str:
        return self.name

    def prepare_document(self, texts: list[str]) -> None:
        """可选钩子：整篇文档翻译前调用，用于术语检索/模型预热。"""
        return None

    @abstractmethod
    def translate(self, texts: list[str], target_lang: str = "zh") -> list[str]:
        """翻译一批文本，返回与输入等长的译文列表。"""
        raise NotImplementedError
