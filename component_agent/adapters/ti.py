from .base import SiteAdapter
from ..models import ProductRecord
from ..profiles import SITE_PROFILES


class TIAdapter(SiteAdapter):
    """TI: JavaScript search, JSON-LD product pages and selection-model APIs."""

    profile = SITE_PROFILES["ti"]

    def detail_headers(self, product: ProductRecord):
        return {
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.ti.com/",
        }
