from .base import SiteAdapter
from ..profiles import SITE_PROFILES


class SZLCSCAdapter(SiteAdapter):
    """SZLCSC: Next.js SSR catalog; __NEXT_DATA__ is rich enough for detail fallback."""

    profile = SITE_PROFILES["szlcsc"]
