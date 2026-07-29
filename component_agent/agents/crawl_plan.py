"""Explore a website and build an exhaustive catalog crawl plan."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import FetchResult
from ..planning.models import (
    ApiCandidate,
    CategoryCandidate,
    CrawlPlan,
    FetchTool,
    NetworkInspector,
    PageAnalysis,
    PaginationPlan,
    PaginationProbe,
)
from ..planning.page_analysis import (
    analyze_page,
    api_candidate_from_payload,
    canonical_url,
    categories_from_payload,
    choose_pagination,
    deduplicate_apis,
    deduplicate_categories,
    load_json,
    normalize_start_url,
    pagination_from_api,
    same_site,
    set_query_parameter,
)
from .decision import WebsiteDecision, WebsiteDecisionAgent, WebsiteType


class CrawlPlanAgent:
    """Perform bounded structure exploration and return an exhaustive execution plan."""

    def __init__(
        self,
        fetch_tool: Optional[FetchTool] = None,
        network_inspector: Optional[NetworkInspector] = None,
        decision_agent: Optional[WebsiteDecisionAgent] = None,
        timeout: int = 30,
        retries: int = 2,
        delay: float = 0.35,
        browser_enabled: bool = True,
        headless: bool = True,
        max_category_probes: int = 1,
        max_pagination_probes: int = 2,
    ) -> None:
        if fetch_tool is None:
            from ..tools import AdaptiveFetchTool

            fetch_tool = AdaptiveFetchTool(
                timeout=timeout,
                retries=retries,
                delay=delay,
                browser_enabled=browser_enabled,
                headless=headless,
            )
        self.fetch_tool = fetch_tool
        self.network_inspector = network_inspector or getattr(fetch_tool, "browser", None)
        self.decision_agent = decision_agent or WebsiteDecisionAgent()
        self.max_category_probes = max(0, max_category_probes)
        self.max_pagination_probes = max(0, max_pagination_probes)

    def run(
        self,
        url: str,
        decision: Optional[WebsiteDecision] = None,
    ) -> CrawlPlan:
        start_url = normalize_start_url(url)
        resolved_decision = decision or self.decision_agent.decide(start_url)
        decision_data = resolved_decision.to_dict()
        issues: List[Dict[str, Any]] = []
        diagnostics: List[Dict[str, Any]] = []

        homepage_result = self._safe_fetch(start_url, "homepage", issues, diagnostics)
        if homepage_result is None:
            return self._build_failed_plan(
                url,
                start_url,
                resolved_decision,
                decision_data,
                issues,
                diagnostics,
            )

        homepage_analysis = analyze_page(homepage_result, source="homepage")
        categories = list(homepage_analysis.categories)
        api_candidates = list(homepage_analysis.api_candidates)
        pagination_candidates = list(homepage_analysis.pagination_candidates)
        network_performed = False

        dynamic_expected = (
            resolved_decision.website_type is WebsiteType.MARKETPLACE_ECOMMERCE
        )
        dynamic_detected = (
            homepage_analysis.page_kind == "javascript_rendered"
            or homepage_result.transport.startswith("playwright")
        )
        if dynamic_expected or dynamic_detected:
            network_performed = self._inspect_network(
                start_url,
                click_next=False,
                stage="homepage_network",
                categories=categories,
                api_candidates=api_candidates,
                pagination_candidates=pagination_candidates,
                issues=issues,
                diagnostics=diagnostics,
            )

        categories = deduplicate_categories(categories)
        listing_analysis = homepage_analysis
        listing_url = start_url
        category_probes = 0
        for category in categories:
            if category_probes >= self.max_category_probes:
                break
            if not category.url or not same_site(start_url, category.url):
                continue
            if canonical_url(category.url) == canonical_url(start_url):
                continue
            category_result = self._safe_fetch(
                category.url,
                "category_probe",
                issues,
                diagnostics,
            )
            category_probes += 1
            if category_result is None:
                continue

            listing_url = category_result.url
            listing_analysis = analyze_page(category_result, source="category_page")
            categories.extend(listing_analysis.categories)
            api_candidates.extend(listing_analysis.api_candidates)
            pagination_candidates.extend(listing_analysis.pagination_candidates)

            listing_dynamic = (
                listing_analysis.page_kind == "javascript_rendered"
                or category_result.transport.startswith("playwright")
            )
            if listing_dynamic:
                network_performed = self._inspect_network(
                    listing_url,
                    click_next=listing_analysis.next_control_found,
                    stage="category_network",
                    categories=categories,
                    api_candidates=api_candidates,
                    pagination_candidates=pagination_candidates,
                    issues=issues,
                    diagnostics=diagnostics,
                ) or network_performed
            break

        categories = deduplicate_categories(categories)
        api_candidates = deduplicate_apis(api_candidates)
        pagination = choose_pagination(pagination_candidates)

        if pagination.method == "unknown" and self.max_pagination_probes:
            probed = self._probe_pagination_parameters(
                listing_url,
                listing_analysis,
                issues,
                diagnostics,
            )
            if probed.method != "unknown":
                pagination = probed
            else:
                pagination.probes.extend(probed.probes)
                pagination.evidence.extend(probed.evidence)

        if pagination.method == "unknown":
            pagination.method = "auto"
            pagination.evidence.append(
                "未确认单一分页方式；执行阶段按 page → offset → response cursor → Next 控件顺序探测"
            )

        product_apis = [
            candidate
            for candidate in api_candidates
            if candidate.purpose == "products"
        ]
        ready = bool(categories or product_apis or listing_analysis.product_count)
        status = "ready" if ready else "partial"
        if not ready:
            issues.append({
                "stage": "plan",
                "code": "catalog_entry_not_confirmed",
                "message": "首页可访问，但未确认分类入口、产品 API 或商品列表结构",
                "url": start_url,
                "retryable": True,
            })

        required_fields = decision_data["recommended_handling"]["target_fields"]
        return CrawlPlan(
            input_url=url,
            start_url=start_url,
            site_key=resolved_decision.site_key,
            website_type=resolved_decision.website_type.value,
            status=status,
            decision=decision_data,
            homepage={
                "url": homepage_result.url,
                "status_code": homepage_result.status_code,
                "transport": homepage_result.transport,
                "page_kind": homepage_analysis.page_kind,
            },
            categories=categories,
            api_candidates=api_candidates,
            pagination=pagination,
            exploration={
                "bounded": True,
                "homepage_requests": 1,
                "max_category_probes": self.max_category_probes,
                "max_pagination_probes": self.max_pagination_probes,
                "network_analysis_performed": network_performed,
                "purpose": "识别结构和生成计划，不在此阶段枚举全部商品",
            },
            execution_policy={
                "exhaustive": True,
                "all_categories_required": True,
                "all_pages_required": True,
                "all_discovered_product_details_required": True,
                "resume_from_checkpoint": True,
                "deduplicate_key_order": [
                    "sku",
                    "site_product_id",
                    "normalized_detail_url",
                ],
                "finish_only_when_stop_condition_reached": True,
                "detail_fetch": {
                    "preferred_transport": "auto",
                    "max_concurrency": 4,
                    "request_interval_seconds": 0.25,
                    "browser_fallback": True,
                    "source_priority": [
                        "detail_json_api",
                        "embedded_json",
                        "json_ld",
                        "microdata",
                        "html_attributes",
                    ],
                },
            },
            validation_policy={
                "verify_reported_count_when_available": True,
                "discovered_count_must_equal_saved_count": True,
                "failed_task_count_must_be_zero": True,
                "unfinished_task_count_must_be_zero": True,
                "required_fields": required_fields,
                "verify_required_field_completeness": True,
                "preserve_site_specific_fields_in_extra": True,
            },
            retry_policy={
                "max_workflow_attempts": 2,
                "first_failure_action": "diagnose_rebuild_plan_and_retry_workflow",
                "second_failure_action": "pause_task_and_report_problem",
                "diagnose_before_retry": True,
                "retry_failed_and_incomplete_stages": True,
                "replan_on": [
                    "page_structure_changed",
                    "pagination_not_advancing",
                    "product_api_changed",
                    "javascript_rendering_required",
                ],
            },
            output_contract=_output_contract(),
            workflow_steps=_workflow_steps(),
            issues=issues,
            diagnostics=diagnostics,
        )

    def _safe_fetch(
        self,
        url: str,
        stage: str,
        issues: List[Dict[str, Any]],
        diagnostics: List[Dict[str, Any]],
    ) -> Optional[FetchResult]:
        try:
            result = self.fetch_tool.fetch(url, preferred_transport="auto")
        except Exception as exc:
            issues.append({
                "stage": stage,
                "code": "fetch_failed",
                "message": str(exc),
                "url": url,
                "retryable": True,
            })
            return None
        diagnostics.append({
            "stage": stage,
            "url": result.url,
            "status_code": result.status_code,
            "transport": result.transport,
            "elapsed_ms": result.elapsed_ms,
        })
        if result.status_code >= 400:
            issues.append({
                "stage": stage,
                "code": "http_error",
                "message": f"HTTP {result.status_code}",
                "url": result.url,
                "retryable": result.status_code == 429 or result.status_code >= 500,
            })
        return result

    def _inspect_network(
        self,
        url: str,
        click_next: bool,
        stage: str,
        categories: List[CategoryCandidate],
        api_candidates: List[ApiCandidate],
        pagination_candidates: List[PaginationPlan],
        issues: List[Dict[str, Any]],
        diagnostics: List[Dict[str, Any]],
    ) -> bool:
        if self.network_inspector is None:
            issues.append({
                "stage": stage,
                "code": "network_inspector_unavailable",
                "message": "页面可能动态渲染，但没有可用的 Playwright Network 分析工具",
                "url": url,
                "retryable": True,
            })
            return False
        try:
            inspection = self.network_inspector.inspect_network(
                url,
                click_next=click_next,
            )
        except Exception as exc:
            issues.append({
                "stage": stage,
                "code": "network_inspection_failed",
                "message": str(exc),
                "url": url,
                "retryable": True,
            })
            return False

        rendered = analyze_page(inspection.page, source=stage)
        categories.extend(rendered.categories)
        api_candidates.extend(rendered.api_candidates)
        pagination_candidates.extend(rendered.pagination_candidates)

        for observation in inspection.responses:
            payload = load_json(observation.response_text)
            if payload is None:
                continue
            categories.extend(categories_from_payload(
                payload,
                observation.url,
                source="network_api",
            ))
            candidate = api_candidate_from_payload(
                payload,
                observation.url,
                source="network",
                method=observation.method,
                status_code=observation.status_code,
            )
            if candidate is not None:
                api_candidates.append(candidate)
                api_pagination = pagination_from_api(candidate)
                if api_pagination is not None:
                    pagination_candidates.append(api_pagination)

        diagnostics.append({
            "stage": stage,
            "url": inspection.page.url,
            "status_code": inspection.page.status_code,
            "transport": inspection.page.transport,
            "captured_response_count": len(inspection.responses),
            "clicked_next": inspection.clicked_next,
            "next_selector": inspection.next_selector,
        })
        if inspection.clicked_next and inspection.next_selector:
            pagination_candidates.append(PaginationPlan(
                method="next_click",
                next_selector=inspection.next_selector,
                evidence=[
                    f"Playwright 成功点击下一页控件: {inspection.next_selector}"
                ],
            ))
        return True

    def _probe_pagination_parameters(
        self,
        listing_url: str,
        first_page: PageAnalysis,
        issues: List[Dict[str, Any]],
        diagnostics: List[Dict[str, Any]],
    ) -> PaginationPlan:
        plan = PaginationPlan(method="unknown")
        page_size = first_page.product_count or 20
        candidates = (
            ("page_parameter", "page", "2"),
            ("offset_parameter", "offset", str(page_size)),
        )
        for method, parameter, value in candidates[: self.max_pagination_probes]:
            probe_url = set_query_parameter(listing_url, parameter, value)
            result = self._safe_fetch(
                probe_url,
                f"pagination_probe_{parameter}",
                issues,
                diagnostics,
            )
            if result is None:
                continue
            analysis = analyze_page(result, source=f"pagination_probe_{parameter}")
            different = bool(
                first_page.product_signature
                and analysis.product_signature != first_page.product_signature
            )
            accepted = (
                result.status_code < 400
                and different
                and (
                    analysis.product_count > 0
                    or first_page.product_count > 0
                )
            )
            reason = (
                "第二页商品签名与第一页不同"
                if accepted
                else "响应与第一页相同或无法识别商品，未确认该参数"
            )
            probe = PaginationProbe(
                method=method,
                url=probe_url,
                status_code=result.status_code,
                product_count=analysis.product_count,
                different_from_first_page=different,
                accepted=accepted,
                reason=reason,
            )
            plan.probes.append(probe)
            if accepted:
                plan.method = method
                plan.parameter = parameter
                plan.request_url_template = set_query_parameter(
                    listing_url,
                    parameter,
                    f"{{{parameter}}}",
                )
                plan.page_size = page_size
                plan.evidence.append(reason)
                if analysis.product_count < page_size:
                    plan.evidence.append(
                        "探测页返回数量小于第一页数量，可用 "
                        "returned_count_less_than_page_size 判断末页"
                    )
                return plan
        plan.evidence.append(
            "page/offset 有限探测未确认，执行阶段需继续检查 cursor 或 Next 控件"
        )
        return plan

    @staticmethod
    def _build_failed_plan(
        input_url: str,
        start_url: str,
        decision: WebsiteDecision,
        decision_data: Dict[str, Any],
        issues: List[Dict[str, Any]],
        diagnostics: List[Dict[str, Any]],
    ) -> CrawlPlan:
        return CrawlPlan(
            input_url=input_url,
            start_url=start_url,
            site_key=decision.site_key,
            website_type=decision.website_type.value,
            status="failed",
            decision=decision_data,
            homepage={},
            categories=[],
            api_candidates=[],
            pagination=PaginationPlan(method="unknown"),
            exploration={
                "bounded": True,
                "purpose": "首页探索失败，未执行商品抓取",
            },
            execution_policy={
                "exhaustive": True,
                "all_categories_required": True,
                "all_pages_required": True,
                "all_discovered_product_details_required": True,
            },
            validation_policy={
                "verify_reported_count_when_available": True,
                "discovered_count_must_equal_saved_count": True,
                "failed_task_count_must_be_zero": True,
                "verify_required_field_completeness": True,
            },
            retry_policy={
                "max_workflow_attempts": 2,
                "first_failure_action": "diagnose_rebuild_plan_and_retry_workflow",
                "second_failure_action": "pause_task_and_report_problem",
            },
            output_contract=_output_contract(),
            workflow_steps=_workflow_steps(),
            issues=issues,
            diagnostics=diagnostics,
        )


def _workflow_steps() -> List[Dict[str, Any]]:
    return [
        {"id": "discover_categories", "depends_on": [], "parallel": False},
        {
            "id": "enumerate_catalog_pages",
            "depends_on": ["discover_categories"],
            "parallel": True,
        },
        {
            "id": "fetch_all_product_details",
            "depends_on": ["enumerate_catalog_pages"],
            "parallel": True,
        },
        {
            "id": "normalize_and_save",
            "depends_on": ["fetch_all_product_details"],
            "parallel": True,
        },
        {
            "id": "validate_count_and_fields",
            "depends_on": ["normalize_and_save"],
            "parallel": False,
        },
        {
            "id": "diagnose_and_retry_once",
            "depends_on": ["validate_count_and_fields"],
            "on_failure": True,
        },
        {
            "id": "pause_and_report",
            "depends_on": ["diagnose_and_retry_once"],
            "on_second_failure": True,
        },
    ]


def _output_contract() -> Dict[str, Any]:
    return {
        "published_outputs": {
            "categories.json": {
                "content": "完整产品分类树",
                "reference": "ickey/data/categories.json",
            },
            "products_final.json": {
                "content": "全部商品基础信息与 attributes 详细参数",
                "reference": "ickey/data/products_final.json",
                "fields": [
                    "cate_id",
                    "sku",
                    "title",
                    "stock",
                    "price",
                    "manufacturer",
                    "moq",
                    "package",
                    "image_url",
                    "detail_url",
                    "description",
                    "detail_title",
                    "attributes",
                    "datasheet_url",
                ],
            },
        },
        "internal_outputs": {
            "run_state/crawl_plan.json": "CrawlPlanAgent 计划和证据",
            "run_state/tasks.jsonl": "Agent 任务状态和依赖",
            "run_state/checkpoints.json": "分类、分页、cursor 和详情断点",
            "run_state/issues.jsonl": "错误、诊断和重试记录",
            "run_state/product_seeds.jsonl": (
                "CatalogAgent 与 ProductAgent 的内部交接数据"
            ),
            "run_state/product_checkpoints.json": (
                "ProductAgent 已完成/失败商品详情断点"
            ),
            "run_state/product_details.jsonl": (
                "ProductAgent 追加式详情结果；失败和稀疏商品也保留"
            ),
        },
        "publish_internal_outputs": False,
        "retain_internal_outputs": True,
    }


__all__ = ["CrawlPlan", "CrawlPlanAgent"]
