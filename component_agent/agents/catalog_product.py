"""ProductAgent variant that normalizes catalog seeds without detail requests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from ..catalog.checkpoint import JsonlJournal
from ..catalog.models import ProductSeed
from ..models import utc_now
from ..planning.models import CrawlPlan
from ..product.checkpoint import ProductCheckpointStore, ProductDetailJournal
from ..product.models import NormalizedProduct, ProductCheckpoint, ProductResult
from .product import ProductAgent


class CatalogOnlyProductAgent(ProductAgent):
    """Keep ProductAgent's handoff contract while making zero detail requests."""

    def run(
        self,
        product_seeds: Sequence[ProductSeed | Dict[str, Any]] | Dict[str, Any] | Any,
        plan: CrawlPlan | Dict[str, Any],
    ) -> ProductResult:
        if isinstance(plan, dict):
            plan = CrawlPlan.from_dict(plan)
        seeds, duplicate_count = _coerce_seeds(product_seeds)
        paths = self._state_paths()
        checkpoint_store = ProductCheckpointStore(paths["checkpoint"])
        detail_journal = ProductDetailJournal(paths["details"])
        task_journal = JsonlJournal(paths["tasks"])
        issue_journal = JsonlJournal(paths["issues"])
        detail_journal.ensure_exists()
        task_journal.ensure_exists()
        issue_journal.ensure_exists()

        fingerprint = _fingerprint(plan.site_key, seeds)
        checkpoint = checkpoint_store.load()
        if checkpoint is None or checkpoint.input_fingerprint != fingerprint:
            checkpoint = ProductCheckpoint(
                site_key=plan.site_key,
                input_fingerprint=fingerprint,
                total_input_count=len(seeds),
            )
        latest = detail_journal.load_latest(plan.site_key)
        completed = {
            key
            for key in checkpoint.completed_keys
            if key in latest and latest[key].fetch_status == "complete"
        }
        pending = [seed for seed in seeds if seed.dedup_key not in completed]
        task_journal.append({
            "event": "product_run_started",
            "agent": "ProductAgent",
            "mode": "catalog_only",
            "site_key": plan.site_key,
            "input_count": len(seeds),
            "pending_count": len(pending),
            "network_requests": 0,
            "timestamp": utc_now(),
        })

        buffer: List[NormalizedProduct] = []
        for seed in pending:
            product = NormalizedProduct.from_seed(seed)
            product.fetch_status = "complete"
            product.transport = "catalog_only"
            product.status_code = 200
            product.source_url = seed.source_url
            product.catalog_source_url = seed.source_url
            product.missing_fields = _missing_fields(product, plan)
            product.fetched_at = utc_now()
            product.ensure_defaults()
            latest[seed.dedup_key] = product
            completed.add(seed.dedup_key)
            buffer.append(product)
            if len(buffer) >= 1000:
                detail_journal.append(buffer)
                buffer.clear()
        if buffer:
            detail_journal.append(buffer)

        checkpoint.completed_keys = sorted(completed)
        checkpoint.failed_keys = []
        checkpoint.total_input_count = len(seeds)
        checkpoint_store.save(checkpoint)
        products = [latest[seed.dedup_key] for seed in seeds]
        task_journal.append({
            "event": "product_run_completed",
            "agent": "ProductAgent",
            "mode": "catalog_only",
            "site_key": plan.site_key,
            "status": "complete",
            "completed_count": len(products),
            "failed_count": 0,
            "network_requests": 0,
            "timestamp": utc_now(),
        })
        return ProductResult(
            status="complete",
            products=products,
            completed_count=len(products),
            failed_count=0,
            skipped_count=duplicate_count + (len(seeds) - len(pending)),
            checkpoint_path=str(checkpoint_store.path),
            detail_output_path=str(detail_journal.path),
            issues=[],
            max_concurrency=1,
            request_interval_seconds=0.0,
        )


def _coerce_seeds(value: Any) -> tuple[List[ProductSeed], int]:
    if hasattr(value, "product_seeds"):
        value = value.product_seeds
    elif isinstance(value, dict) and "product_seeds" in value:
        value = value["product_seeds"]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("CatalogOnlyProductAgent 需要 ProductSeed 序列或 CatalogResult")
    seeds: List[ProductSeed] = []
    seen = set()
    duplicates = 0
    for item in value:
        seed = item if isinstance(item, ProductSeed) else ProductSeed.from_dict(item)
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


def _fingerprint(site_key: str, seeds: Sequence[ProductSeed]) -> str:
    payload = json.dumps(
        {"site_key": site_key, "seed_keys": [seed.dedup_key for seed in seeds]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _missing_fields(product: NormalizedProduct, plan: CrawlPlan) -> List[str]:
    missing: List[str] = []
    fields = plan.validation_policy.get("required_fields", [])
    for field in fields if isinstance(fields, list) else []:
        normalized = str(field).strip().lower()
        if normalized in {"part_number", "model", "mpn"}:
            value = product.part_number or product.model or product.sku
        else:
            value = getattr(product, normalized, product.extra.get(normalized))
        if value in (None, "", [], {}):
            missing.append(str(field))
    return missing


__all__ = ["CatalogOnlyProductAgent"]
