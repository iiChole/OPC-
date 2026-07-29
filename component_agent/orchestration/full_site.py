"""End-to-end site crawl coordinator for the multi-agent workflow."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from ..agents.catalog import CatalogAgent
from ..agents.catalog_product import CatalogOnlyProductAgent
from ..agents.crawl_plan import CrawlPlanAgent
from ..agents.decision import (
    WebsiteDecision,
    WebsiteDecisionAgent,
    WebsiteType,
)
from ..agents.product import ProductAgent
from ..planning.models import CrawlPlan, FetchTool
from ..sites import SiteAdapter, find_site_adapter
from ..tools import AdaptiveFetchTool
from .robots import RobotsAwareFetchTool, RobotsChecker, RobotsPolicy
from .validation import CrawlExecutionSnapshot, CrawlResultValidator


@dataclass
class FullSiteCrawlResult:
    status: str
    target_url: str
    run_state_dir: str
    scope: str
    adapter: str = "generic"
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    robots: Dict[str, Any] = field(default_factory=dict)
    published_files: Dict[str, str] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "target_url": self.target_url,
            "run_state_dir": self.run_state_dir,
            "scope": self.scope,
            "adapter": self.adapter,
            "attempts": self.attempts,
            "robots": self.robots,
            "published_files": self.published_files,
            "error": self.error,
        }


class FullSiteCrawlCoordinator:
    """Select adapters and run plan -> catalog -> product -> validation itself."""

    def __init__(
        self,
        run_state_root: Path | str = "component_agent/run_state",
        timeout: int = 30,
        retries: int = 2,
        delay: float = 0.5,
        browser_enabled: bool = True,
        headless: bool = True,
        max_concurrency: int = 4,
        max_pages_per_category: int = 100_000,
        max_categories: int = 0,
        max_workflow_attempts: int = 2,
        field_completeness_threshold: float = 1.0,
        decision_agent: Optional[WebsiteDecisionAgent] = None,
        robots_checker: Optional[RobotsChecker] = None,
        fetch_tool: Optional[FetchTool] = None,
        adapter: Optional[SiteAdapter] = None,
    ) -> None:
        self.run_state_root = Path(run_state_root)
        self.timeout = timeout
        self.retries = retries
        self.delay = max(0.0, delay)
        self.browser_enabled = browser_enabled
        self.headless = headless
        self.max_concurrency = max(1, max_concurrency)
        self.max_pages_per_category = max(1, max_pages_per_category)
        self.max_categories = max(0, max_categories)
        self.max_workflow_attempts = max(1, max_workflow_attempts)
        self.field_completeness_threshold = min(
            1.0,
            max(0.0, field_completeness_threshold),
        )
        self.decision_agent = decision_agent or WebsiteDecisionAgent()
        self.robots_checker = robots_checker or RobotsChecker(timeout=timeout)
        self.fetch_tool = fetch_tool
        self.adapter = adapter

    def run(self, url: str) -> FullSiteCrawlResult:
        target_url = _normalize_url(url)
        adapter = self.adapter or find_site_adapter(target_url)
        decision = self._resolve_decision(target_url, adapter)
        run_dir = self._run_dir(decision.site_key or adapter.key if adapter else "site")
        run_dir.mkdir(parents=True, exist_ok=True)
        scope = "sample" if self.max_categories else "full"

        robots = self.robots_checker.check(target_url)
        _write_json(run_dir / "robots.json", robots.to_dict())
        if not robots.allowed:
            result = FullSiteCrawlResult(
                status="paused",
                target_url=target_url,
                run_state_dir=str(run_dir.resolve()),
                scope=scope,
                adapter=adapter.key if adapter else "generic",
                robots=robots.to_dict(),
                error=robots.reason,
            )
            _write_json(run_dir / "crawl_summary.json", result.to_dict())
            return result

        effective_delay = max(self.delay, robots.crawl_delay or 0.0)
        delegate = self.fetch_tool or AdaptiveFetchTool(
            timeout=self.timeout,
            retries=self.retries,
            delay=effective_delay,
            browser_enabled=self.browser_enabled,
            headless=self.headless,
        )
        safe_fetch = RobotsAwareFetchTool(delegate, robots)
        attempts: List[Dict[str, Any]] = []
        last_error = ""

        for attempt in range(1, self.max_workflow_attempts + 1):
            try:
                plan = self._build_plan(
                    target_url,
                    decision,
                    adapter,
                    safe_fetch,
                    robots,
                )
                _write_json(run_dir / "crawl_plan.json", plan.to_dict())
                catalog = CatalogAgent(
                    fetch_tool=safe_fetch,
                    parser=adapter.catalog_parser() if adapter else None,
                    run_state_dir=run_dir,
                    max_pages_per_category=self.max_pages_per_category,
                ).run(plan)
                if catalog.status != "complete":
                    attempt_data = {
                        "attempt": attempt,
                        "plan_status": plan.status,
                        "catalog": _catalog_summary(catalog),
                        "product": None,
                        "validation": None,
                    }
                    attempts.append(attempt_data)
                    last_error = (
                        catalog.handoff.reason
                        if catalog.handoff is not None
                        else f"catalog_status={catalog.status}"
                    )
                    if attempt < self.max_workflow_attempts:
                        continue
                    break

                detail_policy = plan.execution_policy.get("detail_fetch")
                detail_policy = detail_policy if isinstance(detail_policy, dict) else {}
                if (
                    detail_policy.get("enabled") is False
                    or detail_policy.get("mode") == "catalog_only"
                ):
                    product_agent = CatalogOnlyProductAgent(run_state_dir=run_dir)
                else:
                    product_agent = ProductAgent(
                        fetch_tool=safe_fetch,
                        run_state_dir=run_dir,
                        max_concurrency=self.max_concurrency,
                        request_interval_seconds=effective_delay,
                    )
                product = product_agent.run(catalog, plan)
                validation = self._validate(plan, catalog, product)
                _write_json(run_dir / "validation.json", validation.to_dict())
                attempt_data = {
                    "attempt": attempt,
                    "plan_status": plan.status,
                    "catalog": _catalog_summary(catalog),
                    "product": _product_summary(product),
                    "validation": validation.to_dict(),
                }
                attempts.append(attempt_data)
                if validation.valid:
                    categories_path = run_dir / "categories.json"
                    products_path = run_dir / "products_final.json"
                    _write_json(categories_path, catalog.categories)
                    _write_json(
                        products_path,
                        [item.to_dict() for item in product.products],
                    )
                    result = FullSiteCrawlResult(
                        status="complete",
                        target_url=target_url,
                        run_state_dir=str(run_dir.resolve()),
                        scope=scope,
                        adapter=adapter.key if adapter else "generic",
                        attempts=attempts,
                        robots=robots.to_dict(),
                        published_files={
                            "categories": str(categories_path.resolve()),
                            "products_final": str(products_path.resolve()),
                        },
                    )
                    _write_json(run_dir / "crawl_summary.json", result.to_dict())
                    return result
                last_error = "validation_failed"
            except Exception as exc:
                last_error = str(exc)
                attempts.append({
                    "attempt": attempt,
                    "error": str(exc),
                })

        result = FullSiteCrawlResult(
            status="paused",
            target_url=target_url,
            run_state_dir=str(run_dir.resolve()),
            scope=scope,
            adapter=adapter.key if adapter else "generic",
            attempts=attempts,
            robots=robots.to_dict(),
            error=last_error,
        )
        _write_json(run_dir / "crawl_summary.json", result.to_dict())
        return result

    def _build_plan(
        self,
        url: str,
        decision: WebsiteDecision,
        adapter: Optional[SiteAdapter],
        fetch_tool: FetchTool,
        robots: RobotsPolicy,
    ) -> CrawlPlan:
        if adapter is not None:
            return adapter.build_plan(
                url,
                decision,
                fetch_tool,
                robots,
                category_limit=self.max_categories,
            )
        return CrawlPlanAgent(
            fetch_tool=fetch_tool,
            decision_agent=self.decision_agent,
        ).run(url, decision=decision)

    def _resolve_decision(
        self,
        url: str,
        adapter: Optional[SiteAdapter],
    ) -> WebsiteDecision:
        decision = self.decision_agent.decide(url)
        if decision.website_type is not WebsiteType.UNKNOWN or adapter is None:
            return decision
        if adapter.key == "icgoo":
            return WebsiteDecision(
                input_site=url,
                normalized_host=urlsplit(url).hostname or "",
                site_key="icgoo",
                site_name="ICGOO 在线商城",
                website_type=WebsiteType.MARKETPLACE_ECOMMERCE,
                confidence=1.0,
                matched_by="site_adapter",
                matched_value="icgoo",
            )
        return decision

    def _run_dir(self, site_key: str) -> Path:
        safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", site_key).strip("._") or "site"
        if self.max_categories:
            safe_key = f"{safe_key}_sample_{self.max_categories}"
        return self.run_state_root / safe_key

    def _validate(self, plan, catalog, product):
        products = [item.to_dict() for item in product.products]
        failed_tasks = [
            {
                "task_id": item.dedup_key,
                "url": item.source_url,
                "status_code": item.status_code,
                "message": item.error or "product fetch failed",
            }
            for item in product.products
            if item.fetch_status != "complete"
        ]
        snapshot = CrawlExecutionSnapshot(
            products=products,
            discovered_product_count=len(catalog.product_seeds),
            reported_product_count=None,
            failed_tasks=failed_tasks,
            unfinished_task_count=product.failed_count,
            issues=(
                list(plan.issues)
                + [item.to_dict() for item in catalog.issues]
                + [item.to_dict() for item in product.issues]
            ),
            categories=catalog.categories,
        )
        return CrawlResultValidator(
            required_fields=plan.validation_policy.get("required_fields", []),
            field_completeness_threshold=self.field_completeness_threshold,
        ).validate(snapshot)


def _normalize_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        raise ValueError("网站 URL 不能为空")
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持有效的 HTTP/HTTPS 网站 URL")
    return url


def _catalog_summary(catalog) -> Dict[str, Any]:
    return {
        "status": catalog.status,
        "product_seed_count": len(catalog.product_seeds),
        "completed_category_count": catalog.completed_category_count,
        "skipped_category_count": catalog.skipped_category_count,
        "duplicate_product_count": catalog.duplicate_product_count,
        "issues": [item.to_dict() for item in catalog.issues],
        "handoff": catalog.handoff.to_dict() if catalog.handoff else None,
    }


def _product_summary(product) -> Dict[str, Any]:
    return {
        "status": product.status,
        "product_count": len(product.products),
        "completed_count": product.completed_count,
        "failed_count": product.failed_count,
        "skipped_count": product.skipped_count,
        "issues": [item.to_dict() for item in product.issues],
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


__all__ = ["FullSiteCrawlCoordinator", "FullSiteCrawlResult"]
