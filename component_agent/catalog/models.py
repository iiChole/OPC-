"""Data contracts used while enumerating a website catalog."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import FetchResult, utc_now
from ..planning.models import CategoryCandidate


@dataclass
class CategoryTask:
    name: str
    url: str = ""
    identifier: str = ""
    parent_key: str = ""
    depth: int = 0
    source: str = "crawl_plan"

    @property
    def key(self) -> str:
        if self.identifier.strip():
            return f"id:{self.identifier.strip().lower()}"
        if self.url.strip():
            return f"url:{canonical_product_url(self.url)}"
        material = f"{self.parent_key}\n{self.name.strip().lower()}"
        return f"name:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    @classmethod
    def from_candidate(
        cls,
        candidate: CategoryCandidate,
        parent_key: str = "",
        depth: int = 0,
    ) -> "CategoryTask":
        return cls(
            name=candidate.name,
            url=candidate.url,
            identifier=candidate.identifier,
            parent_key=parent_key,
            depth=max(0, depth),
            source=candidate.source,
        )

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CategoryTask":
        return cls(
            name=str(value.get("name", "")),
            url=str(value.get("url", "")),
            identifier=str(value.get("identifier", "")),
            parent_key=str(value.get("parent_key", "")),
            depth=int(value.get("depth", 0) or 0),
            source=str(value.get("source", "checkpoint")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "url": self.url,
            "identifier": self.identifier,
            "parent_key": self.parent_key,
            "depth": self.depth,
            "source": self.source,
        }


@dataclass
class ProductSeed:
    site_key: str
    category_id: str = ""
    category_name: str = ""
    sku: str = ""
    product_id: str = ""
    title: str = ""
    stock: Any = None
    price: Any = None
    manufacturer: str = ""
    moq: Any = None
    package: str = ""
    image_url: str = ""
    detail_url: str = ""
    description: str = ""
    datasheet_url: str = ""
    source_url: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    dedup_key: str = ""
    dedup_method: str = ""
    discovered_at: str = field(default_factory=utc_now)

    def assign_dedup_identity(self) -> bool:
        self.dedup_key, self.dedup_method = product_seed_identity(self)
        return bool(self.dedup_key)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ProductSeed":
        seed = cls(
            site_key=str(value.get("site_key", "")),
            category_id=str(value.get("category_id", value.get("cate_id", ""))),
            category_name=str(value.get("category_name", "")),
            sku=str(value.get("sku", "")),
            product_id=str(value.get("product_id", "")),
            title=str(value.get("title", "")),
            stock=value.get("stock"),
            price=value.get("price"),
            manufacturer=str(value.get("manufacturer", "")),
            moq=value.get("moq"),
            package=str(value.get("package", "")),
            image_url=str(value.get("image_url", "")),
            detail_url=str(value.get("detail_url", "")),
            description=str(value.get("description", "")),
            datasheet_url=str(value.get("datasheet_url", "")),
            source_url=str(value.get("source_url", "")),
            attributes=dict(value.get("attributes") or {}),
            extra=dict(value.get("extra") or {}),
            dedup_key=str(value.get("dedup_key", "")),
            dedup_method=str(value.get("dedup_method", "")),
            discovered_at=str(value.get("discovered_at", "") or utc_now()),
        )
        if not seed.dedup_key:
            seed.assign_dedup_identity()
        return seed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "site_key": self.site_key,
            "cate_id": self.category_id,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "sku": self.sku,
            "product_id": self.product_id,
            "title": self.title,
            "stock": self.stock,
            "price": self.price,
            "manufacturer": self.manufacturer,
            "moq": self.moq,
            "package": self.package,
            "image_url": self.image_url,
            "detail_url": self.detail_url,
            "description": self.description,
            "attributes": dict(self.attributes),
            "datasheet_url": self.datasheet_url,
            "source_url": self.source_url,
            "dedup_key": self.dedup_key,
            "dedup_method": self.dedup_method,
            "discovered_at": self.discovered_at,
            "extra": dict(self.extra),
        }


def product_seed_identity(seed: ProductSeed) -> tuple[str, str]:
    """Return the required SKU -> product ID -> URL hash identity."""
    sku = _normalize_identity(seed.sku)
    if sku:
        return f"sku:{sku}", "sku"
    product_id = _normalize_identity(seed.product_id)
    if product_id:
        return f"product_id:{product_id}", "product_id"
    detail_url = canonical_product_url(seed.detail_url)
    if detail_url:
        digest = hashlib.sha256(detail_url.encode("utf-8")).hexdigest()
        return f"url_sha256:{digest}", "url_hash"
    return "", "missing"


@dataclass
class CatalogPage:
    products: List[ProductSeed]
    raw_product_count: int
    child_categories: List[CategoryCandidate] = field(default_factory=list)
    next_cursor: str = ""
    next_url: str = ""
    has_next_control: bool = False
    page_size: Optional[int] = None
    total_count: Optional[int] = None
    signature: str = ""
    source_kind: str = "unknown"


@dataclass
class PaginationState:
    mode: str
    base_url: str
    page_number: int = 1
    offset: int = 0
    cursor: str = ""
    next_url: str = ""
    pages_seen: int = 0
    product_count: int = 0
    reported_total: Optional[int] = None
    page_signatures: List[str] = field(default_factory=list)
    complete: bool = False
    stop_reason: str = ""

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "PaginationState":
        reported_total = value.get("reported_total")
        return cls(
            mode=str(value.get("mode", "auto")),
            base_url=str(value.get("base_url", "")),
            page_number=max(1, int(value.get("page_number", 1) or 1)),
            offset=max(0, int(value.get("offset", 0) or 0)),
            cursor=str(value.get("cursor", "")),
            next_url=str(value.get("next_url", "")),
            pages_seen=max(0, int(value.get("pages_seen", 0) or 0)),
            product_count=max(0, int(value.get("product_count", 0) or 0)),
            reported_total=(
                int(reported_total)
                if reported_total not in (None, "")
                else None
            ),
            page_signatures=list(value.get("page_signatures") or []),
            complete=bool(value.get("complete", False)),
            stop_reason=str(value.get("stop_reason", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "base_url": self.base_url,
            "page_number": self.page_number,
            "offset": self.offset,
            "cursor": self.cursor,
            "next_url": self.next_url,
            "pages_seen": self.pages_seen,
            "product_count": self.product_count,
            "reported_total": self.reported_total,
            "page_signatures": list(self.page_signatures),
            "complete": self.complete,
            "stop_reason": self.stop_reason,
        }


@dataclass
class CatalogCheckpoint:
    site_key: str
    plan_fingerprint: str
    traversal_mode: str
    pending_categories: List[CategoryTask] = field(default_factory=list)
    completed_category_keys: List[str] = field(default_factory=list)
    seen_category_keys: List[str] = field(default_factory=list)
    seen_product_keys: List[str] = field(default_factory=list)
    category_states: Dict[str, PaginationState] = field(default_factory=dict)
    category_records: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)
    version: int = 1

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CatalogCheckpoint":
        return cls(
            site_key=str(value.get("site_key", "")),
            plan_fingerprint=str(value.get("plan_fingerprint", "")),
            traversal_mode=str(value.get("traversal_mode", "dfs")),
            pending_categories=[
                CategoryTask.from_dict(item)
                for item in value.get("pending_categories", [])
                if isinstance(item, dict)
            ],
            completed_category_keys=list(value.get("completed_category_keys") or []),
            seen_category_keys=list(value.get("seen_category_keys") or []),
            seen_product_keys=list(value.get("seen_product_keys") or []),
            category_states={
                str(key): PaginationState.from_dict(item)
                for key, item in (value.get("category_states") or {}).items()
                if isinstance(item, dict)
            },
            category_records=dict(value.get("category_records") or {}),
            issues=list(value.get("issues") or []),
            updated_at=str(value.get("updated_at", "") or utc_now()),
            version=int(value.get("version", 1) or 1),
        )

    def to_dict(self) -> Dict[str, Any]:
        self.updated_at = utc_now()
        return {
            "version": self.version,
            "site_key": self.site_key,
            "plan_fingerprint": self.plan_fingerprint,
            "traversal_mode": self.traversal_mode,
            "pending_categories": [
                category.to_dict()
                for category in self.pending_categories
            ],
            "completed_category_keys": list(self.completed_category_keys),
            "seen_category_keys": list(self.seen_category_keys),
            "seen_product_keys": list(self.seen_product_keys),
            "category_states": {
                key: state.to_dict()
                for key, state in self.category_states.items()
            },
            "category_records": dict(self.category_records),
            "issues": list(self.issues),
            "updated_at": self.updated_at,
        }


@dataclass
class CatalogIssue:
    code: str
    message: str
    category_key: str = ""
    url: str = ""
    retryable: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category_key": self.category_key,
            "url": self.url,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass
class AgentHandoff:
    target_agent: str
    reason: str
    available: bool
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_agent": self.target_agent,
            "reason": self.reason,
            "available": self.available,
            "payload": dict(self.payload),
        }


@dataclass
class CatalogResult:
    status: str
    traversal_mode: str
    product_seeds: List[ProductSeed]
    categories: List[Dict[str, Any]]
    completed_category_count: int
    skipped_category_count: int
    duplicate_product_count: int
    checkpoint_path: str
    product_seed_path: str
    issues: List[CatalogIssue] = field(default_factory=list)
    handoff: Optional[AgentHandoff] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "internal_output_only": True,
            "publish_final_output": False,
            "traversal_mode": self.traversal_mode,
            "product_seed_count": len(self.product_seeds),
            "product_seeds": [seed.to_dict() for seed in self.product_seeds],
            "categories": list(self.categories),
            "completed_category_count": self.completed_category_count,
            "skipped_category_count": self.skipped_category_count,
            "duplicate_product_count": self.duplicate_product_count,
            "checkpoint_path": self.checkpoint_path,
            "product_seed_path": self.product_seed_path,
            "issues": [issue.to_dict() for issue in self.issues],
            "handoff": self.handoff.to_dict() if self.handoff else None,
        }


class NextPaginator(Protocol):
    def paginate_next(
        self,
        url: str,
        next_selector: str = "",
        max_pages: int = 10_000,
    ) -> Iterable[FetchResult]:
        ...


def plan_fingerprint(value: Dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_product_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        query,
        "",
    ))


def _normalize_identity(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


__all__ = [
    "AgentHandoff",
    "CatalogCheckpoint",
    "CatalogIssue",
    "CatalogPage",
    "CatalogResult",
    "CategoryTask",
    "NextPaginator",
    "PaginationState",
    "ProductSeed",
    "canonical_product_url",
    "plan_fingerprint",
    "product_seed_identity",
]
