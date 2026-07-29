from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple
from urllib.parse import quote_plus


@dataclass(frozen=True)
class SiteProfile:
    key: str
    name: str
    domains: Tuple[str, ...]
    structure: str
    catalog_strategy: str
    detail_strategy: str
    search_templates: Tuple[str, ...]
    preferred_transport: str = "requests"

    def search_urls(self, query: str) -> List[str]:
        encoded = quote_plus(query)
        return [template.format(query=encoded) for template in self.search_templates]


SITE_PROFILES: Dict[str, SiteProfile] = {
    "ickey": SiteProfile(
        key="ickey",
        name="云汉芯城 (ICKey)",
        domains=("ickey.cn", "search.ickey.cn"),
        structure="API-backed catalog with server-rendered search shell",
        catalog_strategy="requests HTML -> tokens/API or embedded/static result",
        detail_strategy="requests HTML attribute table",
        search_templates=("https://search.ickey.cn/?keyword={query}",),
    ),
    "szlcsc": SiteProfile(
        key="szlcsc",
        name="立创商城 (SZLCSC)",
        domains=("szlcsc.com", "so.szlcsc.com", "item.szlcsc.com"),
        structure="Next.js SSR with __NEXT_DATA__ and JSON-LD",
        catalog_strategy="requests HTML -> anti-bot cookie -> __NEXT_DATA__",
        detail_strategy="requests/Playwright detail; catalog data is fallback",
        search_templates=("https://so.szlcsc.com/global.html?k={query}",),
    ),
    "ti": SiteProfile(
        key="ti",
        name="Texas Instruments (TI)",
        domains=("ti.com", "www.ti.com"),
        structure="JSON-LD discovery + JavaScript search + selectionmodel API",
        catalog_strategy="Playwright search or JSON-LD category/API",
        detail_strategy="requests product HTML JSON-LD + selectionmodel API",
        search_templates=(
            "https://www.ti.com/sitesearch/en-us/docs/universalsearch.tsp?searchTerm={query}",
        ),
        preferred_transport="auto",
    ),
}


def select_profiles(site_keys: Iterable[str]) -> List[SiteProfile]:
    profiles: List[SiteProfile] = []
    for key in site_keys:
        normalized = key.strip().lower()
        if not normalized:
            continue
        if normalized not in SITE_PROFILES:
            raise ValueError(f"未知站点: {key}; 可选: {', '.join(SITE_PROFILES)}")
        profiles.append(SITE_PROFILES[normalized])
    return profiles
