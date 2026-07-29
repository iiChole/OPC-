"""Backward-compatible imports for the CatalogAgent implementation."""

from .agents.catalog import CatalogAgent
from .catalog.models import (
    AgentHandoff,
    CatalogResult,
    CategoryTask,
    ProductSeed,
)

__all__ = [
    "AgentHandoff",
    "CatalogAgent",
    "CatalogResult",
    "CategoryTask",
    "ProductSeed",
]
