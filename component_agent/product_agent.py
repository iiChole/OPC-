"""Backward-compatible imports for the ProductAgent implementation."""

from .agents.product import ProductAgent
from .product.models import NormalizedProduct, ProductIssue, ProductResult

__all__ = [
    "NormalizedProduct",
    "ProductAgent",
    "ProductIssue",
    "ProductResult",
]
