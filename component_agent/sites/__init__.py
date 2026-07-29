"""Site adapter registry."""

from __future__ import annotations

from typing import List, Optional

from .base import SiteAdapter
from .icgoo import ICGooSiteAdapter


SITE_ADAPTERS: List[SiteAdapter] = [ICGooSiteAdapter()]


def find_site_adapter(url: str) -> Optional[SiteAdapter]:
    return next((adapter for adapter in SITE_ADAPTERS if adapter.matches(url)), None)


__all__ = [
    "ICGooSiteAdapter",
    "SITE_ADAPTERS",
    "SiteAdapter",
    "find_site_adapter",
]
