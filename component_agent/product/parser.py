"""Normalize detail HTML, JSON, JSON-LD, and embedded state into one schema."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..catalog.models import ProductSeed
from ..models import FetchResult, ProductRecord, utc_now
from ..parser import ProductParser
from .models import NormalizedProduct


STANDARD_KEYS = {
    "sku",
    "productid",
    "id",
    "itemid",
    "partnumber",
    "mpn",
    "manufacturerpartnumber",
    "productmodel",
    "model",
    "title",
    "name",
    "productname",
    "manufacturer",
    "manufacturername",
    "brand",
    "brandname",
    "package",
    "packagetype",
    "packaging",
    "casepackage",
    "stock",
    "stockquantity",
    "inventory",
    "availability",
    "price",
    "prices",
    "pricelist",
    "pricing",
    "offers",
    "moq",
    "minimumorderquantity",
    "minorderqty",
    "description",
    "shortdescription",
    "summary",
    "image",
    "imageurl",
    "thumbnail",
    "datasheet",
    "datasheeturl",
    "url",
    "detailurl",
    "producturl",
    "attributes",
    "specifications",
    "parameters",
    "productcode",
    "stockcode",
    "catalognumber",
    "goodsid",
    "genericpartnumber",
    "mfr",
    "availablequantity",
    "validstocknumber",
    "unitprice",
    "minbuynumber",
    "productdesc",
    "remark",
    "primaryimage",
    "bigimageurl",
    "href",
    "specs",
    "paramlinkedmap",
    "technicalparameters",
}

PRODUCT_ANCHOR_KEYS = {
    "sku",
    "productcode",
    "stockcode",
    "catalognumber",
    "productid",
    "goodsid",
    "partnumber",
    "mpn",
    "manufacturerpartnumber",
    "productmodel",
    "genericpartnumber",
    "model",
    "title",
    "productname",
}


class ProductDetailParser:
    def __init__(self, base_parser: Optional[ProductParser] = None) -> None:
        self.base_parser = base_parser or ProductParser()

    def parse(
        self,
        result: FetchResult,
        seed: ProductSeed,
        supplier_name: str = "",
    ) -> NormalizedProduct:
        product = NormalizedProduct.from_seed(seed)
        fallback = ProductRecord(
            site=seed.site_key,
            supplier=supplier_name or seed.site_key,
            model=product.part_number or product.model or seed.title,
            sku=seed.sku,
            title=seed.title,
            manufacturer=seed.manufacturer,
            price=seed.price if isinstance(seed.price, list) else [],
            stock=seed.stock,
            package=seed.package,
            moq=seed.moq,
            description=seed.description,
            attributes=dict(seed.attributes),
            datasheet_url=seed.datasheet_url,
            image_url=seed.image_url,
            detail_url=seed.detail_url,
            source_url=seed.source_url,
            extra=dict(seed.extra),
        )
        legacy = self.base_parser.parse_detail(
            result,
            site=seed.site_key,
            supplier=supplier_name or seed.site_key,
            fallback=fallback,
        )
        _merge_legacy_record(product, legacy)

        payloads = _detail_payloads(result)
        best_mapping = _select_best_mapping(payloads, seed)
        if best_mapping is not None:
            _merge_mapping(product, best_mapping, result.url)

        if not _looks_like_json(result.text):
            soup = BeautifulSoup(result.text or "", "html.parser")
            html_attributes = _extract_html_attributes(soup)
            product.attributes.update(html_attributes)
            metadata = _extract_product_metadata(soup)
            if metadata:
                existing = product.extra.get("page_metadata")
                if not isinstance(existing, dict):
                    existing = {}
                product.extra["page_metadata"] = {**existing, **metadata}

        product.source_url = result.url
        product.detail_url = product.detail_url or seed.detail_url or result.url
        product.transport = result.transport
        product.status_code = result.status_code
        product.fetched_at = utc_now()
        product.fetch_status = "complete"
        product.error = ""
        product.ensure_defaults()
        return product


def _merge_legacy_record(
    product: NormalizedProduct,
    detail: ProductRecord,
) -> None:
    detail_title = detail.title.strip()
    if detail_title:
        product.detail_title = detail_title
        product.title = detail_title
    if detail.model:
        product.part_number = detail.model
        product.model = detail.model
    if detail.sku:
        product.sku = detail.sku
    for field_name in (
        "manufacturer",
        "package",
        "description",
        "datasheet_url",
        "image_url",
        "detail_url",
    ):
        value = getattr(detail, field_name)
        if value not in (None, "", [], {}):
            setattr(product, field_name, value)
    if detail.stock not in (None, ""):
        product.stock = detail.stock
    if detail.price:
        product.price = detail.price
    if detail.moq not in (None, ""):
        product.moq = detail.moq
    product.attributes.update(detail.attributes or {})
    product.extra.update(detail.extra or {})


def _merge_mapping(
    product: NormalizedProduct,
    mapping: Dict[str, Any],
    source_url: str,
) -> None:
    nested = (
        mapping.get("productVO")
        if isinstance(mapping.get("productVO"), dict)
        else mapping
    )
    lookup = {_normalize_key(key): value for key, value in nested.items()}
    outer_lookup = {_normalize_key(key): value for key, value in mapping.items()}

    def value(*keys: str) -> Any:
        for key in keys:
            normalized = _normalize_key(key)
            candidate = lookup.get(normalized)
            if candidate not in (None, "", [], {}):
                return candidate
            candidate = outer_lookup.get(normalized)
            if candidate not in (None, "", [], {}):
                return candidate
        return None

    part_number = _text(value(
        "partNumber",
        "mpn",
        "manufacturerPartNumber",
        "productModel",
        "model",
        "genericPartNumber",
    ))
    if part_number:
        product.part_number = part_number
        product.model = part_number
    sku = _text(value("sku", "productCode", "stockCode", "catalogNumber"))
    if sku:
        product.sku = sku
    product_id = _text(value("productId", "id", "itemId", "goodsId"))
    if product_id:
        product.product_id = product_id
    title = _text(value("title", "productName", "name"))
    if title:
        product.detail_title = title
        product.title = title
    manufacturer = _text(value(
        "manufacturer",
        "manufacturerName",
        "brand",
        "brandName",
        "mfr",
    ))
    if manufacturer:
        product.manufacturer = manufacturer
    package = _text(value(
        "package",
        "packageType",
        "packaging",
        "casePackage",
        "encapsulationModel",
    ))
    if package:
        product.package = package

    stock = value(
        "stock",
        "stockQuantity",
        "inventory",
        "availability",
        "availableQuantity",
        "validStockNumber",
    )
    offers = value("offers")
    if isinstance(offers, list):
        offers = next(
            (item for item in offers if isinstance(item, dict)),
            None,
        )
    if stock in (None, "") and isinstance(offers, dict):
        inventory_level = offers.get("inventoryLevel")
        if isinstance(inventory_level, dict):
            stock = inventory_level.get("value")
        elif inventory_level not in (None, ""):
            stock = inventory_level
        if stock in (None, ""):
            stock = offers.get("availability")
    if stock not in (None, ""):
        product.stock = stock
    price = _normalize_price(
        value("price", "prices", "priceList", "pricing", "offers", "unitPrice")
    )
    if price not in (None, "", [], {}):
        product.price = price
    moq = value("moq", "minimumOrderQuantity", "minOrderQty", "minBuyNumber")
    if moq not in (None, ""):
        product.moq = moq
    description = _text(value(
        "description",
        "shortDescription",
        "summary",
        "productDesc",
        "remark",
    ))
    if description:
        product.description = description

    image = _url_text(value(
        "imageUrl",
        "image",
        "thumbnail",
        "primaryImage",
        "bigImageUrl",
    ))
    if image:
        product.image_url = urljoin(source_url, image)
    datasheet = _url_text(value(
        "datasheetUrl",
        "datasheet",
        "datasheet_url",
    ))
    if not datasheet:
        subject = value("subjectOf")
        if isinstance(subject, list):
            subject = next(
                (item for item in subject if isinstance(item, dict)),
                None,
            )
        if isinstance(subject, dict):
            datasheet = _url_text(
                subject.get("url") or subject.get("contentUrl")
            )
    if datasheet:
        product.datasheet_url = urljoin(source_url, datasheet)
    detail_url = _text(value(
        "detailUrl",
        "productUrl",
        "url",
        "href",
    ))
    if detail_url:
        product.detail_url = urljoin(source_url, detail_url)

    product.attributes.update(_attributes_from_mapping(mapping))
    site_fields: Dict[str, Any] = {}
    containers = [mapping]
    if nested is not mapping:
        containers.append(nested)
    for container in containers:
        for key, child in container.items():
            normalized = _normalize_key(key)
            if normalized in STANDARD_KEYS:
                continue
            if container is mapping and child is nested:
                continue
            site_fields[key] = child
    if site_fields:
        existing = product.extra.get("site_fields")
        if not isinstance(existing, dict):
            existing = {}
        product.extra["site_fields"] = {**existing, **site_fields}


def _detail_payloads(result: FetchResult) -> List[Any]:
    text = str(result.text or "")
    if _looks_like_json(text):
        try:
            return [json.loads(text)]
        except (json.JSONDecodeError, TypeError):
            return []
    soup = BeautifulSoup(text, "html.parser")
    payloads: List[Any] = []
    selectors = (
        "script[type='application/ld+json']",
        "script[type='application/json']",
        "script#__NEXT_DATA__",
        "script#__NUXT_DATA__",
        "script#__INITIAL_STATE__",
    )
    for script in soup.select(",".join(selectors)):
        raw = script.get_text(strip=True)
        if not raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return payloads


def _select_best_mapping(
    payloads: Sequence[Any],
    seed: ProductSeed,
) -> Optional[Dict[str, Any]]:
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    normalized_sku = _compact(seed.sku)
    normalized_product_id = _compact(seed.product_id)
    for payload in payloads:
        for value in _walk_json(payload):
            if not isinstance(value, dict):
                continue
            keys = {_normalize_key(key) for key in value}
            score = len(keys & STANDARD_KEYS)
            item_sku = _compact(_mapping_text(
                value,
                "sku",
                "productCode",
                "stockCode",
                "catalogNumber",
            ))
            item_id = _compact(_mapping_text(
                value,
                "productId",
                "id",
                "itemId",
                "goodsId",
            ))
            if normalized_sku and item_sku == normalized_sku:
                score += 100
            if normalized_product_id and item_id == normalized_product_id:
                score += 100
            identity_match = bool(
                (normalized_sku and item_sku == normalized_sku)
                or (
                    normalized_product_id
                    and item_id == normalized_product_id
                )
            )
            product_type = str(value.get("@type", "")).lower() == "product"
            if product_type:
                score += 40
            has_product_anchor = bool(keys & PRODUCT_ANCHOR_KEYS)
            if score >= 2 or identity_match or product_type or has_product_anchor:
                candidates.append((score, value))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _attributes_from_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {}
    for key, value in mapping.items():
        normalized = _normalize_key(key)
        if normalized in {
            "attributes",
            "specifications",
            "specs",
            "paramlinkedmap",
            "parameters",
            "technicalparameters",
        }:
            attributes.update(_coerce_attributes(value))
    product_vo = mapping.get("productVO")
    if isinstance(product_vo, dict):
        attributes.update(_attributes_from_mapping(product_vo))
    return attributes


def _coerce_attributes(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return {
            str(key).strip(): child
            for key, child in value.items()
            if str(key).strip() and child not in (None, "")
        }
    if not isinstance(value, list):
        return {}
    attributes: Dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        key = _mapping_text(
            item,
            "name",
            "key",
            "label",
            "attributeName",
            "parameterName",
        )
        child = _mapping_value(
            item,
            "value",
            "displayValue",
            "attributeValue",
            "parameterValue",
        )
        if key and child not in (None, ""):
            attributes[key] = child
    return attributes


def _extract_html_attributes(soup: BeautifulSoup) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {}
    for row in soup.select("table tr"):
        cells = row.select("th, td")
        if len(cells) < 2:
            continue
        key = cells[0].get_text(" ", strip=True).rstrip(":：")
        value = cells[1].get_text(" ", strip=True)
        if (
            key
            and value
            and key.lower() not in {
                "属性",
                "值",
                "product attribute",
                "attribute",
                "value",
            }
        ):
            attributes[key] = value
    for term in soup.select("dl dt"):
        definition = term.find_next_sibling("dd")
        if definition is None:
            continue
        key = term.get_text(" ", strip=True).rstrip(":：")
        value = definition.get_text(" ", strip=True)
        if key and value:
            attributes[key] = value
    for node in soup.select("[data-attribute-name]"):
        key = str(node.get("data-attribute-name", "")).strip()
        value = str(
            node.get("data-attribute-value", "")
            or node.get_text(" ", strip=True)
        ).strip()
        if key and value:
            attributes[key] = value
    return attributes


def _extract_product_metadata(soup: BeautifulSoup) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for node in soup.select("meta[property], meta[name], meta[itemprop]"):
        key = str(
            node.get("property", "")
            or node.get("name", "")
            or node.get("itemprop", "")
        ).strip()
        value = str(node.get("content", "")).strip()
        lowered = key.lower()
        if (
            key
            and value
            and (
                lowered.startswith(("og:", "product:"))
                or lowered in {
                    "sku",
                    "mpn",
                    "brand",
                    "manufacturer",
                    "price",
                    "pricecurrency",
                    "availability",
                }
            )
        ):
            metadata[key] = value
    return metadata


def _normalize_price(value: Any) -> Any:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if value.get("price") not in (None, ""):
            return [{
                "quantity": 1,
                "unit_price": value.get("price"),
                "currency": value.get("priceCurrency", ""),
            }]
        return value
    return [{"quantity": 1, "unit_price": value, "currency": ""}]


def _mapping_value(mapping: Dict[str, Any], *keys: str) -> Any:
    lookup = {_normalize_key(key): value for key, value in mapping.items()}
    for key in keys:
        value = lookup.get(_normalize_key(key))
        if value not in (None, "", [], {}):
            return value
    return None


def _mapping_text(mapping: Dict[str, Any], *keys: str) -> str:
    return _text(_mapping_value(mapping, *keys))


def _text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, dict):
        for key in ("name", "label", "value", "displayName", "url"):
            if value.get(key) not in (None, "", [], {}):
                return _text(value[key])
        return ""
    if isinstance(value, list):
        return ", ".join(
            text
            for text in (_text(item) for item in value)
            if text
        )
    return str(value).strip()


def _url_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, list):
        for item in value:
            resolved = _url_text(item)
            if resolved:
                return resolved
        return ""
    if isinstance(value, dict):
        return _url_text(
            value.get("url")
            or value.get("contentUrl")
            or value.get("src")
        )
    return str(value).strip()


def _walk_json(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _looks_like_json(value: str) -> bool:
    stripped = str(value or "").lstrip()
    return stripped.startswith(("{", "["))


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _compact(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


__all__ = ["ProductDetailParser"]
