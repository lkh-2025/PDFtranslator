"""OpenAI 兼容 Chat Completions 翻译服务。"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from .base import BaseProvider, TranslationProviderError

_CHAT_SUFFIX_RE = re.compile(r"/chat/completions$", re.IGNORECASE)
_VERSION_RUN_RE = re.compile(r"(?i)(/v\d+(?:alpha|beta)?)(?:/v\d+(?:alpha|beta)?)+$")


def normalize_base_url(base_url: str) -> str:
    """规范化 OpenAI 兼容 base_url。

    - 去掉尾部斜杠与 `/chat/completions` 后缀
    - 合并重复版本前缀（`/v1/v1` → `/v1`），避免拼出 `/v1/v1/chat/completions`
    - 纯域名自动补 `/v1`（OpenAI SDK 需要）；已带 `/v1` 或自定义路径（如 `/beta`）原样保留
    """
    url = (base_url or "").strip().strip("'\"")
    url = _CHAT_SUFFIX_RE.sub("", url.rstrip("/")).rstrip("/")
    if not url:
        return ""
    if not re.match(r"https?://", url, flags=re.IGNORECASE):
        url = "https://" + url

    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    path = _VERSION_RUN_RE.sub(r"\1", path)
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, parts.fragment))


SYSTEM_PROMPT = (
    "你是专业的英译中 PDF 排版翻译引擎。用户会给出若干带编号的英文文本片段，"
    "请把它们翻译成简体中文。要求："
    "1) 忠实原意，语气专业，术语在同一批次内保持一致；"
    "2) 数字、公式、URL、邮箱、代码标识符原样保留；"
    "3) 不要添加解释、注释或前后缀，不要合并或拆分片段；"
    "4) 只输出一个严格的 JSON 对象，键是片段编号（字符串），值是中文译文。"
)


class OpenAICompatProvider(BaseProvider):
    name = "OpenAI 兼容 API"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 90.0,
    ) -> None:
        if not api_key.strip():
            raise TranslationProviderError("缺少 API Key，请填写后再使用在线翻译。")
        self.api_key = api_key.strip()
        self.base_url = normalize_base_url(base_url)
        if not self.base_url:
            raise TranslationProviderError("接口地址不能为空，请填写 base_url。")
        self.model = model.strip()
        self.timeout = timeout

    def describe(self) -> str:
        return f"{self.name}（{self.model}）"

    def _endpoint(self) -> str:
        # base_url 已在构造时规范化，版本前缀最多出现一次
        return self.base_url + "/chat/completions"

    def _request(self, items: dict[str, str]) -> str:
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"items": items}, ensure_ascii=False
                    ),
                },
            ],
        }
        response = requests.post(
            self._endpoint(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:400]
            raise TranslationProviderError(
                f"接口返回 HTTP {response.status_code}：{detail}"
            )
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationProviderError(
                f"接口响应格式异常：{str(data)[:300]}"
            ) from exc
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content)

    @staticmethod
    def _parse(content: str, count: int) -> list[str]:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()

        payload = None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            left, right = text.find("{"), text.rfind("}")
            if left != -1 and right > left:
                try:
                    payload = json.loads(text[left : right + 1])
                except json.JSONDecodeError:
                    payload = None

        if isinstance(payload, dict):
            raw = payload.get("items", payload)
            if isinstance(raw, list):
                values = [str(item) for item in raw]
            elif isinstance(raw, dict):
                values = [
                    str(raw.get(str(i), raw.get(i, ""))) for i in range(1, count + 1)
                ]
            else:
                values = []
            if len(values) == count and all(values):
                return values

        if isinstance(payload, list) and len(payload) == count:
            return [str(item) for item in payload]

        numbered = re.findall(
            r"^\s*(?:item\s*)?(\d+)\s*[.:：、)]\s*(.+)$", text, flags=re.MULTILINE
        )
        if len(numbered) == count:
            ordered = {int(num): val.strip() for num, val in numbered}
            if all(i in ordered for i in range(1, count + 1)):
                return [ordered[i] for i in range(1, count + 1)]

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if count == 1 and lines:
            return [" ".join(lines)]
        if len(lines) == count:
            return lines

        raise TranslationProviderError("无法解析模型返回的 JSON 译文，请重试或更换模型。")

    def translate(self, texts: list[str], target_lang: str = "zh") -> list[str]:
        if not texts:
            return []
        items = {str(index + 1): text for index, text in enumerate(texts)}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                content = self._request(items)
                return self._parse(content, len(texts))
            except (requests.RequestException, ValueError, TranslationProviderError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise TranslationProviderError(f"翻译请求失败：{last_error}")
