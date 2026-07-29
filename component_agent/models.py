from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ALL_FIELDS: Tuple[str, ...] = (
    "model",
    "price",
    "stock",
    "package",
    "manufacturer",
    "sku",
    "title",
    "description",
    "moq",
    "attributes",
    "datasheet_url",
    "image_url",
)

IDENTITY_FIELDS = {
    "site",
    "supplier",
    "model",
    "sku",
    "title",
    "detail_url",
    "source_url",
    "fetched_at",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PageKind(str, Enum):
    JSON_API = "json_api"
    NEXT_SSR = "next_ssr"
    JSON_LD = "json_ld"
    STATIC_HTML = "static_html"
    JAVASCRIPT_RENDERED = "javascript_rendered"
    ANTI_BOT_CHALLENGE = "anti_bot_challenge"
    EMPTY = "empty"


@dataclass(frozen=True)
class CrawlRequest:
    query: str
    fields: Tuple[str, ...] = ALL_FIELDS

    def __post_init__(self) -> None:
        query = self.query.strip()
        if not query:
            raise ValueError("query 不能为空")
        object.__setattr__(self, "query", query)
        normalized = tuple(dict.fromkeys(f.strip().lower() for f in self.fields if f.strip()))
        object.__setattr__(self, "fields", normalized or ALL_FIELDS)


@dataclass
class FetchResult:
    url: str
    text: str
    status_code: int
    content_type: str = ""
    transport: str = "requests"
    elapsed_ms: int = 0
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class NetworkObservation:
    url: str
    method: str = "GET"
    resource_type: str = ""
    status_code: int = 0
    content_type: str = ""
    response_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "resource_type": self.resource_type,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "response_size": len(self.response_text),
        }


@dataclass
class BrowserInspectionResult:
    page: FetchResult
    responses: List[NetworkObservation] = field(default_factory=list)
    clicked_next: bool = False
    next_selector: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": {
                "url": self.page.url,
                "status_code": self.page.status_code,
                "transport": self.page.transport,
                "elapsed_ms": self.page.elapsed_ms,
            },
            "responses": [response.to_dict() for response in self.responses],
            "clicked_next": self.clicked_next,
            "next_selector": self.next_selector,
        }


@dataclass
class CrawlIssue:
    site: str
    stage: str
    code: str
    message: str
    url: str = ""
    retryable: bool = False
    product: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "site": self.site,
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "url": self.url,
            "retryable": self.retryable,
            "product": self.product,
        }


@dataclass
class PageDiagnostic:
    site: str
    stage: str
    url: str
    page_kind: PageKind
    transport: str
    status_code: int
    elapsed_ms: int
    product_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = vars(self).copy()
        data["page_kind"] = self.page_kind.value
        return data


@dataclass
class ProductRecord:
    site: str
    supplier: str
    model: str = ""
    sku: str = ""
    title: str = ""
    manufacturer: str = ""
    price: List[Dict[str, Any]] = field(default_factory=list)
    stock: Any = None
    package: str = ""
    moq: Any = None
    description: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    datasheet_url: str = ""
    image_url: str = ""
    detail_url: str = ""
    source_url: str = ""
    fetched_at: str = field(default_factory=utc_now)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return (self.model or self.sku or self.detail_url or self.title).strip().upper()

    def relevance(self, query: str) -> int:
        needle = _compact(query)
        candidates = [_compact(self.model), _compact(self.sku), _compact(self.title)]
        if any(value == needle for value in candidates if value):
            return 100
        if any(needle in value for value in candidates if value):
            return 80
        if any(value in needle for value in candidates if len(value) >= 5):
            return 50
        return 0

    def merge(self, detail: "ProductRecord") -> "ProductRecord":
        """Merge detail data without discarding richer catalog price/stock values."""
        merged = ProductRecord(**self.to_dict(include_extra=True))
        merged.extra = {**self.extra, **detail.extra}
        for name in (
            "model",
            "sku",
            "title",
            "manufacturer",
            "moq",
            "description",
            "datasheet_url",
            "image_url",
            "detail_url",
        ):
            value = getattr(detail, name)
            if value not in (None, "", [], {}):
                setattr(merged, name, value)
        if not merged.package and detail.package:
            merged.package = detail.package
        if not merged.price or len(detail.price) > len(merged.price):
            merged.price = detail.price or merged.price
        elif detail.price and detail.price != merged.price:
            merged.extra["detail_price"] = detail.price
        if isinstance(detail.stock, (int, float)):
            merged.stock = detail.stock
        elif merged.stock in (None, ""):
            merged.stock = detail.stock
        elif detail.stock not in (None, ""):
            merged.extra["stock_status"] = detail.stock
        merged.attributes = {**self.attributes, **detail.attributes}
        merged.source_url = detail.source_url or self.source_url
        merged.fetched_at = detail.fetched_at or self.fetched_at
        return merged

    def missing_fields(self, fields: Sequence[str]) -> List[str]:
        missing: List[str] = []
        for name in fields:
            if not hasattr(self, name):
                continue
            if getattr(self, name) in (None, "", [], {}):
                missing.append(name)
        return missing

    def to_dict(
        self,
        requested_fields: Optional[Sequence[str]] = None,
        include_extra: bool = False,
    ) -> Dict[str, Any]:
        data = {
            "site": self.site,
            "supplier": self.supplier,
            "model": self.model,
            "sku": self.sku,
            "title": self.title,
            "manufacturer": self.manufacturer,
            "price": self.price,
            "stock": self.stock,
            "package": self.package,
            "moq": self.moq,
            "description": self.description,
            "attributes": self.attributes,
            "datasheet_url": self.datasheet_url,
            "image_url": self.image_url,
            "detail_url": self.detail_url,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
        }
        if include_extra:
            data["extra"] = self.extra.copy()
        if requested_fields is None:
            return data
        allowed = IDENTITY_FIELDS | set(requested_fields)
        return {key: value for key, value in data.items() if key in allowed}


@dataclass
class CrawlReport:
    request: CrawlRequest
    products: List[ProductRecord]
    issues: List[CrawlIssue]
    diagnostics: List[PageDiagnostic]
    output_dir: str = ""
    started_at: str = field(default_factory=utc_now)
    finished_at: str = field(default_factory=utc_now)

    @property
    def status(self) -> str:
        if self.products and self.issues:
            return "partial"
        if self.products:
            return "success"
        return "failed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.request.query,
            "requested_fields": list(self.request.fields),
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_dir": self.output_dir,
            "result_count": len(self.products),
            "results": [p.to_dict(self.request.fields) for p in self.products],
            "issues": [issue.to_dict() for issue in self.issues],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def _compact(value: Any) -> str:
    return "".join(str(value or "").upper().split()).replace("-", "")
