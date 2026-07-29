"""Normalized product-detail contracts used by ProductAgent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..catalog.models import ProductSeed
from ..models import utc_now


@dataclass
class NormalizedProduct:
    site_key: str
    category_id: str = ""
    category_name: str = ""
    sku: str = ""
    product_id: str = ""
    part_number: str = ""
    model: str = ""
    title: str = ""
    detail_title: str = ""
    manufacturer: str = ""
    package: str = ""
    stock: Any = None
    price: Any = None
    moq: Any = None
    description: str = ""
    image_url: str = ""
    datasheet_url: str = ""
    detail_url: str = ""
    source_url: str = ""
    catalog_source_url: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    dedup_key: str = ""
    dedup_method: str = ""
    fetched_at: str = field(default_factory=utc_now)
    fetch_status: str = "pending"
    transport: str = ""
    status_code: int = 0
    missing_fields: List[str] = field(default_factory=list)
    error: str = ""

    @classmethod
    def from_seed(cls, seed: ProductSeed) -> "NormalizedProduct":
        catalog_raw = (
            seed.extra.get("catalog_raw")
            if isinstance(seed.extra, dict)
            and isinstance(seed.extra.get("catalog_raw"), dict)
            else {}
        )
        part_number = _first_text(
            catalog_raw,
            "partNumber",
            "mpn",
            "manufacturerPartNumber",
            "productModel",
            "model",
        )
        title = (
            seed.title
            or part_number
            or seed.sku
            or seed.product_id
            or "unknown_product"
        )
        return cls(
            site_key=seed.site_key,
            category_id=seed.category_id,
            category_name=seed.category_name,
            sku=seed.sku,
            product_id=seed.product_id,
            part_number=part_number,
            model=part_number,
            title=title,
            manufacturer=seed.manufacturer,
            package=seed.package,
            stock=seed.stock,
            price=seed.price,
            moq=seed.moq,
            description=seed.description,
            image_url=seed.image_url,
            datasheet_url=seed.datasheet_url,
            detail_url=seed.detail_url,
            source_url=seed.detail_url or seed.source_url,
            catalog_source_url=seed.source_url,
            attributes=_safe_dict(seed.attributes),
            extra=_safe_dict(seed.extra),
            dedup_key=seed.dedup_key,
            dedup_method=seed.dedup_method,
        )

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "NormalizedProduct":
        return cls(
            site_key=str(value.get("site_key", value.get("site", ""))),
            category_id=str(value.get("category_id", value.get("cate_id", ""))),
            category_name=str(value.get("category_name", "")),
            sku=str(value.get("sku", "")),
            product_id=str(value.get("product_id", "")),
            part_number=str(value.get("part_number", value.get("model", ""))),
            model=str(value.get("model", value.get("part_number", ""))),
            title=str(value.get("title", "") or "unknown_product"),
            detail_title=str(value.get("detail_title", "")),
            manufacturer=str(value.get("manufacturer", "")),
            package=str(value.get("package", "")),
            stock=value.get("stock"),
            price=value.get("price"),
            moq=value.get("moq"),
            description=str(value.get("description", "")),
            image_url=str(value.get("image_url", "")),
            datasheet_url=str(value.get("datasheet_url", "")),
            detail_url=str(value.get("detail_url", "")),
            source_url=str(value.get("source_url", "")),
            catalog_source_url=str(value.get("catalog_source_url", "")),
            attributes=_safe_dict(value.get("attributes")),
            extra=_safe_dict(value.get("extra")),
            dedup_key=str(value.get("dedup_key", "")),
            dedup_method=str(value.get("dedup_method", "")),
            fetched_at=str(value.get("fetched_at", "") or utc_now()),
            fetch_status=str(value.get("fetch_status", "complete")),
            transport=str(value.get("transport", "")),
            status_code=int(value.get("status_code", 0) or 0),
            missing_fields=list(value.get("missing_fields") or []),
            error=str(value.get("error", "")),
        )

    def ensure_defaults(self) -> None:
        self.title = (
            self.title
            or self.detail_title
            or self.part_number
            or self.model
            or self.sku
            or self.product_id
            or "unknown_product"
        )
        if not isinstance(self.attributes, dict):
            self.attributes = {}
        if not isinstance(self.extra, dict):
            self.extra = {}

    def to_dict(self) -> Dict[str, Any]:
        self.ensure_defaults()
        return {
            "site_key": self.site_key,
            "cate_id": self.category_id,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "sku": self.sku,
            "product_id": self.product_id,
            "part_number": self.part_number,
            "model": self.model,
            "title": self.title,
            "detail_title": self.detail_title,
            "manufacturer": self.manufacturer,
            "package": self.package,
            "stock": self.stock,
            "price": self.price,
            "moq": self.moq,
            "description": self.description,
            "image_url": self.image_url,
            "datasheet_url": self.datasheet_url,
            "detail_url": self.detail_url,
            "source_url": self.source_url,
            "catalog_source_url": self.catalog_source_url,
            "attributes": dict(self.attributes),
            "extra": dict(self.extra),
            "dedup_key": self.dedup_key,
            "dedup_method": self.dedup_method,
            "fetched_at": self.fetched_at,
            "fetch_status": self.fetch_status,
            "transport": self.transport,
            "status_code": self.status_code,
            "missing_fields": list(self.missing_fields),
            "error": self.error,
        }


@dataclass
class ProductIssue:
    code: str
    message: str
    dedup_key: str = ""
    url: str = ""
    retryable: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "dedup_key": self.dedup_key,
            "url": self.url,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass
class ProductCheckpoint:
    site_key: str
    input_fingerprint: str
    completed_keys: List[str] = field(default_factory=list)
    failed_keys: List[str] = field(default_factory=list)
    total_input_count: int = 0
    updated_at: str = field(default_factory=utc_now)
    version: int = 1

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ProductCheckpoint":
        return cls(
            site_key=str(value.get("site_key", "")),
            input_fingerprint=str(value.get("input_fingerprint", "")),
            completed_keys=list(value.get("completed_keys") or []),
            failed_keys=list(value.get("failed_keys") or []),
            total_input_count=int(value.get("total_input_count", 0) or 0),
            updated_at=str(value.get("updated_at", "") or utc_now()),
            version=int(value.get("version", 1) or 1),
        )

    def to_dict(self) -> Dict[str, Any]:
        self.updated_at = utc_now()
        return {
            "version": self.version,
            "site_key": self.site_key,
            "input_fingerprint": self.input_fingerprint,
            "completed_keys": list(self.completed_keys),
            "failed_keys": list(self.failed_keys),
            "total_input_count": self.total_input_count,
            "updated_at": self.updated_at,
        }


@dataclass
class ProductFetchOutcome:
    index: int
    seed: ProductSeed
    product: NormalizedProduct
    issue: Optional[ProductIssue] = None


@dataclass
class ProductResult:
    status: str
    products: List[NormalizedProduct]
    completed_count: int
    failed_count: int
    skipped_count: int
    checkpoint_path: str
    detail_output_path: str
    issues: List[ProductIssue] = field(default_factory=list)
    max_concurrency: int = 1
    request_interval_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "internal_output_only": True,
            "publish_final_output": False,
            "product_count": len(self.products),
            "products": [product.to_dict() for product in self.products],
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "checkpoint_path": self.checkpoint_path,
            "detail_output_path": self.detail_output_path,
            "issues": [issue.to_dict() for issue in self.issues],
            "max_concurrency": self.max_concurrency,
            "request_interval_seconds": self.request_interval_seconds,
        }


def _first_text(mapping: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}) and not isinstance(value, (dict, list)):
            return str(value).strip()
    return ""


def _safe_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "NormalizedProduct",
    "ProductCheckpoint",
    "ProductFetchOutcome",
    "ProductIssue",
    "ProductResult",
]
