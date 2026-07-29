"""robots.txt policy enforcement shared by planning and execution tools."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib import robotparser
from urllib.parse import urlsplit, urlunsplit

import requests

from ..planning.models import FetchTool


DEFAULT_CRAWLER_USER_AGENT = "MultiAgentCrawler/1.0"


@dataclass
class RobotsPolicy:
    robots_url: str
    target_url: str
    user_agent: str
    allowed: bool
    status_code: int = 0
    reason: str = ""
    crawl_delay: Optional[float] = None
    sitemap_urls: List[str] = field(default_factory=list)
    disallow_rules: List[str] = field(default_factory=list)
    _parser: Optional[robotparser.RobotFileParser] = field(
        default=None,
        repr=False,
        compare=False,
    )

    def allows(self, url: str) -> bool:
        if not self.allowed:
            return False
        if self._parser is None:
            return True
        return self._parser.can_fetch(self.user_agent, url)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "robots_url": self.robots_url,
            "target_url": self.target_url,
            "user_agent": self.user_agent,
            "allowed": self.allowed,
            "status_code": self.status_code,
            "reason": self.reason,
            "crawl_delay": self.crawl_delay,
            "sitemap_urls": list(self.sitemap_urls),
            "disallow_rules": list(self.disallow_rules),
        }


class RobotsChecker:
    def __init__(
        self,
        timeout: int = 20,
        user_agent: str = DEFAULT_CRAWLER_USER_AGENT,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.session = session or requests.Session()

    def check(self, target_url: str) -> RobotsPolicy:
        robots_url = _robots_url(target_url)
        try:
            response = self.session.get(
                robots_url,
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
            )
        except requests.RequestException as exc:
            return RobotsPolicy(
                robots_url=robots_url,
                target_url=target_url,
                user_agent=self.user_agent,
                allowed=False,
                reason=f"robots_fetch_failed: {exc}",
            )

        if response.status_code == 404:
            return RobotsPolicy(
                robots_url=robots_url,
                target_url=target_url,
                user_agent=self.user_agent,
                allowed=True,
                status_code=404,
                reason="robots_not_found",
            )
        if response.status_code >= 400:
            return RobotsPolicy(
                robots_url=robots_url,
                target_url=target_url,
                user_agent=self.user_agent,
                allowed=False,
                status_code=response.status_code,
                reason=f"robots_http_error: HTTP {response.status_code}",
            )

        text = response.text or ""
        parser = robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(text.splitlines())
        allowed = parser.can_fetch(self.user_agent, target_url)
        crawl_delay = parser.crawl_delay(self.user_agent)
        if crawl_delay is None:
            crawl_delay = parser.crawl_delay("*")
        return RobotsPolicy(
            robots_url=robots_url,
            target_url=target_url,
            user_agent=self.user_agent,
            allowed=allowed,
            status_code=response.status_code,
            reason="allowed" if allowed else "disallowed_by_robots",
            crawl_delay=float(crawl_delay) if crawl_delay is not None else None,
            sitemap_urls=_sitemap_urls(text),
            disallow_rules=_disallow_rules(text),
            _parser=parser,
        )


class RobotsAwareFetchTool:
    """Reject every disallowed request before delegating to HTTP/browser tools."""

    def __init__(self, delegate: FetchTool, policy: RobotsPolicy) -> None:
        self.delegate = delegate
        self.policy = policy
        self.browser = getattr(delegate, "browser", None)

    def fetch(
        self,
        url: str,
        preferred_transport: str = "auto",
        headers: Optional[Dict[str, str]] = None,
    ):
        if not self.policy.allows(url):
            raise RuntimeError(f"robots.txt 禁止访问: {url}")
        return self.delegate.fetch(
            url,
            preferred_transport=preferred_transport,
            headers=headers,
        )


def _robots_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))


def _sitemap_urls(text: str) -> List[str]:
    values = re.findall(r"^\s*Sitemap\s*:\s*(\S+)\s*$", text, re.I | re.M)
    return list(dict.fromkeys(values))


def _disallow_rules(text: str) -> List[str]:
    values = re.findall(r"^\s*Disallow\s*:\s*(\S*)\s*$", text, re.I | re.M)
    return list(dict.fromkeys(value for value in values if value))


__all__ = [
    "DEFAULT_CRAWLER_USER_AGENT",
    "RobotsAwareFetchTool",
    "RobotsChecker",
    "RobotsPolicy",
]
