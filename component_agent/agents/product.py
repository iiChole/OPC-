"""Fetch ProductSeed detail pages with bounded concurrency and stable persistence."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..catalog.checkpoint import JsonlJournal
from ..catalog.models import ProductSeed
from ..models import FetchResult, utc_now
from ..planning.models import CrawlPlan, FetchTool
from ..product.checkpoint import ProductCheckpointStore, ProductDetailJournal
from ..product.models import (
    NormalizedProduct,
    ProductCheckpoint,
    ProductFetchOutcome,
    ProductIssue,
    ProductResult,
)
from ..product.parser import ProductDetailParser


class _RequestGate:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, interval_seconds)
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        if self.interval_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_start)
            self._next_start = start_at + self.interval_seconds
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)


class ProductAgent:
    """Fetch and normalize every ProductSeed without dropping sparse products."""

    def __init__(
        self,
        fetch_tool: Optional[FetchTool] = None,
        parser: Optional[ProductDetailParser] = None,
        run_state_dir: Path | str = "run_state",
        max_concurrency: int = 4,
        request_interval_seconds: float = 0.25,
        refresh_existing: bool = False,
        retry_failed: bool = True,
        timeout: int = 30,
        retries: int = 2,
        delay: float = 0.35,
        browser_enabled: bool = True,
        headless: bool = True,
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
        self.parser = parser or ProductDetailParser()
        self.run_state_dir = Path(run_state_dir)
        self.max_concurrency = min(16, max(1, max_concurrency))
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self.refresh_existing = refresh_existing
        self.retry_failed = retry_failed

    def run(
        self,
        product_seeds: Sequence[ProductSeed | Dict[str, Any]]
        | Dict[str, Any]
        | Any,
        plan: CrawlPlan | Dict[str, Any],
    ) -> ProductResult:
        if isinstance(plan, dict):
            plan = CrawlPlan.from_dict(plan)
        seeds, duplicate_input_count = _coerce_product_seeds(product_seeds)
        policy = _detail_fetch_policy(plan)
        effective_concurrency = min(
            self.max_concurrency,
            _positive_int(policy.get("max_concurrency"), self.max_concurrency),
        )
        effective_interval = max(
            self.request_interval_seconds,
            _nonnegative_float(
                policy.get("request_interval_seconds"),
                self.request_interval_seconds,
            ),
        )
        paths = self._state_paths()
        checkpoint_store = ProductCheckpointStore(paths["checkpoint"])
        detail_journal = ProductDetailJournal(paths["details"])
        task_journal = JsonlJournal(paths["tasks"])
        issue_journal = JsonlJournal(paths["issues"])
        detail_journal.ensure_exists()
        task_journal.ensure_exists()
        issue_journal.ensure_exists()

        fingerprint = _input_fingerprint(plan, seeds)
        issues: List[ProductIssue] = []
        checkpoint = self._load_or_create_checkpoint(
            checkpoint_store,
            plan,
            seeds,
            fingerprint,
            issues,
            issue_journal,
        )
        try:
            latest = detail_journal.load_latest(plan.site_key)
        except (OSError, ValueError) as exc:
            latest = {}
            issue = ProductIssue(
                code="product_detail_journal_load_failed",
                message=str(exc),
                retryable=True,
            )
            issues.append(issue)
            issue_journal.append(_issue_event(issue))

        seed_keys = {seed.dedup_key for seed in seeds if seed.dedup_key}
        completed = {
            key
            for key in checkpoint.completed_keys
            if key in seed_keys
            and key in latest
            and latest[key].fetch_status == "complete"
        }
        completed.update(
            key
            for key, product in latest.items()
            if key in seed_keys and product.fetch_status == "complete"
        )
        failed = {
            key
            for key in checkpoint.failed_keys
            if key in seed_keys and key not in completed
        }
        skipped_count = duplicate_input_count

        if self.refresh_existing:
            completed.clear()
        pending: List[Tuple[int, ProductSeed]] = []
        for index, seed in enumerate(seeds):
            if seed.dedup_key in completed:
                skipped_count += 1
                continue
            if (
                seed.dedup_key in failed
                and not self.retry_failed
                and seed.dedup_key in latest
            ):
                skipped_count += 1
                continue
            pending.append((index, seed))

        checkpoint.completed_keys = sorted(completed)
        checkpoint.failed_keys = sorted(failed)
        checkpoint_store.save(checkpoint)
        task_journal.append({
            "event": "product_run_started",
            "agent": "ProductAgent",
            "site_key": plan.site_key,
            "input_count": len(seeds),
            "pending_count": len(pending),
            "max_concurrency": effective_concurrency,
            "request_interval_seconds": effective_interval,
            "preferred_transport": policy["preferred_transport"],
            "timestamp": utc_now(),
        })

        gate = _RequestGate(effective_interval)
        result_by_key: Dict[str, NormalizedProduct] = {
            key: value
            for key, value in latest.items()
            if key in seed_keys
        }
        if pending:
            with ThreadPoolExecutor(
                max_workers=effective_concurrency,
                thread_name_prefix="product-detail",
            ) as executor:
                futures: Dict[Future[ProductFetchOutcome], Tuple[int, ProductSeed]] = {
                    executor.submit(
                        self._fetch_one,
                        index,
                        seed,
                        plan,
                        policy,
                        gate,
                    ): (index, seed)
                    for index, seed in pending
                }
                for future in as_completed(futures):
                    index, seed = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = _failed_outcome(
                            index,
                            seed,
                            code="product_worker_failed",
                            message=str(exc),
                            retryable=True,
                        )
                    product = outcome.product
                    product.ensure_defaults()
                    detail_journal.append([product])
                    result_by_key[seed.dedup_key] = product

                    if outcome.issue is not None:
                        issues.append(outcome.issue)
                        issue_journal.append(_issue_event(outcome.issue))
                    if product.fetch_status == "complete":
                        completed.add(seed.dedup_key)
                        failed.discard(seed.dedup_key)
                    else:
                        failed.add(seed.dedup_key)
                        completed.discard(seed.dedup_key)
                    checkpoint.completed_keys = sorted(completed)
                    checkpoint.failed_keys = sorted(failed)
                    checkpoint_store.save(checkpoint)
                    task_journal.append({
                        "event": "product_detail_completed",
                        "agent": "ProductAgent",
                        "dedup_key": seed.dedup_key,
                        "detail_url": seed.detail_url,
                        "fetch_status": product.fetch_status,
                        "transport": product.transport,
                        "status_code": product.status_code,
                        "missing_fields": list(product.missing_fields),
                        "timestamp": utc_now(),
                    })

        products: List[NormalizedProduct] = []
        for seed in seeds:
            product = result_by_key.get(seed.dedup_key)
            if product is None:
                product = NormalizedProduct.from_seed(seed)
                product.fetch_status = "failed"
                product.error = "详情结果不存在"
                product.ensure_defaults()
            products.append(product)

        checkpoint.completed_keys = sorted(completed)
        checkpoint.failed_keys = sorted(failed)
        checkpoint_store.save(checkpoint)
        status = "complete" if not failed else "partial"
        task_journal.append({
            "event": "product_run_completed",
            "agent": "ProductAgent",
            "site_key": plan.site_key,
            "status": status,
            "completed_count": len(completed),
            "failed_count": len(failed),
            "timestamp": utc_now(),
        })
        return ProductResult(
            status=status,
            products=products,
            completed_count=len(completed),
            failed_count=len(failed),
            skipped_count=skipped_count,
            checkpoint_path=str(checkpoint_store.path),
            detail_output_path=str(detail_journal.path),
            issues=issues,
            max_concurrency=effective_concurrency,
            request_interval_seconds=effective_interval,
        )

    def _fetch_one(
        self,
        index: int,
        seed: ProductSeed,
        plan: CrawlPlan,
        policy: Dict[str, Any],
        gate: _RequestGate,
    ) -> ProductFetchOutcome:
        request = _detail_request(seed, policy)
        url = request["url"]
        if not url:
            return _failed_outcome(
                index,
                seed,
                code="detail_url_missing",
                message="ProductSeed 缺少详情 URL，已保留目录字段",
                retryable=False,
            )

        gate.wait()
        try:
            result = self.fetch_tool.fetch(
                url,
                preferred_transport=request["preferred_transport"],
                headers=request["headers"] or None,
            )
        except Exception as exc:
            return _failed_outcome(
                index,
                seed,
                code="detail_fetch_failed",
                message=str(exc),
                retryable=True,
                url=url,
            )
        if result.status_code >= 400:
            return _failed_outcome(
                index,
                seed,
                code="detail_http_error",
                message=f"HTTP {result.status_code}",
                retryable=(
                    result.status_code == 429
                    or result.status_code >= 500
                ),
                url=result.url,
                result=result,
            )

        try:
            product = self.parser.parse(
                result,
                seed,
                supplier_name=_supplier_name(plan),
            )
        except Exception as exc:
            return _failed_outcome(
                index,
                seed,
                code="detail_parse_failed",
                message=str(exc),
                retryable=True,
                url=result.url,
                result=result,
            )
        product.missing_fields = _missing_target_fields(
            product,
            _target_fields(plan),
        )
        issue = None
        if product.missing_fields:
            issue = ProductIssue(
                code="detail_fields_missing",
                message=(
                    "详情页未提供部分目标字段；商品已保留并使用空值"
                ),
                dedup_key=seed.dedup_key,
                url=product.source_url,
                retryable=False,
                details={"missing_fields": list(product.missing_fields)},
            )
        return ProductFetchOutcome(
            index=index,
            seed=seed,
            product=product,
            issue=issue,
        )

    def _load_or_create_checkpoint(
        self,
        store: ProductCheckpointStore,
        plan: CrawlPlan,
        seeds: Sequence[ProductSeed],
        fingerprint: str,
        issues: List[ProductIssue],
        issue_journal: JsonlJournal,
    ) -> ProductCheckpoint:
        try:
            checkpoint = store.load()
        except (OSError, ValueError) as exc:
            checkpoint = None
            issue = ProductIssue(
                code="product_checkpoint_load_failed",
                message=str(exc),
                retryable=True,
            )
            issues.append(issue)
            issue_journal.append(_issue_event(issue))
        if (
            checkpoint is not None
            and checkpoint.site_key == plan.site_key
            and checkpoint.input_fingerprint == fingerprint
        ):
            return checkpoint
        if checkpoint is not None:
            issue = ProductIssue(
                code="product_checkpoint_input_mismatch",
                message="Product checkpoint 与当前 ProductSeed/CrawlPlan 不一致",
                retryable=False,
            )
            issues.append(issue)
            issue_journal.append(_issue_event(issue))
        return ProductCheckpoint(
            site_key=plan.site_key,
            input_fingerprint=fingerprint,
            total_input_count=len(seeds),
        )

    def _state_paths(self) -> Dict[str, Path]:
        return {
            "checkpoint": self.run_state_dir / "product_checkpoints.json",
            "details": self.run_state_dir / "product_details.jsonl",
            "tasks": self.run_state_dir / "tasks.jsonl",
            "issues": self.run_state_dir / "issues.jsonl",
        }


def _coerce_product_seeds(value: Any) -> Tuple[List[ProductSeed], int]:
    if hasattr(value, "product_seeds"):
        value = getattr(value, "product_seeds")
    elif isinstance(value, dict):
        value = value.get("product_seeds", value.get("products", []))
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        raise TypeError("ProductAgent 需要 ProductSeed 序列或 CatalogResult")

    seeds: List[ProductSeed] = []
    seen = set()
    duplicates = 0
    for item in value:
        if isinstance(item, ProductSeed):
            seed = item
        elif isinstance(item, dict):
            normalized_item = dict(item)
            if not isinstance(normalized_item.get("attributes"), dict):
                normalized_item["attributes"] = {}
            if not isinstance(normalized_item.get("extra"), dict):
                normalized_item["extra"] = {}
            seed = ProductSeed.from_dict(normalized_item)
        else:
            raise TypeError("ProductSeed 条目必须是 ProductSeed 或字典")
        if not seed.dedup_key:
            seed.assign_dedup_identity()
        if not seed.dedup_key:
            raise ValueError("ProductSeed 缺少可用去重键")
        if seed.dedup_key in seen:
            duplicates += 1
            continue
        seen.add(seed.dedup_key)
        seeds.append(seed)
    return seeds, duplicates


def _detail_fetch_policy(plan: CrawlPlan) -> Dict[str, Any]:
    configured = plan.execution_policy.get("detail_fetch")
    configured = configured if isinstance(configured, dict) else {}
    return {
        "preferred_transport": _normalize_transport(str(
            configured.get("preferred_transport", "auto") or "auto"
        )),
        "max_concurrency": _positive_int(
            configured.get("max_concurrency"),
            4,
        ),
        "request_interval_seconds": _nonnegative_float(
            configured.get("request_interval_seconds"),
            0.25,
        ),
        "headers": dict(configured.get("headers") or {}),
        "url_template": str(configured.get("url_template", "")),
        "browser_fallback": bool(configured.get("browser_fallback", True)),
        "source_priority": list(configured.get("source_priority") or []),
    }


def _detail_request(
    seed: ProductSeed,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    override = seed.extra.get("detail_request")
    override = override if isinstance(override, dict) else {}
    url = str(override.get("url", "") or "")
    template = str(override.get("url_template", "") or policy["url_template"])
    if not url and template:
        values = {
            "sku": seed.sku,
            "product_id": seed.product_id,
            "detail_url": seed.detail_url,
            "category_id": seed.category_id,
        }
        try:
            url = template.format(**values)
        except (KeyError, ValueError):
            url = ""
    url = url or seed.detail_url
    headers = {**policy["headers"], **dict(override.get("headers") or {})}
    if seed.source_url and "Referer" not in headers:
        headers["Referer"] = seed.source_url
    return {
        "url": url,
        "preferred_transport": _normalize_transport(
            str(
                override.get("preferred_transport", "")
                or policy["preferred_transport"]
            )
        ),
        "headers": headers,
    }


def _failed_outcome(
    index: int,
    seed: ProductSeed,
    code: str,
    message: str,
    retryable: bool,
    url: str = "",
    result: Optional[FetchResult] = None,
) -> ProductFetchOutcome:
    product = NormalizedProduct.from_seed(seed)
    product.fetch_status = "failed"
    product.error = message
    product.source_url = (
        result.url
        if result is not None
        else url or seed.detail_url or seed.source_url
    )
    product.transport = result.transport if result is not None else ""
    product.status_code = result.status_code if result is not None else 0
    product.fetched_at = utc_now()
    product.ensure_defaults()
    issue = ProductIssue(
        code=code,
        message=message,
        dedup_key=seed.dedup_key,
        url=product.source_url,
        retryable=retryable,
    )
    return ProductFetchOutcome(index, seed, product, issue)


def _target_fields(plan: CrawlPlan) -> List[str]:
    required = plan.validation_policy.get("required_fields")
    if isinstance(required, list) and required:
        return [str(value) for value in required]
    handling = plan.decision.get("recommended_handling")
    if isinstance(handling, dict):
        fields = handling.get("target_fields")
        if isinstance(fields, list):
            return [str(value) for value in fields]
    return []


def _missing_target_fields(
    product: NormalizedProduct,
    fields: Sequence[str],
) -> List[str]:
    missing: List[str] = []
    for field_name in fields:
        normalized = str(field_name).strip().lower()
        if not normalized:
            continue
        if normalized in {"part_number", "model", "mpn"}:
            value = product.part_number or product.model or product.sku
        elif normalized in {"stock_availability", "availability"}:
            value = product.stock
        elif normalized in {
            "electrical_parameters",
            "operating_conditions",
            "application_information",
        }:
            value = product.attributes
        elif normalized == "package_information":
            value = product.package or product.attributes
        elif normalized == "product_status":
            value = product.stock or product.extra.get("product_status")
        elif normalized == "quoted_price":
            value = product.price
        else:
            value = getattr(product, normalized, product.extra.get(normalized))
        if value in (None, "", [], {}):
            missing.append(field_name)
    return missing


def _supplier_name(plan: CrawlPlan) -> str:
    recognized = plan.decision.get("recognized_site")
    if isinstance(recognized, dict):
        return str(recognized.get("name", "") or plan.site_key)
    return plan.site_key


def _input_fingerprint(
    plan: CrawlPlan,
    seeds: Sequence[ProductSeed],
) -> str:
    payload = {
        "site_key": plan.site_key,
        "detail_fetch": _detail_fetch_policy(plan),
        "seed_keys": [seed.dedup_key for seed in seeds],
        "detail_urls": [seed.detail_url for seed in seeds],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _issue_event(issue: ProductIssue) -> Dict[str, Any]:
    return {
        "event": "product_issue",
        "agent": "ProductAgent",
        "issue": issue.to_dict(),
        "timestamp": utc_now(),
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return max(1, default)
    return max(1, parsed)


def _nonnegative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return max(0.0, default)
    return max(0.0, parsed)


def _normalize_transport(value: str) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized in {"browser", "javascript", "dynamic"}:
        return "playwright"
    if normalized in {"http", "api"}:
        return "requests"
    if normalized not in {"auto", "requests", "playwright"}:
        return "auto"
    return normalized


__all__ = ["ProductAgent", "ProductResult", "NormalizedProduct"]
