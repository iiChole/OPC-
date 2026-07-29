"""Electronic component website decision and crawling agents."""

from typing import TYPE_CHECKING, Any

from .agents.catalog import CatalogAgent
from .agents.crawl_plan import CrawlPlanAgent
from .agents.decision import WebsiteDecision, WebsiteDecisionAgent, WebsiteType
from .agents.product import ProductAgent
from .orchestration.validation import (
    CrawlExecutionSnapshot,
    CrawlRecoveryController,
    CrawlResultValidator,
    CrawlWorkflowGuard,
)
from .planning.models import CrawlPlan
from .product.models import NormalizedProduct, ProductResult

if TYPE_CHECKING:
    from .agent import ComponentSearchAgent
    from .models import CrawlRequest, CrawlReport, ProductRecord

__all__ = [
    "ComponentSearchAgent",
    "CrawlRequest",
    "CrawlReport",
    "ProductRecord",
    "WebsiteDecision",
    "WebsiteDecisionAgent",
    "WebsiteType",
    "CatalogAgent",
    "CrawlPlan",
    "CrawlPlanAgent",
    "ProductAgent",
    "ProductResult",
    "NormalizedProduct",
    "CrawlExecutionSnapshot",
    "CrawlRecoveryController",
    "CrawlResultValidator",
    "CrawlWorkflowGuard",
]


def __getattr__(name: str) -> Any:
    """Keep decision-only usage independent from optional crawler dependencies."""
    if name == "ComponentSearchAgent":
        from .agent import ComponentSearchAgent

        return ComponentSearchAgent
    if name in {"CrawlRequest", "CrawlReport", "ProductRecord"}:
        from .models import CrawlReport, CrawlRequest, ProductRecord

        return {
            "CrawlRequest": CrawlRequest,
            "CrawlReport": CrawlReport,
            "ProductRecord": ProductRecord,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
