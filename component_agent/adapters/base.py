from __future__ import annotations

from typing import Dict, List, Optional

from ..models import FetchResult, ProductRecord
from ..parser import ProductParser
from ..profiles import SiteProfile


class SiteAdapter:
    profile: SiteProfile

    def __init__(self) -> None:
        self.parser = ProductParser()

    def catalog_urls(self, query: str) -> List[str]:
        return self.profile.search_urls(query)

    def catalog_headers(self, url: str) -> Optional[Dict[str, str]]:
        return None

    def detail_headers(self, product: ProductRecord) -> Optional[Dict[str, str]]:
        return {"Referer": product.source_url} if product.source_url else None

    def parse_catalog(self, result: FetchResult, query: str) -> List[ProductRecord]:
        return self.parser.parse_catalog(
            result,
            site=self.profile.key,
            supplier=self.profile.name,
            query=query,
        )

    def parse_detail(self, result: FetchResult, fallback: ProductRecord) -> ProductRecord:
        return self.parser.parse_detail(
            result,
            site=self.profile.key,
            supplier=self.profile.name,
            fallback=fallback,
        )
