"""Pagination strategy selection and resumable state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..planning.models import CrawlPlan, PaginationPlan
from .models import CatalogPage, CategoryTask, PaginationState


MODE_ALIASES = {
    "page": "page",
    "page_parameter": "page",
    "offset": "offset",
    "offset_parameter": "offset",
    "cursor": "cursor",
    "cursor_from_response": "cursor",
    "next": "next_link",
    "next_link": "next_link",
    "next_click": "next_click",
    "next_control_click": "next_click",
    "auto": "auto",
    "unknown": "auto",
}
CATEGORY_QUERY_KEYS = {
    "category",
    "categoryid",
    "category_id",
    "cateid",
    "cate_id",
    "cat",
    "catalogid",
}


@dataclass(frozen=True)
class PageTransition:
    complete: bool
    anomaly: bool = False
    reason: str = ""


def select_traversal_mode(
    requested: str,
    root_category_count: int,
    prefer_parallel: bool = False,
    bfs_threshold: int = 20,
) -> str:
    normalized = str(requested or "auto").strip().lower()
    if normalized in {"dfs", "bfs"}:
        return normalized
    if prefer_parallel or root_category_count >= max(1, bfs_threshold):
        return "bfs"
    return "dfs"


def select_pagination_mode(plan: PaginationPlan) -> str:
    return MODE_ALIASES.get(str(plan.method or "auto").lower(), "auto")


def resolve_base_url(plan: CrawlPlan, category: CategoryTask) -> str:
    product_apis = [
        candidate
        for candidate in plan.api_candidates
        if candidate.purpose == "products"
    ]
    use_api = bool(plan.pagination.product_list_path and product_apis)
    if use_api:
        return bind_category(product_apis[0].url, category)
    if category.url:
        return category.url
    if product_apis:
        return bind_category(product_apis[0].url, category)
    return plan.start_url


def initial_pagination_state(
    plan: CrawlPlan,
    category: CategoryTask,
) -> PaginationState:
    mode = select_pagination_mode(plan.pagination)
    return PaginationState(
        mode=mode,
        base_url=resolve_base_url(plan, category),
        next_url=plan.pagination.next_url if mode == "next_link" else "",
        cursor=(
            ""
            if mode == "cursor"
            else plan.pagination.next_cursor_sample
        ),
    )


def request_url(state: PaginationState, plan: PaginationPlan) -> str:
    if state.mode == "next_link" and state.pages_seen > 0:
        return state.next_url or state.base_url
    if state.mode == "next_click":
        return state.base_url
    if state.mode == "cursor":
        if not state.cursor:
            return state.base_url
        parameter = plan.parameter or "cursor"
        return _template_or_query(
            plan.request_url_template,
            state.base_url,
            parameter,
            state.cursor,
        )
    if state.mode == "offset":
        if state.pages_seen == 0 and not _query_has_parameter(
            state.base_url,
            plan.parameter or "offset",
        ):
            return state.base_url
        parameter = plan.parameter or "offset"
        return _template_or_query(
            plan.request_url_template,
            state.base_url,
            parameter,
            str(state.offset),
        )
    if state.mode in {"page", "auto"}:
        if state.pages_seen == 0 and not _query_has_parameter(
            state.base_url,
            plan.parameter or "page",
        ):
            return state.base_url
        parameter = plan.parameter or "page"
        return _template_or_query(
            plan.request_url_template if state.mode == "page" else "",
            state.base_url,
            parameter,
            str(state.page_number),
        )
    return state.base_url


def advance_pagination(
    state: PaginationState,
    page: CatalogPage,
    plan: PaginationPlan,
) -> PageTransition:
    if page.signature and page.signature in state.page_signatures:
        state.stop_reason = "repeated_page_signature"
        return PageTransition(
            complete=True,
            anomaly=True,
            reason=state.stop_reason,
        )

    if page.signature:
        state.page_signatures.append(page.signature)
    state.pages_seen += 1
    state.product_count += page.raw_product_count
    if page.total_count is not None:
        state.reported_total = page.total_count

    if page.raw_product_count == 0:
        state.complete = True
        state.stop_reason = "empty_product_list"
        anomaly = bool(
            state.reported_total is not None
            and state.product_count < state.reported_total
        )
        return PageTransition(True, anomaly, state.stop_reason)

    page_size = plan.page_size or page.page_size
    if page_size and page.raw_product_count < page_size:
        state.complete = True
        state.stop_reason = "returned_count_less_than_page_size"
        anomaly = bool(
            state.reported_total is not None
            and state.product_count < state.reported_total
        )
        return PageTransition(True, anomaly, state.stop_reason)

    if state.mode == "auto":
        if page.next_cursor:
            state.mode = "cursor"
        elif page.next_url:
            state.mode = "next_link"
        else:
            state.mode = "page"

    if state.mode == "cursor":
        if not page.next_cursor:
            state.complete = True
            state.stop_reason = "missing_next_cursor"
            anomaly = bool(
                state.reported_total is not None
                and state.product_count < state.reported_total
            )
            return PageTransition(True, anomaly, state.stop_reason)
        if page.next_cursor == state.cursor:
            state.stop_reason = "cursor_not_advancing"
            return PageTransition(True, True, state.stop_reason)
        state.cursor = page.next_cursor
        return PageTransition(False)

    if state.mode == "next_link":
        if not page.next_url:
            state.complete = True
            state.stop_reason = "next_link_or_control_absent"
            anomaly = bool(
                state.reported_total is not None
                and state.product_count < state.reported_total
            )
            return PageTransition(True, anomaly, state.stop_reason)
        if page.next_url == state.next_url and state.pages_seen > 1:
            state.stop_reason = "next_url_not_advancing"
            return PageTransition(True, True, state.stop_reason)
        state.next_url = page.next_url
        return PageTransition(False)

    if state.mode == "offset":
        state.offset += page_size or page.raw_product_count
        return PageTransition(False)

    if state.mode == "page":
        state.page_number += 1
        return PageTransition(False)

    return PageTransition(False)


def count_anomaly_reason(state: PaginationState) -> Optional[str]:
    if (
        state.reported_total is not None
        and state.product_count != state.reported_total
    ):
        return (
            f"category_count_mismatch: reported={state.reported_total}, "
            f"enumerated={state.product_count}"
        )
    return None


def bind_category(url: str, category: CategoryTask) -> str:
    if not category.identifier:
        return url
    parsed = urlsplit(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    replaced = False
    bound = []
    for key, value in query_pairs:
        normalized = key.lower().replace("-", "_")
        if normalized in CATEGORY_QUERY_KEYS:
            bound.append((key, category.identifier))
            replaced = True
        else:
            bound.append((key, value))
    if not replaced:
        return url
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(bound),
        parsed.fragment,
    ))


def set_query_parameter(url: str, parameter: str, value: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[parameter] = value
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(query),
        parsed.fragment,
    ))


def _template_or_query(
    template: str,
    base_url: str,
    parameter: str,
    value: str,
) -> str:
    if template and not _template_matches_base(template, base_url):
        template = ""
    placeholder = f"{{{parameter}}}"
    if template and placeholder in template:
        template_base = _copy_category_binding(template, base_url)
        return template_base.replace(placeholder, value)
    generic_placeholders = ("{page}", "{offset}", "{cursor}")
    if template and any(item in template for item in generic_placeholders):
        template_base = _copy_category_binding(template, base_url)
        for item in generic_placeholders:
            if item in template_base:
                return template_base.replace(item, value)
    return set_query_parameter(base_url, parameter, value)


def _template_matches_base(template: str, base_url: str) -> bool:
    template_parts = urlsplit(template)
    base_parts = urlsplit(base_url)
    return (
        template_parts.netloc.lower() == base_parts.netloc.lower()
        and template_parts.path == base_parts.path
    )


def _query_has_parameter(url: str, parameter: str) -> bool:
    return parameter in dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _copy_category_binding(template: str, base_url: str) -> str:
    template_parts = urlsplit(template)
    base_query = dict(parse_qsl(urlsplit(base_url).query, keep_blank_values=True))
    template_query = dict(parse_qsl(template_parts.query, keep_blank_values=True))
    for key, value in base_query.items():
        normalized = key.lower().replace("-", "_")
        if normalized in CATEGORY_QUERY_KEYS and key in template_query:
            template_query[key] = value
    return urlunsplit((
        template_parts.scheme,
        template_parts.netloc,
        template_parts.path,
        urlencode(template_query),
        template_parts.fragment,
    ))


__all__ = [
    "PageTransition",
    "advance_pagination",
    "bind_category",
    "count_anomaly_reason",
    "initial_pagination_state",
    "request_url",
    "resolve_base_url",
    "select_pagination_mode",
    "select_traversal_mode",
    "set_query_parameter",
]
