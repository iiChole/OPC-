from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from .adapters import SiteAdapter, build_adapters
from .models import (
    CrawlIssue,
    CrawlReport,
    CrawlRequest,
    PageDiagnostic,
    ProductRecord,
    utc_now,
)
from .parser import PageInspector
from .storage import CrawlStorage
from .tools import AdaptiveFetchTool, CrawlToolError, ToolUnavailable


class ComponentSearchAgent:
    """One orchestrator that analyzes, inspects and crawls multiple supplier sites."""

    def __init__(
        self,
        sites: Sequence[str] = ("ickey", "szlcsc", "ti"),
        output_dir: str | Path = Path(__file__).parent / "data",
        timeout: int = 30,
        retries: int = 3,
        delay: float = 0.35,
        browser_enabled: bool = True,
        headless: bool = True,
        max_results_per_site: int = 10,
        fetch_tool: AdaptiveFetchTool | None = None,
    ) -> None:
        self.adapters: List[SiteAdapter] = build_adapters(sites)
        self.storage = CrawlStorage(output_dir)
        self.fetch_tool = fetch_tool or AdaptiveFetchTool(
            timeout=timeout,
            retries=retries,
            delay=delay,
            browser_enabled=browser_enabled,
            headless=headless,
        )
        self.max_results_per_site = max(1, max_results_per_site)

    def run(self, request: CrawlRequest) -> CrawlReport:
        started_at = utc_now()
        catalog_products: List[ProductRecord] = []
        detail_products: List[ProductRecord] = []
        final_products: List[ProductRecord] = []
        issues: List[CrawlIssue] = []
        diagnostics: List[PageDiagnostic] = []

        for adapter in self.adapters:
            candidates = self._crawl_catalog(adapter, request, issues, diagnostics)
            catalog_products.extend(candidates)

            for product in candidates:
                detail = self._crawl_detail(adapter, product, issues, diagnostics)
                if detail is not None:
                    detail_products.append(detail)
                    final = product.merge(detail)
                else:
                    final = product

                missing = final.missing_fields(request.fields)
                if missing:
                    issues.append(CrawlIssue(
                        site=adapter.profile.key,
                        stage="validate",
                        code="missing_fields",
                        message=f"未能提取字段: {', '.join(missing)}",
                        url=final.detail_url or final.source_url,
                        retryable=False,
                        product=final.model or final.sku,
                    ))
                final_products.append(final)

        final_products = self._deduplicate_final(final_products)
        report = CrawlReport(
            request=request,
            products=final_products,
            issues=issues,
            diagnostics=diagnostics,
            started_at=started_at,
            finished_at=utc_now(),
        )
        target = self.storage.save(report, catalog_products, detail_products)
        report.output_dir = str(target.resolve())
        # Save once more so products_final.json contains the resolved output_dir.
        self.storage.save(report, catalog_products, detail_products)
        return report

    def _crawl_catalog(
        self,
        adapter: SiteAdapter,
        request: CrawlRequest,
        issues: List[CrawlIssue],
        diagnostics: List[PageDiagnostic],
    ) -> List[ProductRecord]:
        found: List[ProductRecord] = []
        last_text = ""
        for url in adapter.catalog_urls(request.query):
            try:
                page = self.fetch_tool.fetch(
                    url,
                    preferred_transport=adapter.profile.preferred_transport,
                    headers=adapter.catalog_headers(url),
                )
                last_text = page.text
            except ToolUnavailable as exc:
                issues.append(CrawlIssue(
                    site=adapter.profile.key,
                    stage="catalog",
                    code="tool_unavailable",
                    message=str(exc),
                    url=url,
                    retryable=True,
                ))
                continue
            except CrawlToolError as exc:
                issues.append(CrawlIssue(
                    site=adapter.profile.key,
                    stage="catalog",
                    code="fetch_failed",
                    message=str(exc),
                    url=url,
                    retryable=True,
                ))
                continue

            kind = PageInspector.inspect(page)
            if page.status_code >= 400:
                issues.append(CrawlIssue(
                    site=adapter.profile.key,
                    stage="catalog",
                    code="http_error",
                    message=f"HTTP {page.status_code}",
                    url=page.url,
                    retryable=page.status_code >= 500 or page.status_code == 429,
                ))
                diagnostics.append(PageDiagnostic(
                    site=adapter.profile.key,
                    stage="catalog",
                    url=page.url,
                    page_kind=kind,
                    transport=page.transport,
                    status_code=page.status_code,
                    elapsed_ms=page.elapsed_ms,
                ))
                continue

            parsed = adapter.parse_catalog(page, request.query)
            parsed = [item for item in parsed if item.relevance(request.query) > 0]
            parsed.sort(key=lambda item: item.relevance(request.query), reverse=True)
            diagnostics.append(PageDiagnostic(
                site=adapter.profile.key,
                stage="catalog",
                url=page.url,
                page_kind=kind,
                transport=page.transport,
                status_code=page.status_code,
                elapsed_ms=page.elapsed_ms,
                product_count=len(parsed),
            ))
            found.extend(parsed)
            if found:
                break

        found = self._deduplicate_final(found)[: self.max_results_per_site]
        if not found:
            no_result = adapter.parser.has_no_results(last_text)
            issues.append(CrawlIssue(
                site=adapter.profile.key,
                stage="catalog",
                code="no_results" if no_result else "parse_empty",
                message="站点未返回匹配商品" if no_result else "页面可访问，但未识别到匹配商品结构",
                url=adapter.catalog_urls(request.query)[0],
                retryable=not no_result,
            ))
        return found

    def _crawl_detail(
        self,
        adapter: SiteAdapter,
        product: ProductRecord,
        issues: List[CrawlIssue],
        diagnostics: List[PageDiagnostic],
    ) -> ProductRecord | None:
        if not product.detail_url:
            return None
        try:
            page = self.fetch_tool.fetch(
                product.detail_url,
                preferred_transport="auto",
                headers=adapter.detail_headers(product),
            )
        except ToolUnavailable as exc:
            issues.append(CrawlIssue(
                site=adapter.profile.key,
                stage="detail",
                code="tool_unavailable",
                message=f"详情页回退到目录数据: {exc}",
                url=product.detail_url,
                retryable=True,
                product=product.model or product.sku,
            ))
            return None
        except CrawlToolError as exc:
            issues.append(CrawlIssue(
                site=adapter.profile.key,
                stage="detail",
                code="fetch_failed",
                message=f"详情页回退到目录数据: {exc}",
                url=product.detail_url,
                retryable=True,
                product=product.model or product.sku,
            ))
            return None

        kind = PageInspector.inspect(page)
        diagnostics.append(PageDiagnostic(
            site=adapter.profile.key,
            stage="detail",
            url=page.url,
            page_kind=kind,
            transport=page.transport,
            status_code=page.status_code,
            elapsed_ms=page.elapsed_ms,
            product_count=1 if page.status_code < 400 else 0,
        ))
        if page.status_code >= 400:
            issues.append(CrawlIssue(
                site=adapter.profile.key,
                stage="detail",
                code="http_error",
                message=f"详情页 HTTP {page.status_code}，已保留目录数据",
                url=page.url,
                retryable=page.status_code >= 500 or page.status_code == 429,
                product=product.model or product.sku,
            ))
            return None
        return adapter.parse_detail(page, product)

    @staticmethod
    def _deduplicate_final(products: Iterable[ProductRecord]) -> List[ProductRecord]:
        unique: List[ProductRecord] = []
        index = {}
        for product in products:
            key = (product.site, product.key)
            if not product.key:
                continue
            if key in index:
                position = index[key]
                unique[position] = unique[position].merge(product)
            else:
                index[key] = len(unique)
                unique.append(product)
        return unique
