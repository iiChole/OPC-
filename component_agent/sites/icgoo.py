"""ICGoGo sitemap planner and catalog-only table parser."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import List
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..agents.decision import WebsiteDecision
from ..catalog.models import CatalogPage, CategoryTask, ProductSeed
from ..catalog.parser import CatalogParser
from ..models import FetchResult
from ..orchestration.robots import RobotsPolicy
from ..planning.models import CategoryCandidate, CrawlPlan, FetchTool, PaginationPlan


class ICGooCatalogParser(CatalogParser):
    def parse(
        self,
        result: FetchResult,
        category: CategoryTask,
        site_key: str,
        product_list_path: str = "",
        next_cursor_path: str = "",
    ) -> CatalogPage:
        if not _is_icgoo_catalog_url(result.url):
            return super().parse(
                result,
                category,
                site_key,
                product_list_path,
                next_cursor_path,
            )

        soup = BeautifulSoup(result.text or "", "html.parser")
        table = soup.select_one("table.main_table")
        products: List[ProductSeed] = []
        if table is not None:
            for row in table.select("tr")[1:]:
                cells = row.select("td")
                if len(cells) < 5:
                    continue
                model = cells[1].get_text(" ", strip=True)
                category_name = cells[2].get_text(" ", strip=True)
                product_id = cells[3].get_text(" ", strip=True)
                created_at = cells[4].get_text(" ", strip=True)
                if not model:
                    continue
                link = cells[1].select_one("a[href]")
                restricted_url = urljoin(
                    result.url,
                    str(link.get("href", "")) if link else "",
                )
                seed = ProductSeed(
                    site_key=site_key,
                    category_id=category.identifier,
                    category_name=category_name or category.name,
                    sku=model,
                    product_id=product_id,
                    title=model,
                    source_url=result.url,
                    attributes={
                        "catalog_name": category_name or category.name,
                        "catalog_code": product_id,
                        "created_at": created_at,
                    },
                    extra={
                        "catalog_only": True,
                        "robots_disallowed_detail_url": restricted_url,
                        "catalog_raw": {
                            "partNumber": model,
                            "productId": product_id,
                            "title": model,
                            "categoryName": category_name,
                            "createdAt": created_at,
                        },
                    },
                )
                seed.assign_dedup_identity()
                products.append(seed)

        visible_text = soup.get_text(" ", strip=True)
        total_match = re.search(r"共\s*([\d,]+)\s*条", visible_text)
        total_count = (
            int(total_match.group(1).replace(",", ""))
            if total_match
            else None
        )
        next_url = ""
        pagination = soup.select_one("ul.pagination")
        if pagination is not None:
            for anchor in pagination.select("a[href]"):
                text = anchor.get_text(" ", strip=True)
                if text in {"下一页", "下页", "Next", "›", "»"}:
                    next_url = urljoin(result.url, str(anchor.get("href", "")))
                    break

        signature_material = "\n".join(
            f"{seed.sku}\t{seed.product_id}" for seed in products
        )
        signature = (
            hashlib.sha256(signature_material.encode("utf-8")).hexdigest()
            if signature_material
            else ""
        )
        return CatalogPage(
            products=products,
            raw_product_count=len(products),
            child_categories=[],
            next_url=next_url,
            has_next_control=bool(next_url),
            page_size=40,
            total_count=total_count,
            signature=signature,
            source_kind="icgoo_catalog_html",
        )


class ICGooSiteAdapter:
    key = "icgoo"

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host == "icgoo.net" or host.endswith(".icgoo.net")

    def catalog_parser(self) -> CatalogParser:
        return ICGooCatalogParser()

    def build_plan(
        self,
        url: str,
        decision: WebsiteDecision,
        fetch_tool: FetchTool,
        robots_policy: RobotsPolicy,
        category_limit: int = 0,
    ) -> CrawlPlan:
        sitemap_url = next(
            (
                value
                for value in robots_policy.sitemap_urls
                if robots_policy.allows(value)
            ),
            "https://www.icgoo.net/sitemap.xml",
        )
        result = fetch_tool.fetch(sitemap_url, preferred_transport="requests")
        if result.status_code >= 400:
            raise RuntimeError(f"ICGoGo sitemap 请求失败: HTTP {result.status_code}")
        categories = _categories_from_sitemap(result.text)
        if category_limit > 0:
            categories = categories[:category_limit]
        if not categories:
            raise RuntimeError("ICGoGo sitemap 未发现 /catalog/<id>/ 分类")

        decision_data = decision.to_dict()
        scope = "sample" if category_limit > 0 else "full"
        return CrawlPlan(
            input_url=url,
            start_url="https://www.icgoo.net/catalog/",
            site_key=self.key,
            website_type=decision.website_type.value,
            status="ready",
            decision=decision_data,
            homepage={
                "url": result.url,
                "status_code": result.status_code,
                "transport": result.transport,
                "page_kind": "xml_sitemap",
            },
            categories=categories,
            api_candidates=[],
            pagination=PaginationPlan(
                method="page_parameter",
                parameter="page",
                page_size=40,
                evidence=[
                    "ICGoGo catalog 页面使用 ?page=N 分页",
                    "商品型号来自 table.main_table；/search/ 受 robots.txt 禁止",
                ],
            ),
            exploration={
                "bounded": True,
                "source": "robots_sitemap",
                "sitemap_url": result.url,
                "sitemap_category_count": len(categories),
                "scope": scope,
            },
            execution_policy={
                "exhaustive": category_limit <= 0,
                "scope": scope,
                "all_categories_required": category_limit <= 0,
                "all_pages_required": True,
                "all_discovered_product_details_required": False,
                "resume_from_checkpoint": True,
                "deduplicate_key_order": [
                    "sku",
                    "site_product_id",
                    "normalized_detail_url",
                ],
                "detail_fetch": {
                    "enabled": False,
                    "mode": "catalog_only",
                    "reason": "robots.txt 禁止 /search/ 和 /partno-detail 路径",
                    "max_concurrency": 1,
                    "request_interval_seconds": 0,
                },
            },
            validation_policy={
                "verify_reported_count_when_available": True,
                "discovered_count_must_equal_saved_count": True,
                "failed_task_count_must_be_zero": True,
                "unfinished_task_count_must_be_zero": True,
                "required_fields": ["part_number", "title"],
                "verify_required_field_completeness": True,
                "preserve_site_specific_fields_in_extra": True,
            },
            retry_policy={
                "max_workflow_attempts": 2,
                "diagnose_before_retry": True,
                "retry_failed_and_incomplete_stages": True,
            },
            output_contract={
                "run_state/crawl_plan.json": "站点计划与 sitemap 证据",
                "run_state/product_seeds.jsonl": "目录页型号记录",
                "run_state/product_details.jsonl": "catalog-only 统一商品记录",
                "categories.json": "已完成分类和分页统计",
                "products_final.json": "最终去重型号数据",
            },
            workflow_steps=[
                {"id": "robots", "agent": "FullSiteCrawlCoordinator"},
                {"id": "plan", "agent": "ICGooSiteAdapter"},
                {"id": "catalog", "agent": "CatalogAgent"},
                {"id": "normalize", "agent": "ProductAgent", "mode": "catalog_only"},
                {"id": "validate", "agent": "CrawlResultValidator"},
            ],
            issues=[{
                "stage": "plan",
                "code": "robots_restricted_detail_pages",
                "message": "按 robots.txt 仅抓取 catalog 分类与型号，不访问 /search/ 详情",
                "url": robots_policy.robots_url,
                "retryable": False,
            }],
            diagnostics=[{
                "stage": "sitemap",
                "url": result.url,
                "status_code": result.status_code,
                "transport": result.transport,
                "category_count": len(categories),
            }],
        )


def _categories_from_sitemap(text: str) -> List[CategoryCandidate]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RuntimeError(f"ICGoGo sitemap XML 无效: {exc}") from exc
    categories: List[CategoryCandidate] = []
    seen = set()
    for element in root.iter():
        if not element.tag.lower().endswith("loc") or not element.text:
            continue
        normalized = _https_url(element.text.strip())
        match = re.fullmatch(r"/catalog/(\d+)/?", urlsplit(normalized).path)
        if not match or normalized in seen:
            continue
        seen.add(normalized)
        identifier = match.group(1)
        categories.append(CategoryCandidate(
            name=f"catalog_{identifier}",
            url=normalized,
            identifier=identifier,
            source="sitemap",
            confidence=1.0,
            evidence="ICGoGo robots.txt 声明的 sitemap",
        ))
    return categories


def _https_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _is_icgoo_catalog_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    return (
        (host == "icgoo.net" or host.endswith(".icgoo.net"))
        and bool(re.fullmatch(r"/catalog/\d+/?", parsed.path))
    )


__all__ = ["ICGooCatalogParser", "ICGooSiteAdapter"]
