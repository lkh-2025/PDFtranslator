"""离线 mock 翻译器：不联网、不需要 Key，用于验证完整流程。"""

from __future__ import annotations

import hashlib
import re

from .base import BaseProvider

PHRASES = {
    "annual financial overview": "年度财务概览",
    "fiscal year 2025 · prepared by the finance team": "2025 财年 · 财务团队编制",
    "northwind analytics · internal report": "北风分析 · 内部报告",
    "abstract": "摘要",
    "introduction": "引言",
    "revenue breakdown": "收入明细",
    "conclusion": "结论",
    "appendix a: reference data": "附录 A：参考资料",
    "figure 1. revenue trend by quarter": "图 1. 各季度收入趋势",
    "table 1. revenue by segment": "表 1. 各业务板块收入",
    "segment": "业务板块",
    "q1 revenue": "一季度收入",
    "q2 revenue": "二季度收入",
    "total revenue": "收入合计",
    "operating expenses": "运营费用",
    "net income": "净利润",
    "gross margin": "毛利率",
    "customer growth": "客户增长",
    "market outlook": "市场展望",
}

WORDS = {
    "cloud": "云",
    "products": "产品",
    "product": "产品",
    "hardware": "硬件",
    "software": "软件",
    "subscription": "订阅",
    "subscriptions": "订阅",
    "professional": "专业",
    "advisory": "咨询",
    "support": "支持",
    "partners": "合作伙伴",
    "network": "网络",
    "annual": "年度",
    "financial": "财务",
    "overview": "概览",
    "report": "报告",
    "fiscal": "财年",
    "year": "年份",
    "prepared": "编制",
    "finance": "财务",
    "team": "团队",
    "abstract": "摘要",
    "introduction": "引言",
    "conclusion": "结论",
    "revenue": "收入",
    "breakdown": "明细",
    "segment": "板块",
    "total": "合计",
    "growth": "增长",
    "quarter": "季度",
    "quarterly": "季度",
    "cost": "成本",
    "costs": "成本",
    "profit": "利润",
    "margin": "毛利",
    "customer": "客户",
    "customers": "客户",
    "market": "市场",
    "service": "服务",
    "services": "服务",
    "region": "地区",
    "regions": "地区",
    "table": "表",
    "figure": "图",
    "page": "页",
    "data": "数据",
    "source": "来源",
    "method": "方法",
    "methods": "方法",
    "result": "结果",
    "results": "结果",
    "discussion": "讨论",
    "note": "说明",
    "company": "公司",
    "sales": "销售额",
    "operating": "运营",
    "expense": "费用",
    "expenses": "费用",
    "net": "净",
    "income": "收入",
    "internal": "内部",
    "confidential": "机密",
    "northwind": "北风",
    "analytics": "分析",
    "trend": "趋势",
    "key": "关键",
    "highlights": "要点",
    "increase": "增长",
    "decrease": "下降",
    "compared": "相比",
    "previous": "上一",
    "strong": "强劲",
    "performance": "表现",
    "driven": "受推动于",
    "demand": "需求",
    "investment": "投资",
    "technology": "技术",
    "operations": "运营",
    "efficiency": "效率",
    "outlook": "展望",
    "remain": "保持",
    "positive": "积极",
    "risk": "风险",
    "risks": "风险",
    "include": "包括",
    "includes": "包括",
    "supply": "供应",
    "chain": "链",
    "currency": "汇率",
    "regulation": "监管",
}

FALLBACKS = [
    "该段为离线模拟译文，用于验证中文重排与阶段产物流程",
    "此处展示占位中文，真实翻译请切换至在线接口",
    "模拟翻译内容，段落位置与原始英文保持一致",
    "离线模式占位文本，用于检查分页与表格是否完整",
]

FOOTER_RE = re.compile(r"^confidential\s*[·|]\s*page\s+(\d+)$", re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .:;")


def _fallback(text: str) -> str:
    digest = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
    template = FALLBACKS[digest % len(FALLBACKS)]
    target = max(10, min(110, int(len(text) * 0.42)))
    parts: list[str] = []
    total = 0
    while total < target:
        parts.append(template)
        total += len(template) + 1
    body = "，".join(parts)
    return "【模拟译文】" + body + "。"


class MockProvider(BaseProvider):
    name = "离线模拟"

    def describe(self) -> str:
        return "离线模拟（无需 API Key）"

    def translate_text(self, text: str) -> str:
        norm = _norm(text)
        if norm in PHRASES:
            return PHRASES[norm]
        footer = FOOTER_RE.match(norm)
        if footer:
            return f"机密 · 第 {footer.group(1)} 页"

        words = re.findall(r"[A-Za-z]+", text)
        if words:
            mapped = [WORDS[w.lower()] for w in words if w.lower() in WORDS]
            coverage = len(mapped) / len(words)
            if coverage >= 0.7 and len(words) <= 18:
                return "".join(mapped)
            if len(words) <= 3 and len(text) <= 24:
                return "（模拟译文）"
        return _fallback(text)

    def translate(self, texts: list[str], target_lang: str = "zh") -> list[str]:
        return [self.translate_text(text) for text in texts]
