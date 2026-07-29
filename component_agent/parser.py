from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import FetchResult, PageKind, ProductRecord


PRODUCT_LIST_KEYS = {
    "productrecordlist",
    "products",
    "results",
    "items",
    "productlist",
}


class PageInspector:
    """Classify a response before the agent selects a parser/transport."""

    @staticmethod
    def inspect(result: FetchResult) -> PageKind:
        text = result.text or ""
        content_type = (result.content_type or "").lower()
        stripped = text.lstrip()
        if not stripped:
            return PageKind.EMPTY
        if "var _xvasu" in text and "var _xvpts" in text:
            return PageKind.ANTI_BOT_CHALLENGE
        if (
            "getRenderData()" in text
            and re.search(r'id=["\']renderData["\']', text, re.I)
        ):
            return PageKind.ANTI_BOT_CHALLENGE
        if "json" in content_type or stripped.startswith(("{", "[")):
            try:
                json.loads(text)
                return PageKind.JSON_API
            except (json.JSONDecodeError, TypeError):
                pass
        if re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\']', text, re.I):
            return PageKind.NEXT_SSR
        if re.search(r'<script[^>]+type=["\']application/ld\+json["\']', text, re.I):
            return PageKind.JSON_LD

        soup = BeautifulSoup(text, "html.parser")
        visible_text = " ".join(soup.stripped_strings)
        script_count = len(soup.select("script[src], script:not([src])"))
        js_markers = (
            "__nuxt",
            "ng-version",
            "data-reactroot",
            "atomic-result-list",
            'id="root"',
            'id="app"',
        )
        if script_count >= 5 and (
            len(visible_text) < 300 or any(marker in text.lower() for marker in js_markers)
        ):
            return PageKind.JAVASCRIPT_RENDERED
        return PageKind.STATIC_HTML


class ProductParser:
    """Parse API, SSR, JSON-LD and ordinary HTML into one product schema."""

    def parse_catalog(
        self,
        result: FetchResult,
        site: str,
        supplier: str,
        query: str,
    ) -> List[ProductRecord]:
        kind = PageInspector.inspect(result)
        products: List[ProductRecord] = []

        if kind == PageKind.JSON_API:
            try:
                payload = json.loads(result.text)
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                products.extend(self._products_from_payload(payload, site, supplier, result.url))
        else:
            soup = BeautifulSoup(result.text, "html.parser")
            next_data = soup.select_one("#__NEXT_DATA__")
            if next_data:
                try:
                    payload = json.loads(next_data.get_text(strip=True))
                    products.extend(self._products_from_payload(payload, site, supplier, result.url))
                except json.JSONDecodeError:
                    pass

            products.extend(self._products_from_jsonld(soup, site, supplier, result.url))
            products.extend(self._products_from_microdata(soup, site, supplier, result.url))
            if not products:
                products.extend(self._products_from_html_cards(soup, site, supplier, result.url, query))

        return self._deduplicate(products)

    def parse_detail(
        self,
        result: FetchResult,
        site: str,
        supplier: str,
        fallback: Optional[ProductRecord] = None,
    ) -> ProductRecord:
        candidates = self.parse_catalog(
            result,
            site=site,
            supplier=supplier,
            query=(fallback.model if fallback else ""),
        )
        if candidates:
            if fallback:
                candidates.sort(key=lambda item: item.relevance(fallback.model or fallback.sku), reverse=True)
            product = candidates[0]
        else:
            product = ProductRecord(site=site, supplier=supplier, source_url=result.url)

        soup = BeautifulSoup(result.text, "html.parser")
        attrs = self._extract_attributes(soup)
        product.attributes = {**product.attributes, **attrs}

        if not product.title:
            product.title = self._first_text(soup, ["h1", "meta[property='og:title']", "title"])
        if not product.model:
            product.model = self._attribute_value(attrs, ("型号", "产品型号", "part number", "mpn", "model"))
        if not product.manufacturer:
            product.manufacturer = self._attribute_value(
                attrs, ("制造商", "生产厂家", "厂商", "品牌", "manufacturer", "brand")
            )
        if not product.package:
            product.package = self._attribute_value(
                attrs, ("封装", "封装规格", "封装型号", "package", "case/package")
            )
        if product.stock in (None, ""):
            product.stock = self._attribute_value(attrs, ("库存", "stock", "availability")) or None
        if not product.datasheet_url:
            for link in soup.select("a[href]"):
                href = str(link.get("href", ""))
                if href.lower().endswith(".pdf") or "datasheet" in href.lower():
                    product.datasheet_url = urljoin(result.url, href)
                    break
        if not product.image_url:
            image = soup.select_one("meta[property='og:image'], img[itemprop='image'], main img")
            if image:
                product.image_url = urljoin(
                    result.url,
                    str(image.get("content", "") or image.get("src", "")),
                )
        product.source_url = result.url
        return product

    @staticmethod
    def has_no_results(text: str) -> bool:
        lowered = re.sub(r"\s+", " ", text).lower()
        markers = (
            "未找到",
            "没有找到",
            "无搜索结果",
            "no results",
            "couldn't find anything",
            "0 results",
        )
        return any(marker in lowered for marker in markers)

    def _products_from_payload(
        self, payload: Any, site: str, supplier: str, source_url: str
    ) -> List[ProductRecord]:
        mappings: List[Dict[str, Any]] = []
        if isinstance(payload, dict) and self._looks_like_product(payload):
            mappings.append(payload)
        for values in self._find_product_lists(payload):
            mappings.extend(item for item in values if isinstance(item, dict))

        products: List[ProductRecord] = []
        for mapping in mappings:
            record = self._normalize_mapping(mapping, site, supplier, source_url)
            if record.key:
                products.append(record)
        return products

    def _find_product_lists(self, value: Any, depth: int = 0) -> Iterator[List[Any]]:
        if depth > 12:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, list) and key.lower() in PRODUCT_LIST_KEYS:
                    if any(isinstance(item, dict) for item in child):
                        yield child
                yield from self._find_product_lists(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                yield from self._find_product_lists(child, depth + 1)

    def _normalize_mapping(
        self, mapping: Dict[str, Any], site: str, supplier: str, source_url: str
    ) -> ProductRecord:
        raw = mapping.get("productVO") if isinstance(mapping.get("productVO"), dict) else mapping
        product = raw or mapping

        model = self._pick(product, "productModel", "genericPartNumber", "mpn", "model", "partNumber")
        model = model or self._pick(mapping, "lightProductModel", "genericPartNumber", "mpn", "model")
        sku = self._pick(product, "productCode", "sku", "lightProductCode", "pro_sno", "partNumber")
        sku = sku or self._pick(mapping, "lightProductCode", "sku", "partNumber")
        title = self._pick(product, "productName", "title", "name") or self._pick(
            mapping, "lightProductName", "title", "name"
        )
        if not model and self._looks_part_number(title):
            model = title

        brand = self._pick(product, "productGradePlateName", "manufacturer", "brandName", "pro_maf")
        brand = brand or self._pick(mapping, "lightBrandName", "manufacturer", "brandName")
        if not brand:
            brand = self._nested_name(mapping.get("brand") or product.get("brand"))

        package = self._pick(product, "encapsulationModel", "package", "reference_package", "casePackage")
        package = package or self._pick(mapping, "lightStandard", "package", "reference_package")

        stock = self._pick_value(
            product,
            "validStockNumber",
            "stockNumber",
            "totalStockNumber",
            "stock",
            "inventory",
            "availability",
        )
        if stock is None:
            stock = self._pick_value(mapping, "totalStockNumber", "stock", "availability")

        prices = self._normalize_prices(product, mapping)
        attributes = mapping.get("paramLinkedMap") or product.get("attributes") or mapping.get("attributes") or {}
        if not isinstance(attributes, dict):
            attributes = {}

        product_id = self._pick(product, "productId", "id")
        detail_url = self._pick(product, "detail_url", "detailUrl", "url") or self._pick(
            mapping, "detail_url", "detailUrl", "url"
        )
        offers = mapping.get("offers") or product.get("offers")
        if isinstance(offers, dict):
            detail_url = detail_url or str(offers.get("url") or "")
        if not detail_url and site == "szlcsc" and product_id:
            detail_url = f"https://item.szlcsc.com/{product_id}.html"
        if detail_url:
            detail_url = urljoin(source_url, str(detail_url))

        image = self._pick(product, "breviaryImageUrl", "bigImageUrl", "image_url", "image")
        image = image or self._pick(mapping, "image_url", "image")
        if isinstance(mapping.get("image"), list):
            image = str(mapping["image"][0]) if mapping["image"] else image

        datasheet = self._pick(product, "datasheet_url", "datasheetUrl") or self._pick(
            mapping, "datasheet_url", "datasheetUrl"
        )
        if not datasheet:
            datasheet = self._datasheet_from_file_list(product.get("fileTypeVOList"))

        return ProductRecord(
            site=site,
            supplier=supplier,
            model=str(model or "").strip(),
            sku=str(sku or "").strip(),
            title=str(title or "").strip(),
            manufacturer=str(brand or "").strip(),
            price=prices,
            stock=stock,
            package=str(package or "").strip(),
            moq=self._pick_value(product, "minBuyNumber", "moq", "minimumOrderQuantity"),
            description=str(
                self._pick(mapping, "lightProductIntro", "description", "productDesc")
                or self._pick(product, "remark", "description", "productDesc")
                or ""
            ).strip(),
            attributes=attributes,
            datasheet_url=urljoin(source_url, str(datasheet)) if datasheet else "",
            image_url=urljoin(source_url, str(image)) if image else "",
            detail_url=detail_url,
            source_url=source_url,
            extra={"raw_id": product_id} if product_id else {},
        )

    def _products_from_jsonld(
        self, soup: BeautifulSoup, site: str, supplier: str, source_url: str
    ) -> List[ProductRecord]:
        products: List[ProductRecord] = []
        for script in soup.select("script[type='application/ld+json']"):
            try:
                payload = json.loads(script.get_text(strip=True))
            except (json.JSONDecodeError, TypeError):
                continue
            for node in self._walk_json(payload):
                if not isinstance(node, dict) or str(node.get("@type", "")).lower() != "product":
                    continue
                record = self._normalize_mapping(node, site, supplier, source_url)
                offers = node.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    availability = offers.get("availability")
                    record.stock = self._availability(availability) if availability else record.stock
                    if not record.price and offers.get("price") not in (None, ""):
                        record.price = [{
                            "quantity": 1,
                            "unit_price": offers.get("price"),
                            "currency": offers.get("priceCurrency", ""),
                        }]
                    record.detail_url = urljoin(source_url, str(offers.get("url") or record.detail_url))
                if record.key:
                    products.append(record)
        return products

    def _products_from_microdata(
        self, soup: BeautifulSoup, site: str, supplier: str, source_url: str
    ) -> List[ProductRecord]:
        products: List[ProductRecord] = []
        for node in soup.select("[itemtype$='/Product']"):
            name = self._itemprop(node, "name")
            if not name:
                continue
            offer = node.select_one("[itemtype$='/Offer']")
            brand = node.select_one("[itemtype$='/Brand']")
            detail_url = self._itemprop(offer, "url") if offer else ""
            price = self._itemprop(offer, "price") if offer else ""
            currency = self._itemprop(offer, "priceCurrency") if offer else ""
            availability = self._itemprop(offer, "availability") if offer else ""
            products.append(ProductRecord(
                site=site,
                supplier=supplier,
                model=name,
                title=name,
                manufacturer=self._itemprop(brand, "name") if brand else "",
                price=[{"quantity": 1, "unit_price": price, "currency": currency}] if price else [],
                stock=self._availability(availability) if availability else None,
                description=self._itemprop(node, "description"),
                image_url=urljoin(source_url, self._itemprop(node, "image")),
                detail_url=urljoin(source_url, detail_url),
                source_url=source_url,
            ))
        return products

    def _products_from_html_cards(
        self,
        soup: BeautifulSoup,
        site: str,
        supplier: str,
        source_url: str,
        query: str,
    ) -> List[ProductRecord]:
        products: List[ProductRecord] = []
        compact_query = self._compact(query)
        selectors = (
            "a[href*='/detail/']",
            "a[href*='item.szlcsc.com']",
            "a[href*='/product/']",
            "a[data-sku]",
        )
        for link in soup.select(",".join(selectors)):
            text = link.get_text(" ", strip=True)
            href = urljoin(source_url, str(link.get("href", "")))
            nearby = link.parent.get_text(" ", strip=True) if link.parent else text
            if compact_query and compact_query not in self._compact(text + " " + nearby + " " + href):
                continue
            model = self._part_number_from_text(text) or query
            products.append(ProductRecord(
                site=site,
                supplier=supplier,
                model=model,
                title=text or model,
                sku=str(link.get("data-sku", "")),
                detail_url=href,
                source_url=source_url,
            ))
        return products

    @staticmethod
    def _extract_attributes(soup: BeautifulSoup) -> Dict[str, str]:
        attrs: Dict[str, str] = {}
        for row in soup.select("table tr"):
            cells = row.select("th, td")
            if len(cells) >= 2:
                key = cells[0].get_text(" ", strip=True).rstrip(":：")
                value = cells[1].get_text(" ", strip=True)
                if key and value and key.lower() not in {"属性", "值", "product attribute", "value"}:
                    attrs[key] = value
        for term in soup.select("dl dt"):
            definition = term.find_next_sibling("dd")
            if definition:
                key = term.get_text(" ", strip=True).rstrip(":：")
                value = definition.get_text(" ", strip=True)
                if key and value:
                    attrs[key] = value
        return attrs

    @staticmethod
    def _normalize_prices(product: Dict[str, Any], mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
        tiers = product.get("productPriceList") or mapping.get("productPriceList")
        prices: List[Dict[str, Any]] = []
        if isinstance(tiers, list):
            for tier in tiers:
                if not isinstance(tier, dict):
                    continue
                value = tier.get("productPrice", tier.get("thePrice", tier.get("price")))
                if value in (None, ""):
                    continue
                prices.append({
                    "quantity": tier.get("startPurchasedNumber", tier.get("num", 1)),
                    "unit_price": value,
                    "currency": tier.get("currency", "CNY"),
                })
        nums = product.get("nums") or mapping.get("nums") or []
        values = product.get("calc_sale_rmb_price") or mapping.get("calc_sale_rmb_price") or []
        if not prices and isinstance(nums, list) and isinstance(values, list):
            for num, value in zip(nums, values):
                prices.append({"quantity": num, "unit_price": value, "currency": "CNY"})
        direct = product.get("price")
        if not prices and direct not in (None, "", [], {}):
            if isinstance(direct, list):
                prices = direct
            else:
                prices = [{"quantity": 1, "unit_price": direct, "currency": product.get("priceCurrency", "")}]
        return prices

    @staticmethod
    def _availability(value: Any) -> str:
        lowered = str(value or "").lower()
        if "outofstock" in lowered or "out_of_stock" in lowered:
            return "out_of_stock"
        if "instock" in lowered or "in_stock" in lowered:
            return "in_stock"
        if "preorder" in lowered or "pre_order" in lowered:
            return "pre_order"
        return str(value or "")

    @staticmethod
    def _looks_like_product(mapping: Dict[str, Any]) -> bool:
        keys = {key.lower() for key in mapping}
        return bool(keys & {"productmodel", "genericpartnumber", "mpn", "sku", "productcode"})

    @staticmethod
    def _walk_json(value: Any) -> Iterator[Any]:
        yield value
        if isinstance(value, dict):
            for child in value.values():
                yield from ProductParser._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from ProductParser._walk_json(child)

    @staticmethod
    def _pick(mapping: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = mapping.get(key)
            if value not in (None, "", [], {}):
                if isinstance(value, (dict, list)):
                    continue
                return value
        return ""

    @staticmethod
    def _pick_value(mapping: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in mapping and mapping[key] not in (None, ""):
                return mapping[key]
        return None

    @staticmethod
    def _nested_name(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("name") or value.get("brandName") or "")
        return str(value or "")

    @staticmethod
    def _datasheet_from_file_list(file_types: Any) -> str:
        if not isinstance(file_types, list):
            return ""
        for file_type in file_types:
            if not isinstance(file_type, dict) or file_type.get("fileType") != "pdf_property":
                continue
            details = file_type.get("detailVOList") or []
            if details and isinstance(details[0], dict):
                path = str(details[0].get("fileUrl") or "")
                if path.startswith("/"):
                    return "https://datasheet.lcsc.com" + path
                return path
        return ""

    @staticmethod
    def _itemprop(node: Optional[Tag], name: str) -> str:
        if node is None:
            return ""
        item = node.select_one(f"[itemprop='{name}']")
        if not item:
            return ""
        return str(item.get("content", "") or item.get("href", "") or item.get_text(" ", strip=True))

    @staticmethod
    def _first_text(soup: BeautifulSoup, selectors: Sequence[str]) -> str:
        for selector in selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            value = str(node.get("content", "") or node.get_text(" ", strip=True)).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _attribute_value(attrs: Dict[str, Any], aliases: Sequence[str]) -> str:
        for key, value in attrs.items():
            lowered = key.strip().lower()
            if any(alias.lower() == lowered or alias.lower() in lowered for alias in aliases):
                return str(value)
        return ""

    @staticmethod
    def _looks_part_number(value: Any) -> bool:
        text = str(value or "").strip()
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+\-]{4,}", text))

    @staticmethod
    def _part_number_from_text(value: str) -> str:
        matches = re.findall(r"\b[A-Za-z0-9][A-Za-z0-9._/+\-]{4,}\b", value)
        return matches[0] if matches else ""

    @staticmethod
    def _compact(value: Any) -> str:
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

    @staticmethod
    def _deduplicate(products: Iterable[ProductRecord]) -> List[ProductRecord]:
        unique: List[ProductRecord] = []
        seen: Set[str] = set()
        for product in products:
            key = "|".join((product.site, product.key, product.detail_url)).upper()
            if not product.key or key in seen:
                continue
            seen.add(key)
            unique.append(product)
        return unique
