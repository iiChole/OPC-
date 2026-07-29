"""Site-specific planning and parsing hooks used by the coordinator."""

from __future__ import annotations

from typing import Protocol

from ..agents.decision import WebsiteDecision
from ..catalog.parser import CatalogParser
from ..orchestration.robots import RobotsPolicy
from ..planning.models import CrawlPlan, FetchTool


class SiteAdapter(Protocol):
    key: str

    def matches(self, url: str) -> bool:
        ...

    def build_plan(
        self,
        url: str,
        decision: WebsiteDecision,
        fetch_tool: FetchTool,
        robots_policy: RobotsPolicy,
        category_limit: int = 0,
    ) -> CrawlPlan:
        ...

    def catalog_parser(self) -> CatalogParser:
        ...


__all__ = ["SiteAdapter"]
