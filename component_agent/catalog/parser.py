"""Extract ProductSeed records and pagination metadata from catalog pages."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

from ..models import FetchResult
from ..planning.page_analysis import analyze_page, load_json
from .models import CatalogPage, CategoryTask, ProductSeed


PRODUCT_LIST_KEYS = {
    "products",
    "productlist",
    "productrecordlist",
    "productrecords",
    "items",
    "results",
    "records",
}
NEXT_CURSOR_KEYS = {
    "nextcursor",
    "nextcursortoken",
    "nextpagetoken",
    "continuationtoken",
}
NEXT_URL_KEYS = {"nexturl", "nextpageurl", "nextlink"}
PAGE_SIZE_KEYS = {"pagesize", "perpage", "limit", "size"}
TOTAL_COUNT_KEYS = {
    "total",
    "totalcount",
    "totalelements",
    "totalrecords",
    "recordcount",
}
NEXT_TEXTS = {"next", "next page", "下一页", "下页", "›", "»"}


class CatalogParser:
    def parse(
        self,
        result: FetchResult,
        category: CategoryTask,
        site_key: str,
        product_list_path: str = "",
        next_cursor_path: str = "",
    ) -> CatalogPage:
        payload = load_json(result.text)
        if payload is not None:
            page = self._parse_payload(
                payload,
                result.url,
                category,
                site_key,
                product_list_path,
                next_cursor_path,
            )
            analysis = analyze_page(result, source="catalog_json")
            page.child_categories = analysis.categories
            page.source_kind = "json"
            return page

        html_parser = _CatalogHTMLParser()
        try:
            html_parser.feed(result.text or "")
        except Exception:
            pass

        payload_pages = []
        for raw_json in html_parser.json_scripts:
            embedded = load_json(raw_json)
            if embedded is None:
                continue
            payload_pages.append(self._parse_payload(
                embedded,
                result.url,
                category,
                site_key,
                product_list_path,
                next_cursor_path,
            ))
        embedded_page = max(
            payload_pages,
            key=lambda item: item.raw_product_count,
            default=None,
        )
        html_products = [
            self._seed_from_mapping(
                item,
                result.url,
                category,
                site_key,
            )
            for item in html_parser.products
        ]
        html_products = [seed for seed in html_products if seed is not None]

        products = (
            embedded_page.products
            if embedded_page and embedded_page.raw_product_count >= len(html_products)
            else html_products
        )
        raw_count = (
            embedded_page.raw_product_count
            if embedded_page and embedded_page.raw_product_count >= len(html_parser.products)
            else len(html_parser.products)
        )
        next_cursor = embedded_page.next_cursor if embedded_page else ""
        next_url = (
            html_parser.next_url
            or (embedded_page.next_url if embedded_page else "")
        )
        page_size = embedded_page.page_size if embedded_page else None
        total_count = embedded_page.total_count if embedded_page else None
        analysis = analyze_page(result, source="catalog_html")
        return CatalogPage(
            products=products,
            raw_product_count=raw_count,
            child_categories=analysis.categories,
            next_cursor=next_cursor,
            next_url=urljoin(result.url, next_url) if next_url else "",
            has_next_control=html_parser.has_next_control,
            page_size=page_size,
            total_count=total_count,
            signature=_product_signature(products, html_parser.products),
            source_kind="embedded_json" if embedded_page else "html",
        )

    def _parse_payload(
        self,
        payload: Any,
        source_url: str,
        category: CategoryTask,
        site_key: str,
        product_list_path: str,
        next_cursor_path: str,
    ) -> CatalogPage:
        raw_products = _resolve_product_list(payload, product_list_path)
        products = [
            self._seed_from_mapping(item, source_url, category, site_key)
            for item in raw_products
            if isinstance(item, dict)
        ]
        products = [seed for seed in products if seed is not None]

        next_cursor = _scalar_at_path(payload, next_cursor_path)
        if not next_cursor:
            _, next_cursor = _find_first_value(payload, NEXT_CURSOR_KEYS)
        _, next_url = _find_first_value(payload, NEXT_URL_KEYS)
        _, page_size = _find_first_integer(payload, PAGE_SIZE_KEYS)
        _, total_count = _find_first_integer(payload, TOTAL_COUNT_KEYS)
        return CatalogPage(
            products=products,
            raw_product_count=len(raw_products),
            next_cursor=str(next_cursor or ""),
            next_url=urljoin(source_url, str(next_url)) if next_url else "",
            has_next_control=bool(next_cursor or next_url),
            page_size=page_size,
            total_count=total_count,
            signature=_product_signature(products, raw_products),
            source_kind="json",
        )

    @staticmethod
    def _seed_from_mapping(
        item: Dict[str, Any],
        source_url: str,
        category: CategoryTask,
        site_key: str,
    ) -> Optional[ProductSeed]:
        sku = _text_value(item, (
            "sku",
            "sellerSku",
            "stockCode",
            "catalogNumber",
            "digikeyPartNumber",
            "mouserPartNumber",
        ))
        product_id = _text_value(item, (
            "productId",
            "product_id",
            "id",
            "itemId",
            "goodsId",
            "recordId",
        ))
        detail_url = _text_value(item, (
            "detailUrl",
            "detail_url",
            "productUrl",
            "product_url",
            "url",
            "href",
            "link",
        ))
        if detail_url:
            detail_url = urljoin(source_url, detail_url)

        seed = ProductSeed(
            site_key=site_key,
            category_id=(
                _text_value(item, (
                    "cateId",
                    "cate_id",
                    "categoryId",
                    "category_id",
                ))
                or category.identifier
            ),
            category_name=category.name,
            sku=sku,
            product_id=product_id,
            title=_text_value(item, (
                "title",
                "productName",
                "name",
                "partNumber",
                "mpn",
                "manufacturerPartNumber",
                "model",
            )),
            stock=_value(item, (
                "stock",
                "stockQuantity",
                "inventory",
                "availableQuantity",
                "quantityAvailable",
            )),
            price=_value(item, (
                "price",
                "prices",
                "priceList",
                "pricing",
                "unitPrice",
            )),
            manufacturer=_text_value(item, (
                "manufacturer",
                "manufacturerName",
                "brand",
                "brandName",
                "mfr",
            )),
            moq=_value(item, (
                "moq",
                "minimumOrderQuantity",
                "minOrderQty",
            )),
            package=_text_value(item, (
                "package",
                "packageType",
                "packaging",
                "casePackage",
            )),
            image_url=_absolute_url(source_url, _text_value(item, (
                "imageUrl",
                "image_url",
                "image",
                "thumbnail",
                "primaryImage",
            ))),
            detail_url=detail_url,
            description=_text_value(item, (
                "description",
                "shortDescription",
                "summary",
            )),
            datasheet_url=_absolute_url(source_url, _text_value(item, (
                "datasheetUrl",
                "datasheet_url",
                "datasheet",
            ))),
            source_url=source_url,
            extra={"catalog_raw": item},
        )
        seed.assign_dedup_identity()
        return seed


class _CatalogHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: List[Dict[str, Any]] = []
        self.json_scripts: List[str] = []
        self.next_url = ""
        self.has_next_control = False
        self._depth = 0
        self._active_product: Optional[Dict[str, Any]] = None
        self._script: Optional[Dict[str, Any]] = None
        self._control: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._depth += 1
        tag = tag.lower()
        values = {key.lower(): str(value or "") for key, value in attrs}
        classes = values.get("class", "")
        marker = " ".join((
            classes,
            values.get("itemtype", ""),
            values.get("data-product-id", ""),
            values.get("data-sku", ""),
        ))
        starts_product = bool(
            values.get("data-product-id")
            or values.get("data-sku")
            or values.get("itemtype", "").lower().endswith("/product")
            or re.search(
                r"\bproduct(?:-|_)?(?:item|card|tile|record)?\b",
                marker,
                re.I,
            )
        )
        if starts_product and self._active_product is None:
            self._active_product = {
                "depth": self._depth,
                "sku": values.get("data-sku", ""),
                "productId": values.get("data-product-id", ""),
                "title": values.get("title", ""),
                "detailUrl": values.get("href", ""),
                "imageUrl": values.get("src", ""),
                "text": [],
            }
        elif self._active_product is not None:
            if tag == "a" and values.get("href") and not self._active_product["detailUrl"]:
                self._active_product["detailUrl"] = values["href"]
            if tag == "img" and values.get("src") and not self._active_product["imageUrl"]:
                self._active_product["imageUrl"] = values["src"]

        if tag in {"a", "button"}:
            self._control = {
                "tag": tag,
                "href": values.get("href", ""),
                "rel": values.get("rel", ""),
                "class": classes,
                "text": [],
            }
        if tag == "script":
            script_type = values.get("type", "").lower()
            script_id = values.get("id", "")
            self._script = {
                "capture": (
                    "json" in script_type
                    or script_id in {
                        "__NEXT_DATA__",
                        "__NUXT_DATA__",
                        "__INITIAL_STATE__",
                    }
                ),
                "text": [],
            }

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"a", "button"} and self._control is not None:
            text = " ".join(self._control["text"]).strip()
            normalized = " ".join(text.lower().split())
            classes = self._control["class"].lower()
            is_next = (
                "next" in self._control["rel"].lower().split()
                or normalized in NEXT_TEXTS
                or re.search(r"\bnext\b", classes)
            )
            if is_next:
                self.has_next_control = True
                if self._control["href"]:
                    self.next_url = self._control["href"]
            self._control = None
        if tag == "script" and self._script is not None:
            if self._script["capture"]:
                text = "".join(self._script["text"]).strip()
                if text:
                    self.json_scripts.append(text)
            self._script = None

        if (
            self._active_product is not None
            and self._active_product["depth"] == self._depth
        ):
            product = dict(self._active_product)
            product.pop("depth", None)
            text = " ".join(product.pop("text", [])).strip()
            if not product.get("title"):
                product["title"] = text
            self.products.append(product)
            self._active_product = None
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped and self._active_product is not None:
            self._active_product["text"].append(stripped)
        if stripped and self._control is not None:
            self._control["text"].append(stripped)
        if self._script is not None:
            self._script["text"].append(data)


def _resolve_product_list(payload: Any, explicit_path: str) -> List[Any]:
    if explicit_path:
        resolved = _resolve_path(payload, explicit_path)
        if isinstance(resolved, list):
            return resolved

    candidates: List[Tuple[int, List[Any]]] = []
    for _, value in _walk_json(payload):
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            if not isinstance(child, list):
                continue
            normalized = _normalize_key(key)
            mappings = [item for item in child if isinstance(item, dict)]
            if normalized in PRODUCT_LIST_KEYS:
                score = 100 + len(mappings)
                if mappings and not any(
                    _looks_like_product(item)
                    for item in mappings[:5]
                ):
                    score -= 80
                candidates.append((score, child))
            elif normalized == "data" and mappings and any(
                _looks_like_product(item)
                for item in mappings[:5]
            ):
                candidates.append((50 + len(mappings), child))
    if not candidates:
        return []
    return max(candidates, key=lambda item: item[0])[1]


def _looks_like_product(item: Dict[str, Any]) -> bool:
    keys = {_normalize_key(key) for key in item}
    return bool(keys & {
        "sku",
        "productid",
        "partnumber",
        "mpn",
        "manufacturerpartnumber",
        "producturl",
        "detailurl",
        "stockcode",
    })


def _resolve_path(payload: Any, path: str) -> Any:
    normalized = str(path or "").strip()
    if not normalized:
        return None
    if normalized.startswith("$"):
        normalized = normalized[1:]
    tokens = re.findall(r"\.([^\.\[]+)|\[(\d+)\]", normalized)
    current = payload
    for key_token, index_token in tokens:
        if key_token:
            if not isinstance(current, dict) or key_token not in current:
                return None
            current = current[key_token]
        else:
            if not isinstance(current, list):
                return None
            index = int(index_token)
            if index >= len(current):
                return None
            current = current[index]
    return current


def _scalar_at_path(payload: Any, path: str) -> Any:
    if not path:
        return None
    value = _resolve_path(payload, path)
    if isinstance(value, (dict, list)):
        return None
    return value


def _find_first_value(
    payload: Any,
    normalized_keys: set[str],
) -> Tuple[str, Any]:
    for path, value in _walk_json(payload):
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            if (
                _normalize_key(key) in normalized_keys
                and child not in (None, "", [], {})
            ):
                return f"{path}.{key}", child
    return "", None


def _find_first_integer(
    payload: Any,
    normalized_keys: set[str],
) -> Tuple[str, Optional[int]]:
    path, value = _find_first_value(payload, normalized_keys)
    if isinstance(value, bool):
        return path, None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return path, None
    return path, number if number >= 0 else None


def _walk_json(value: Any, path: str = "$") -> Iterable[Tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_json(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{path}[{index}]")


def _value(mapping: Dict[str, Any], keys: Sequence[str]) -> Any:
    lookup = {_normalize_key(key): value for key, value in mapping.items()}
    for key in keys:
        value = lookup.get(_normalize_key(key))
        if value not in (None, "", [], {}):
            return value
    return None


def _text_value(mapping: Dict[str, Any], keys: Sequence[str]) -> str:
    return _to_text(_value(mapping, keys))


def _to_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, dict):
        for key in ("name", "label", "value", "displayName", "url"):
            if value.get(key) not in (None, "", [], {}):
                return _to_text(value[key])
        return ""
    if isinstance(value, list):
        return ", ".join(
            text
            for text in (_to_text(item) for item in value)
            if text
        )
    return str(value).strip()


def _absolute_url(source_url: str, value: str) -> str:
    return urljoin(source_url, value) if value else ""


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _product_signature(
    products: Sequence[ProductSeed],
    raw_products: Sequence[Any],
) -> str:
    values = [
        seed.dedup_key
        or seed.sku
        or seed.product_id
        or seed.detail_url
        for seed in products
    ]
    if not any(values):
        values = [
            json.dumps(item, ensure_ascii=False, sort_keys=True)[:1000]
            for item in raw_products[:100]
        ]
    normalized = "\n".join(sorted({value for value in values if value}))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


__all__ = ["CatalogParser"]
