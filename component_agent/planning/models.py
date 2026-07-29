"""Typed crawl-planning contracts shared by planning and execution agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from ..models import BrowserInspectionResult, FetchResult


STOP_CONDITIONS = (
    "empty_product_list",
    "returned_count_less_than_page_size",
    "missing_next_cursor",
    "next_link_or_control_absent",
    "repeated_page_signature",
)


class FetchTool(Protocol):
    def fetch(
        self,
        url: str,
        preferred_transport: str = "auto",
        headers: Optional[Dict[str, str]] = None,
    ) -> FetchResult:
        ...


class NetworkInspector(Protocol):
    def inspect_network(
        self,
        url: str,
        click_next: bool = False,
        max_response_chars: int = 1_000_000,
    ) -> BrowserInspectionResult:
        ...


@dataclass(frozen=True)
class CategoryCandidate:
    name: str
    url: str = ""
    identifier: str = ""
    source: str = "html_navigation"
    confidence: float = 0.5
    evidence: str = ""

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CategoryCandidate":
        return cls(
            name=str(value.get("name", "")),
            url=str(value.get("url", "")),
            identifier=str(value.get("identifier", "")),
            source=str(value.get("source", "agent_handoff")),
            confidence=float(value.get("confidence", 0.5) or 0.0),
            evidence=str(value.get("evidence", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "identifier": self.identifier,
            "source": self.source,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ApiCandidate:
    url: str
    purpose: str
    source: str
    method: str = "GET"
    status_code: int = 0
    product_list_path: str = ""
    category_list_paths: Tuple[str, ...] = ()
    next_cursor_path: str = ""
    next_cursor_sample: str = ""
    observed_product_count: int = 0
    page_size: Optional[int] = None
    total_count: Optional[int] = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ApiCandidate":
        return cls(
            url=str(value.get("url", "")),
            purpose=str(value.get("purpose", "")),
            source=str(value.get("source", "agent_handoff")),
            method=str(value.get("method", "GET")),
            status_code=int(value.get("status_code", 0) or 0),
            product_list_path=str(value.get("product_list_path", "")),
            category_list_paths=tuple(value.get("category_list_paths") or ()),
            next_cursor_path=str(value.get("next_cursor_path", "")),
            next_cursor_sample=str(value.get("next_cursor_sample", "")),
            observed_product_count=int(
                value.get("observed_product_count", 0) or 0
            ),
            page_size=_optional_int(value.get("page_size")),
            total_count=_optional_int(value.get("total_count")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "purpose": self.purpose,
            "source": self.source,
            "method": self.method,
            "status_code": self.status_code,
            "product_list_path": self.product_list_path,
            "category_list_paths": list(self.category_list_paths),
            "next_cursor_path": self.next_cursor_path,
            "next_cursor_sample": self.next_cursor_sample,
            "observed_product_count": self.observed_product_count,
            "page_size": self.page_size,
            "total_count": self.total_count,
        }


@dataclass(frozen=True)
class PaginationProbe:
    method: str
    url: str
    status_code: int
    product_count: int
    different_from_first_page: bool
    accepted: bool
    reason: str

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "PaginationProbe":
        return cls(
            method=str(value.get("method", "")),
            url=str(value.get("url", "")),
            status_code=int(value.get("status_code", 0) or 0),
            product_count=int(value.get("product_count", 0) or 0),
            different_from_first_page=bool(
                value.get("different_from_first_page", False)
            ),
            accepted=bool(value.get("accepted", False)),
            reason=str(value.get("reason", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "status_code": self.status_code,
            "product_count": self.product_count,
            "different_from_first_page": self.different_from_first_page,
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass
class PaginationPlan:
    method: str = "unknown"
    parameter: str = ""
    request_url_template: str = ""
    page_size: Optional[int] = None
    product_list_path: str = ""
    next_cursor_path: str = ""
    next_cursor_sample: str = ""
    next_url: str = ""
    next_selector: str = ""
    fallback_methods: Tuple[str, ...] = (
        "page_parameter",
        "offset_parameter",
        "cursor_from_response",
        "next_control_click",
    )
    stop_conditions: Tuple[str, ...] = STOP_CONDITIONS
    evidence: List[str] = field(default_factory=list)
    probes: List[PaginationProbe] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "PaginationPlan":
        return cls(
            method=str(value.get("method", "unknown")),
            parameter=str(value.get("parameter", "")),
            request_url_template=str(value.get("request_url_template", "")),
            page_size=_optional_int(value.get("page_size")),
            product_list_path=str(value.get("product_list_path", "")),
            next_cursor_path=str(value.get("next_cursor_path", "")),
            next_cursor_sample=str(value.get("next_cursor_sample", "")),
            next_url=str(value.get("next_url", "")),
            next_selector=str(value.get("next_selector", "")),
            fallback_methods=tuple(
                value.get("fallback_methods")
                or (
                    "page_parameter",
                    "offset_parameter",
                    "cursor_from_response",
                    "next_control_click",
                )
            ),
            stop_conditions=tuple(
                value.get("stop_conditions") or STOP_CONDITIONS
            ),
            evidence=list(value.get("evidence") or []),
            probes=[
                PaginationProbe.from_dict(item)
                for item in value.get("probes", [])
                if isinstance(item, dict)
            ],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "parameter": self.parameter,
            "request_url_template": self.request_url_template,
            "page_size": self.page_size,
            "product_list_path": self.product_list_path,
            "next_cursor_path": self.next_cursor_path,
            "next_cursor_sample": self.next_cursor_sample,
            "next_url": self.next_url,
            "next_selector": self.next_selector,
            "fallback_methods": list(self.fallback_methods),
            "stop_conditions": list(self.stop_conditions),
            "evidence": list(self.evidence),
            "probes": [probe.to_dict() for probe in self.probes],
        }


@dataclass
class CrawlPlan:
    input_url: str
    start_url: str
    site_key: str
    website_type: str
    status: str
    decision: Dict[str, Any]
    homepage: Dict[str, Any]
    categories: List[CategoryCandidate]
    api_candidates: List[ApiCandidate]
    pagination: PaginationPlan
    exploration: Dict[str, Any]
    execution_policy: Dict[str, Any]
    validation_policy: Dict[str, Any]
    retry_policy: Dict[str, Any]
    output_contract: Dict[str, Any]
    workflow_steps: List[Dict[str, Any]]
    issues: List[Dict[str, Any]]
    diagnostics: List[Dict[str, Any]]

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CrawlPlan":
        return cls(
            input_url=str(value.get("input_url", value.get("start_url", ""))),
            start_url=str(value.get("start_url", "")),
            site_key=str(value.get("site_key", "")),
            website_type=str(value.get("website_type", "unknown")),
            status=str(value.get("status", "partial")),
            decision=dict(value.get("decision") or {}),
            homepage=dict(value.get("homepage") or {}),
            categories=[
                CategoryCandidate.from_dict(item)
                for item in value.get("categories", [])
                if isinstance(item, dict)
            ],
            api_candidates=[
                ApiCandidate.from_dict(item)
                for item in value.get("api_candidates", [])
                if isinstance(item, dict)
            ],
            pagination=PaginationPlan.from_dict(
                value.get("pagination")
                if isinstance(value.get("pagination"), dict)
                else {}
            ),
            exploration=dict(value.get("exploration") or {}),
            execution_policy=dict(value.get("execution_policy") or {}),
            validation_policy=dict(value.get("validation_policy") or {}),
            retry_policy=dict(value.get("retry_policy") or {}),
            output_contract=dict(value.get("output_contract") or {}),
            workflow_steps=list(value.get("workflow_steps") or []),
            issues=list(value.get("issues") or []),
            diagnostics=list(value.get("diagnostics") or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "plan_only": True,
            "exploration_performed": bool(self.diagnostics),
            "product_crawl_performed": False,
            "execute": False,
            "input_url": self.input_url,
            "start_url": self.start_url,
            "site_key": self.site_key,
            "website_type": self.website_type,
            "decision": self.decision,
            "homepage": self.homepage,
            "categories": [category.to_dict() for category in self.categories],
            "api_candidates": [candidate.to_dict() for candidate in self.api_candidates],
            "pagination": self.pagination.to_dict(),
            "exploration": self.exploration,
            "execution_policy": self.execution_policy,
            "validation_policy": self.validation_policy,
            "retry_policy": self.retry_policy,
            "output_contract": self.output_contract,
            "workflow_steps": self.workflow_steps,
            "issues": self.issues,
            "diagnostics": self.diagnostics,
        }


@dataclass
class PageAnalysis:
    result: FetchResult
    page_kind: str
    categories: List[CategoryCandidate]
    api_candidates: List[ApiCandidate]
    pagination_candidates: List[PaginationPlan]
    product_count: int
    product_signature: str
    next_control_found: bool


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ApiCandidate",
    "CategoryCandidate",
    "CrawlPlan",
    "FetchTool",
    "NetworkInspector",
    "PageAnalysis",
    "PaginationPlan",
    "PaginationProbe",
    "STOP_CONDITIONS",
]
