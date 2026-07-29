from .base import SiteAdapter
from .ickey import ICKeyAdapter
from .szlcsc import SZLCSCAdapter
from .ti import TIAdapter


def build_adapters(site_keys):
    registry = {
        "ickey": ICKeyAdapter,
        "szlcsc": SZLCSCAdapter,
        "ti": TIAdapter,
    }
    adapters = []
    for key in site_keys:
        normalized = key.strip().lower()
        if normalized not in registry:
            raise ValueError(f"未知站点: {key}; 可选: {', '.join(registry)}")
        adapters.append(registry[normalized]())
    return adapters


__all__ = ["SiteAdapter", "ICKeyAdapter", "SZLCSCAdapter", "TIAdapter", "build_adapters"]
