from .base import SiteAdapter
from ..profiles import SITE_PROFILES


class ICKeyAdapter(SiteAdapter):
    """ICKey: server search shell, API-backed catalog, static detail tables."""

    profile = SITE_PROFILES["ickey"]
