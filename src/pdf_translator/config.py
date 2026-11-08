"""全局默认配置。

排版相关参数（页边距、行距、字号）在 ``pdf_translator.reflow.styles`` 里，
这里只留界面/命令行共用的默认值。
"""

APP_NAME = "PDF 文档翻译"

# OpenAI 兼容接口的默认值（可在界面「高级设置」或命令行覆盖）
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
