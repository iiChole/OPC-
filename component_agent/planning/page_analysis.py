"""Analyze HTML, embedded JSON, and API payloads during crawl planning."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..models import FetchResult
from .models import (
    ApiCandidate,
    CategoryCandidate,
    PageAnalysis,
    PaginationPlan,
)


PRODUCT_LIST_KEYS = {
    "products",
    "productlist",
    "productrecordlist",
    "items",
    "results",
}
NEXT_CURSOR_KEYS = {
    "nextcursor",
    "nextcursortoken",
    "nextpagetoken",
    "continuationtoken",
}
CATEGORY_CONTAINER_MARKERS = (
    "category",
    "categories",
    "categorytree",
    "catalogtree",
    "taxonomy",
    "navigationtree",
)
NEXT_TEXTS = {"next", "next page", "下一页", "下页", "›", "»"}
UTILITY_TEXTS = {
    "home",
    "首页",
    "login",
    "登录",
    "register",
    "注册",
    "account",
    "账户",
    "cart",
    "购物车",
    "contact",
    "联系我们",
    "about",
    "关于我们",
}


@dataclass
class _Anchor:
    tag: str
    href: str
    text: str
    in_navigation: bool
    rel: str
    css_selector: str


@dataclass
class _DocumentSignals:
    anchors: List[_Anchor] = field(default_factory=list)
    json_scripts: List[str] = field(default_factory=list)
    visible_text_chars: int = 0
    script_count: int = 0
    product_marker_count: int = 0


class _ExplorationHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.signals = _DocumentSignals()
        self._stack: List[Tuple[str, bool]] = []
        self._navigation_depth = 0
        self._anchor: Optional[Dict[str, Any]] = None
        self._script: Optional[Dict[str, Any]] = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        values = {key.lower(): str(value or "") for key, value in attrs}
        role = values.get("role", "").lower()
        starts_navigation = tag in {"header", "nav"} or role == "navigation"
        self._stack.append((tag, starts_navigation))
        if starts_navigation:
            self._navigation_depth += 1
        if tag in {"style", "noscript"}:
            self._ignored_depth += 1

        marker_text = " ".join((
            values.get("class", ""),
            values.get("itemtype", ""),
            values.get("data-product-id", ""),
        ))
        if (
            values.get("data-product-id")
            or values.get("data-sku")
            or re.search(r"\bproduct(?:-|_)?(?:item|card|tile|record)?\b", marker_text, re.I)
            or values.get("itemtype", "").lower().endswith("/product")
        ):
            self.signals.product_marker_count += 1

        if tag in {"a", "button"}:
            self._anchor = {
                "tag": tag,
                "href": values.get("href", ""),
                "text": [],
                "in_navigation": self._navigation_depth > 0,
                "rel": values.get("rel", ""),
                "css_selector": _css_selector(tag, values),
            }
        if tag == "script":
            self.signals.script_count += 1
            script_type = values.get("type", "").lower()
            script_id = values.get("id", "")
            capture = "json" in script_type or script_id in {
                "__NEXT_DATA__",
                "__NUXT_DATA__",
                "__INITIAL_STATE__",
            }
            self._script = {"capture": capture, "text": []}

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"a", "button"} and self._anchor is not None:
            self.signals.anchors.append(_Anchor(
                tag=self._anchor["tag"],
                href=self._anchor["href"],
                text=" ".join(self._anchor["text"]).strip(),
                in_navigation=self._anchor["in_navigation"],
                rel=self._anchor["rel"],
                css_selector=self._anchor["css_selector"],
            ))
            self._anchor = None
        if tag == "script" and self._script is not None:
            if self._script["capture"]:
                text = "".join(self._script["text"]).strip()
                if text:
                    self.signals.json_scripts.append(text)
            self._script = None
        if tag in {"style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

        for index in range(len(self._stack) - 1, -1, -1):
            stack_tag, _ = self._stack[index]
            if stack_tag != tag:
                continue
            removed = self._stack[index:]
            del self._stack[index:]
            self._navigation_depth -= sum(
                1 for _, starts_navigation in removed if starts_navigation
            )
            self._navigation_depth = max(0, self._navigation_depth)
            break

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            stripped = data.strip()
            if stripped:
                self._anchor["text"].append(stripped)
        if self._script is not None:
            self._script["text"].append(data)
        elif not self._ignored_depth:
            self.signals.visible_text_chars += len(data.strip())


def analyze_page(result: FetchResult, source: str) -> PageAnalysis:
    """Extract planning signals from one HTML or JSON response."""
    payload = load_json(result.text)
    categories: List[CategoryCandidate] = []
    api_candidates: List[ApiCandidate] = []
    paginations: List[PaginationPlan] = []
    product_count = 0
    product_signature = ""

    if payload is not None:
        categories.extend(categories_from_payload(
            payload,
            result.url,
            source=f"{source}_json",
        ))
        candidate = api_candidate_from_payload(
            payload,
            result.url,
            source=f"{source}_json",
            status_code=result.status_code,
        )
        if candidate:
            api_candidates.append(candidate)
            pagination = pagination_from_api(candidate)
            if pagination:
                paginations.append(pagination)
            product_count = candidate.observed_product_count
            product_signature = payload_product_signature(payload)
        return PageAnalysis(
            result=result,
            page_kind="json_api",
            categories=categories,
            api_candidates=api_candidates,
            pagination_candidates=paginations,
            product_count=product_count,
            product_signature=product_signature,
            next_control_found=False,
        )

    parser = _ExplorationHTMLParser()
    try:
        parser.feed(result.text or "")
    except Exception:
        pass
    signals = parser.signals
    categories.extend(_categories_from_navigation(signals.anchors, result.url))

    for raw_json in signals.json_scripts:
        embedded = load_json(raw_json)
        if embedded is None:
            continue
        categories.extend(categories_from_payload(
            embedded,
            result.url,
            source="embedded_json",
        ))
        candidate = api_candidate_from_payload(
            embedded,
            result.url,
            source="embedded_json",
            status_code=result.status_code,
        )
        if candidate:
            api_candidates.append(candidate)
            pagination = pagination_from_api(candidate)
            if pagination:
                paginations.append(pagination)
            if candidate.observed_product_count > product_count:
                product_count = candidate.observed_product_count
                product_signature = payload_product_signature(embedded)

    html_pagination = _pagination_from_anchors(signals.anchors, result.url)
    next_control = html_pagination is not None
    if html_pagination is not None:
        paginations.append(html_pagination)

    product_links = [
        urljoin(result.url, anchor.href)
        for anchor in signals.anchors
        if _looks_like_product_url(anchor.href)
    ]
    html_count = max(signals.product_marker_count, len(set(product_links)))
    if html_count > product_count:
        product_count = html_count
        product_signature = hash_values(product_links or [result.text[:5000]])

    return PageAnalysis(
        result=result,
        page_kind=_classify_html(result.text, signals),
        categories=deduplicate_categories(categories),
        api_candidates=deduplicate_apis(api_candidates),
        pagination_candidates=paginations,
        product_count=product_count,
        product_signature=product_signature,
        next_control_found=next_control,
    )


def normalize_start_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        raise ValueError("网站 URL 不能为空")
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持有效的 HTTP/HTTPS 网站 URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        parsed.query,
        "",
    ))


def same_site(first: str, second: str) -> bool:
    first_host = (urlsplit(first).hostname or "").lower()
    second_host = (urlsplit(second).hostname or "").lower()
    return bool(first_host and second_host) and (
        first_host == second_host
        or first_host.endswith(f".{second_host}")
        or second_host.endswith(f".{first_host}")
    )


def load_json(text: str) -> Any:
    stripped = str(text or "").strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None


def categories_from_payload(
    payload: Any,
    source_url: str,
    source: str,
) -> List[CategoryCandidate]:
    categories: List[CategoryCandidate] = []
    for path, value in _walk_json(payload):
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            normalized = _normalize_key(key)
            if not any(marker in normalized for marker in CATEGORY_CONTAINER_MARKERS):
                continue
            categories.extend(_collect_category_nodes(
                child,
                source_url=source_url,
                source=source,
                evidence_path=f"{path}.{key}",
            ))
    return deduplicate_categories(categories)


def api_candidate_from_payload(
    payload: Any,
    url: str,
    source: str,
    method: str = "GET",
    status_code: int = 0,
) -> Optional[ApiCandidate]:
    product_lists = _find_product_lists(payload)
    category_paths = tuple(sorted({
        path
        for path, value in _walk_json(payload)
        if isinstance(value, dict)
        for key, child in value.items()
        if isinstance(child, (list, dict))
        and any(marker in _normalize_key(key) for marker in CATEGORY_CONTAINER_MARKERS)
    }))
    if not product_lists and not category_paths:
        return None
    product_path = product_lists[0][0] if product_lists else ""
    product_count = len(product_lists[0][1]) if product_lists else 0
    cursor_path, cursor_value = _find_first_key(payload, NEXT_CURSOR_KEYS)
    page_size = _find_first_integer(payload, {"pagesize", "limit", "perpage", "size"})
    total_count = _find_first_integer(
        payload,
        {"total", "totalcount", "totalelements", "totalrecords", "recordcount"},
    )
    return ApiCandidate(
        url=url,
        purpose="products" if product_lists else "categories",
        source=source,
        method=method,
        status_code=status_code,
        product_list_path=product_path,
        category_list_paths=category_paths,
        next_cursor_path=cursor_path,
        next_cursor_sample=str(cursor_value or ""),
        observed_product_count=product_count,
        page_size=page_size or (product_count or None),
        total_count=total_count,
    )


def pagination_from_api(candidate: ApiCandidate) -> Optional[PaginationPlan]:
    if candidate.next_cursor_path:
        parameter = _cursor_parameter_from_url(candidate.url) or "cursor"
        return PaginationPlan(
            method="cursor",
            parameter=parameter,
            request_url_template=set_query_parameter(candidate.url, parameter, "{cursor}"),
            page_size=candidate.page_size,
            product_list_path=candidate.product_list_path,
            next_cursor_path=candidate.next_cursor_path,
            next_cursor_sample=candidate.next_cursor_sample,
            evidence=[
                f"API 响应包含下一游标 {candidate.next_cursor_path}",
                f"产品数组路径为 {candidate.product_list_path}",
            ],
        )
    query = dict(parse_qsl(urlsplit(candidate.url).query, keep_blank_values=True))
    for parameter in ("page", "pageNo", "pageNum", "offset"):
        if parameter not in query:
            continue
        method = "offset_parameter" if parameter == "offset" else "page_parameter"
        return PaginationPlan(
            method=method,
            parameter=parameter,
            request_url_template=set_query_parameter(
                candidate.url,
                parameter,
                f"{{{parameter}}}",
            ),
            page_size=candidate.page_size,
            product_list_path=candidate.product_list_path,
            evidence=[f"产品 API URL 使用 {parameter} 参数"],
        )
    return None


def choose_pagination(candidates: Sequence[PaginationPlan]) -> PaginationPlan:
    if not candidates:
        return PaginationPlan()
    priority = {
        "cursor": 100,
        "page_parameter": 90,
        "offset_parameter": 90,
        "next_link": 80,
        "next_click": 70,
    }
    return max(candidates, key=lambda item: priority.get(item.method, 0))


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
    )).replace(urlencode({parameter: value}), f"{parameter}={value}")


def payload_product_signature(payload: Any) -> str:
    lists = _find_product_lists(payload)
    if not lists:
        return ""
    values = []
    for item in lists[0][1][:100]:
        if not isinstance(item, dict):
            continue
        values.append(str(
            _first_scalar(
                item,
                "productId",
                "id",
                "sku",
                "productCode",
                "partNumber",
                "mpn",
                "url",
            )
            or json.dumps(item, ensure_ascii=False, sort_keys=True)[:500]
        ))
    return hash_values(values)


def hash_values(values: Sequence[str]) -> str:
    normalized = "\n".join(sorted({str(value) for value in values if value}))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def deduplicate_categories(
    categories: Iterable[CategoryCandidate],
) -> List[CategoryCandidate]:
    unique: List[CategoryCandidate] = []
    seen = set()
    for category in categories:
        key = (
            canonical_url(category.url) if category.url else "",
            category.identifier.strip().lower(),
            category.name.strip().lower(),
        )
        if key in seen or not any(key):
            continue
        seen.add(key)
        unique.append(category)
    return unique


def deduplicate_apis(candidates: Iterable[ApiCandidate]) -> List[ApiCandidate]:
    unique: List[ApiCandidate] = []
    seen = set()
    for candidate in candidates:
        key = (
            canonical_url(candidate.url),
            candidate.purpose,
            candidate.product_list_path,
            candidate.next_cursor_path,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _css_selector(tag: str, attrs: Dict[str, str]) -> str:
    identifier = re.sub(r"[^a-zA-Z0-9_-]", "", attrs.get("id", ""))
    if identifier:
        return f"#{identifier}"
    classes = [
        re.sub(r"[^a-zA-Z0-9_-]", "", value)
        for value in attrs.get("class", "").split()
    ]
    classes = [value for value in classes if value]
    if classes:
        return f"{tag}.{classes[0]}"
    if "next" in attrs.get("rel", "").lower():
        return f"{tag}[rel='next']"
    return ""


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _walk_json(value: Any, path: str = "$") -> Iterable[Tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_json(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{path}[{index}]")


def _categories_from_navigation(
    anchors: Sequence[_Anchor],
    source_url: str,
) -> List[CategoryCandidate]:
    categories: List[CategoryCandidate] = []
    for anchor in anchors:
        if not anchor.in_navigation or not anchor.href:
            continue
        lowered_text = " ".join(anchor.text.lower().split())
        if lowered_text in UTILITY_TEXTS:
            continue
        absolute = urljoin(source_url, anchor.href)
        if not same_site(source_url, absolute):
            continue
        if urlsplit(absolute).scheme not in {"http", "https"}:
            continue
        lowered_href = anchor.href.lower()
        score = 1
        if any(marker in lowered_href for marker in (
            "/category",
            "/categories",
            "/catalog",
            "/products",
        )):
            score += 2
        if any(marker in lowered_text for marker in (
            "product",
            "category",
            "产品",
            "分类",
            "元器件",
        )):
            score += 1
        if canonical_url(absolute) == canonical_url(source_url):
            continue
        if not anchor.text and score < 3:
            continue
        categories.append(CategoryCandidate(
            name=anchor.text or urlsplit(absolute).path.rstrip("/").split("/")[-1],
            url=absolute,
            source="html_navigation",
            confidence=min(0.95, 0.45 + score * 0.15),
            evidence="链接位于 header/nav/role=navigation 范围内",
        ))
    return categories


def _collect_category_nodes(
    value: Any,
    source_url: str,
    source: str,
    evidence_path: str,
) -> List[CategoryCandidate]:
    categories: List[CategoryCandidate] = []
    if isinstance(value, str):
        if value.strip():
            categories.append(CategoryCandidate(
                name=value.strip(),
                source=source,
                confidence=0.6,
                evidence=evidence_path,
            ))
        return categories
    if isinstance(value, list):
        for index, child in enumerate(value):
            categories.extend(_collect_category_nodes(
                child,
                source_url,
                source,
                f"{evidence_path}[{index}]",
            ))
        return categories
    if not isinstance(value, dict):
        return categories

    name = _first_scalar(value, "name", "title", "label", "categoryName", "catalogName")
    identifier = _first_scalar(value, "id", "categoryId", "catalogId", "code", "slug")
    link = _first_scalar(value, "url", "href", "link", "categoryUrl", "path")
    children_present = any(
        _normalize_key(key) in {"children", "childcategories", "subcategories", "nodes"}
        for key in value
    )
    if name and (identifier or link or children_present):
        categories.append(CategoryCandidate(
            name=str(name),
            url=urljoin(source_url, str(link)) if link else "",
            identifier=str(identifier or ""),
            source=source,
            confidence=0.9 if link or identifier else 0.7,
            evidence=evidence_path,
        ))
    for key, child in value.items():
        if _normalize_key(key) in {
            "children",
            "childcategories",
            "subcategories",
            "nodes",
            "categories",
            "categorylist",
        }:
            categories.extend(_collect_category_nodes(
                child,
                source_url,
                source,
                f"{evidence_path}.{key}",
            ))
    return categories


def _find_product_lists(payload: Any) -> List[Tuple[str, List[Any]]]:
    found: List[Tuple[str, List[Any]]] = []
    for path, value in _walk_json(payload):
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            if not isinstance(child, list) or not child:
                continue
            normalized = _normalize_key(key)
            if normalized not in PRODUCT_LIST_KEYS:
                continue
            mappings = [item for item in child if isinstance(item, dict)]
            if not mappings:
                continue
            if normalized in {"items", "results"} and not any(
                _looks_like_product_mapping(item) for item in mappings[:5]
            ):
                continue
            found.append((f"{path}.{key}", child))
    return found


def _looks_like_product_mapping(mapping: Dict[str, Any]) -> bool:
    keys = {_normalize_key(key) for key in mapping}
    return bool(keys & {
        "productid",
        "productmodel",
        "partnumber",
        "genericpartnumber",
        "mpn",
        "sku",
        "productcode",
        "manufacturerpartnumber",
    })


def _find_first_key(payload: Any, keys: set[str]) -> Tuple[str, Any]:
    for path, value in _walk_json(payload):
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            if _normalize_key(key) in keys and child not in (None, "", [], {}):
                return f"{path}.{key}", child
    return "", None


def _find_first_integer(payload: Any, keys: set[str]) -> Optional[int]:
    _, value = _find_first_key(payload, keys)
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _first_scalar(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}) and not isinstance(value, (dict, list)):
            return value
    return ""


def _pagination_from_anchors(
    anchors: Sequence[_Anchor],
    source_url: str,
) -> Optional[PaginationPlan]:
    for anchor in anchors:
        text = " ".join(anchor.text.lower().split())
        rel_next = "next" in anchor.rel.lower().split()
        text_next = text in NEXT_TEXTS
        if not rel_next and not text_next:
            continue
        if anchor.href:
            return PaginationPlan(
                method="next_link",
                next_url=urljoin(source_url, anchor.href),
                next_selector=anchor.css_selector or "a[rel='next']",
                evidence=[f"HTML 中发现下一页链接，文本为 {anchor.text!r}"],
            )
        return PaginationPlan(
            method="next_click",
            next_selector=anchor.css_selector or f"text={anchor.text}",
            evidence=[f"HTML 中发现需要点击的下一页控件，文本为 {anchor.text!r}"],
        )
    return None


def _cursor_parameter_from_url(url: str) -> str:
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    for key in query:
        if "cursor" in key.lower() or "token" in key.lower():
            return key
    return ""


def _classify_html(text: str, signals: _DocumentSignals) -> str:
    lowered = str(text or "").lower()
    if "var _xvasu" in lowered and "var _xvpts" in lowered:
        return "anti_bot_challenge"
    if 'id="__next_data__"' in lowered:
        return "next_ssr"
    dynamic_markers = (
        'id="root"',
        'id="app"',
        "__nuxt",
        "data-reactroot",
        "ng-version",
    )
    if signals.script_count >= 5 and (
        signals.visible_text_chars < 300
        or any(marker in lowered for marker in dynamic_markers)
    ):
        return "javascript_rendered"
    return "static_html"


def _looks_like_product_url(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in (
        "/product/",
        "/products/",
        "/detail/",
        "/item/",
        "item.szlcsc.com/",
    ))


__all__ = [
    "analyze_page",
    "api_candidate_from_payload",
    "canonical_url",
    "categories_from_payload",
    "choose_pagination",
    "deduplicate_apis",
    "deduplicate_categories",
    "hash_values",
    "load_json",
    "normalize_start_url",
    "pagination_from_api",
    "payload_product_signature",
    "same_site",
    "set_query_parameter",
]
