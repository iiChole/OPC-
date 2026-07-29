"""Classify a website before any crawl work is scheduled."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit


class WebsiteType(str, Enum):
    DISTRIBUTOR = "distributor"
    MANUFACTURER = "manufacturer"
    MARKETPLACE_ECOMMERCE = "marketplace_ecommerce"
    SUPPLIER_MARKETPLACE = "supplier_marketplace"
    BOM_INTELLIGENCE = "bom_intelligence"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CategoryProfile:
    key: WebsiteType
    label: str
    expected_structure: Tuple[str, ...]
    strategy: Tuple[str, ...]
    target_fields: Tuple[str, ...]
    route_to: str
    intentionally_omitted_fields: Tuple[str, ...] = ()


@dataclass(frozen=True)
class WebsiteRule:
    key: str
    name: str
    website_type: WebsiteType
    domains: Tuple[str, ...]
    aliases: Tuple[str, ...]


CATEGORY_PROFILES: Dict[WebsiteType, CategoryProfile] = {
    WebsiteType.DISTRIBUTOR: CategoryProfile(
        key=WebsiteType.DISTRIBUTOR,
        label="电子元件授权分销商（Distributor）",
        expected_structure=(
            "产品信息结构化程度高",
            "通常包含标准产品详情页（Product Detail Page）",
            "型号、制造商、封装、库存、价格和数据手册通常有稳定字段",
        ),
        strategy=(
            "根据元件型号搜索产品页面",
            "进入标准产品详情页",
            "提取标准字段并转换为统一产品 JSON",
        ),
        target_fields=(
            "part_number",
            "manufacturer",
            "package",
            "stock_availability",
            "price",
            "datasheet_url",
        ),
        route_to="full_catalog_workflow",
    ),
    WebsiteType.MANUFACTURER: CategoryProfile(
        key=WebsiteType.MANUFACTURER,
        label="半导体原厂网站（Manufacturer）",
        expected_structure=(
            "主要提供技术资料，而非商品交易",
            "信息集中在官方产品页、技术文档和 PDF 数据手册",
            "通常不提供可直接比较的实时价格和库存",
        ),
        strategy=(
            "根据型号定位官方产品页",
            "提取电气参数、工作条件、封装和应用信息",
            "需要时下载并解析 PDF 数据手册中的关键参数",
        ),
        target_fields=(
            "electrical_parameters",
            "operating_conditions",
            "package_information",
            "application_information",
            "datasheet_url",
        ),
        intentionally_omitted_fields=("price", "stock"),
        route_to="full_catalog_workflow",
    ),
    WebsiteType.MARKETPLACE_ECOMMERCE: CategoryProfile(
        key=WebsiteType.MARKETPLACE_ECOMMERCE,
        label="国内电子元件商城（Marketplace / E-commerce）",
        expected_structure=(
            "类似电子元器件电商平台",
            "页面可能由 JavaScript 动态渲染",
            "商品列表、详情、价格和库存可能通过 API 加载",
        ),
        strategy=(
            "优先使用浏览器自动化分析 DOM 和 Network 请求",
            "发现稳定 API 后优先改为 API 数据获取",
            "提取商品字段并保留在售状态",
        ),
        target_fields=(
            "part_number",
            "manufacturer",
            "price",
            "stock",
            "package",
            "product_status",
        ),
        route_to="full_catalog_workflow",
    ),
    WebsiteType.SUPPLIER_MARKETPLACE: CategoryProfile(
        key=WebsiteType.SUPPLIER_MARKETPLACE,
        label="元器件交易信息平台（Supplier Marketplace）",
        expected_structure=(
            "不是标准单一卖家商城",
            "同一型号的信息来自多个供应商",
            "重点是供应商、报价、库存和交易条件",
        ),
        strategy=(
            "遍历公开分类、产品和供应信息列表",
            "将每条商品或供应记录独立保存",
            "不执行跨供应商价格、库存或交易条件比较",
        ),
        target_fields=(
            "company_name",
            "public_contact_information",
            "stock_quantity",
            "quoted_price",
            "trade_terms",
        ),
        route_to="full_catalog_workflow",
    ),
    WebsiteType.BOM_INTELLIGENCE: CategoryProfile(
        key=WebsiteType.BOM_INTELLIGENCE,
        label="BOM 分析与聚合平台（BOM Intelligence Platform）",
        expected_structure=(
            "面向完整 BOM 而非单一商品页",
            "关注替代料、供应链风险和采购优化",
            "输入通常是 BOM 文件或元件清单",
        ),
        strategy=(
            "解析输入 BOM 并拆分元件列表",
            "查询多个供应渠道",
            "汇总替代型号、供应商、成本和可采购性",
        ),
        target_fields=(
            "alternative_part_numbers",
            "suppliers",
            "cost",
            "availability",
        ),
        route_to="unsupported_for_full_catalog",
    ),
    WebsiteType.UNKNOWN: CategoryProfile(
        key=WebsiteType.UNKNOWN,
        label="未知网站类型（Unknown）",
        expected_structure=(),
        strategy=("补充域名规则，或提供人工确认的网站结构信息后再分类",),
        target_fields=(),
        route_to="manual_review",
    ),
}


SITE_RULES: Tuple[WebsiteRule, ...] = (
    WebsiteRule(
        "digikey",
        "DigiKey",
        WebsiteType.DISTRIBUTOR,
        ("digikey.com", "digikey.cn"),
        ("digikey", "digi-key"),
    ),
    WebsiteRule(
        "mouser",
        "Mouser Electronics",
        WebsiteType.DISTRIBUTOR,
        ("mouser.com", "mouser.cn"),
        ("mouser", "mouser electronics", "贸泽"),
    ),
    WebsiteRule(
        "arrow",
        "Arrow Electronics",
        WebsiteType.DISTRIBUTOR,
        ("arrow.com",),
        ("arrow", "arrow electronics", "艾睿电子"),
    ),
    WebsiteRule(
        "future_electronics",
        "Future Electronics",
        WebsiteType.DISTRIBUTOR,
        ("futureelectronics.com",),
        ("future electronics", "富昌电子"),
    ),
    WebsiteRule(
        "element14",
        "Element14",
        WebsiteType.DISTRIBUTOR,
        ("element14.com",),
        ("element14",),
    ),
    WebsiteRule(
        "adi",
        "Analog Devices (ADI)",
        WebsiteType.MANUFACTURER,
        ("analog.com",),
        ("analog devices", "adi", "亚德诺"),
    ),
    WebsiteRule(
        "ti",
        "Texas Instruments (TI)",
        WebsiteType.MANUFACTURER,
        ("ti.com",),
        ("texas instruments", "ti", "德州仪器"),
    ),
    WebsiteRule(
        "lcsc",
        "LCSC / 立创商城",
        WebsiteType.MARKETPLACE_ECOMMERCE,
        ("lcsc.com", "szlcsc.com"),
        ("lcsc", "szlcsc", "立创商城"),
    ),
    WebsiteRule(
        "ickey",
        "ICKey / 云汉芯城",
        WebsiteType.MARKETPLACE_ECOMMERCE,
        ("ickey.cn",),
        ("ickey", "云汉芯城"),
    ),
    WebsiteRule(
        "ic_trade",
        "IC 交易网",
        WebsiteType.SUPPLIER_MARKETPLACE,
        ("ic.net.cn",),
        ("ic交易网", "ic 交易网"),
    ),
    WebsiteRule(
        "hqew",
        "华强电子网",
        WebsiteType.SUPPLIER_MARKETPLACE,
        ("hqew.com",),
        ("华强电子网", "hqew"),
    ),
    WebsiteRule(
        "positive_energy_electronics",
        "正能量电子网",
        WebsiteType.SUPPLIER_MARKETPLACE,
        (),
        ("正能量电子网",),
    ),
    WebsiteRule(
        "bom_ai",
        "BOM.ai",
        WebsiteType.BOM_INTELLIGENCE,
        ("bom.ai",),
        ("bom.ai", "bomai"),
    ),
)


@dataclass(frozen=True)
class WebsiteDecision:
    input_site: str
    normalized_host: str
    site_key: str
    site_name: str
    website_type: WebsiteType
    confidence: float
    matched_by: str
    matched_value: str

    def to_dict(self) -> Dict[str, Any]:
        profile = CATEGORY_PROFILES[self.website_type]
        return {
            "status": "classified" if self.website_type is not WebsiteType.UNKNOWN else "unclassified",
            "decision_only": True,
            "crawl_performed": False,
            "input_site": self.input_site,
            "normalized_host": self.normalized_host,
            "recognized_site": {
                "key": self.site_key,
                "name": self.site_name,
            },
            "classification": {
                "type": self.website_type.value,
                "label": profile.label,
                "confidence": self.confidence,
            },
            "structure_analysis": {
                "mode": "offline_profile",
                "page_inspected": False,
                "expected_characteristics": list(profile.expected_structure),
            },
            "recommended_handling": {
                "strategy": list(profile.strategy),
                "target_fields": list(profile.target_fields),
                "intentionally_omitted_fields": list(profile.intentionally_omitted_fields),
                "route_to": profile.route_to,
                "execute": False,
            },
            "debug": {
                "matched_by": self.matched_by,
                "matched_value": self.matched_value,
                "rule_count": len(SITE_RULES),
            },
        }


class WebsiteDecisionAgent:
    """Classify a supplied website without fetching it or invoking a crawler."""

    def decide(self, site: str) -> WebsiteDecision:
        input_site = str(site or "").strip()
        host = _extract_host(input_site)

        for rule in SITE_RULES:
            for domain in rule.domains:
                if _domain_matches(host, domain):
                    return self._build_decision(
                        input_site,
                        host,
                        rule,
                        confidence=1.0,
                        matched_by="domain",
                        matched_value=domain,
                    )

        normalized_input = _normalize_alias(input_site)
        for rule in SITE_RULES:
            for alias in rule.aliases:
                if normalized_input == _normalize_alias(alias):
                    return self._build_decision(
                        input_site,
                        host,
                        rule,
                        confidence=0.95,
                        matched_by="alias",
                        matched_value=alias,
                    )

        return WebsiteDecision(
            input_site=input_site,
            normalized_host=host,
            site_key="",
            site_name="",
            website_type=WebsiteType.UNKNOWN,
            confidence=0.0,
            matched_by="none",
            matched_value="",
        )

    @staticmethod
    def _build_decision(
        input_site: str,
        host: str,
        rule: WebsiteRule,
        confidence: float,
        matched_by: str,
        matched_value: str,
    ) -> WebsiteDecision:
        return WebsiteDecision(
            input_site=input_site,
            normalized_host=host,
            site_key=rule.key,
            site_name=rule.name,
            website_type=rule.website_type,
            confidence=confidence,
            matched_by=matched_by,
            matched_value=matched_value,
        )


def _extract_host(value: str) -> str:
    if not value:
        return ""
    candidate = value if "://" in value else f"//{value}"
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def _domain_matches(host: str, domain: str) -> bool:
    normalized_domain = domain.lower().rstrip(".")
    return bool(host) and (host == normalized_domain or host.endswith(f".{normalized_domain}"))


def _normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)
