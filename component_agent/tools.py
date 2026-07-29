from __future__ import annotations

import base64
import random
import re
import shutil
import subprocess
import time
from typing import Dict, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import BrowserInspectionResult, FetchResult, NetworkObservation, PageKind
from .parser import PageInspector


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class CrawlToolError(RuntimeError):
    pass


class ToolUnavailable(CrawlToolError):
    pass


class RequestsTool:
    """HTTP tool with retries, throttling and the known SZLCSC cookie challenge."""

    def __init__(
        self,
        timeout: int = 30,
        retries: int = 3,
        delay: float = 0.35,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self._last_request_at = 0.0

    def fetch(self, url: str, headers: Optional[Dict[str, str]] = None) -> FetchResult:
        self._throttle()
        started = time.perf_counter()
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise CrawlToolError(f"requests 请求失败: {exc}") from exc
        result = self._to_result(response, started)

        if PageInspector.inspect(result) == PageKind.ANTI_BOT_CHALLENGE:
            if self._solve_szlcsc_challenge(result.text, result.url):
                self._throttle()
                started = time.perf_counter()
                try:
                    response = self.session.get(url, headers=headers, timeout=self.timeout)
                except requests.RequestException as exc:
                    raise CrawlToolError(f"Cookie challenge 重试失败: {exc}") from exc
                result = self._to_result(response, started)
        return result

    def _to_result(self, response: requests.Response, started: float) -> FetchResult:
        if response.encoding in (None, "ISO-8859-1"):
            response.encoding = response.apparent_encoding
        return FetchResult(
            url=response.url,
            text=response.text,
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            transport="requests",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            headers={key: value for key, value in response.headers.items()},
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.delay + random.random() * min(self.delay, 0.25) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _solve_szlcsc_challenge(self, html: str, url: str) -> bool:
        points = re.search(r"var _xvpts\s*=\s*([\d.]+);", html)
        salt = re.search(r"var _xvasu\s*=\s*(\d+);", html)
        if not points or not salt:
            return False
        key = b"tg09It3*9h"
        data = f"{points.group(1)}:{salt.group(1)}".encode()
        value = base64.b64encode(self._rc4(key, data)).decode()
        host_parts = (urlparse(url).hostname or "").split(".")
        domain = "." + ".".join(host_parts[-2:]) if len(host_parts) >= 2 else None
        self.session.cookies.set(f"tws2_{salt.group(1)}", value, domain=domain)
        return True

    @staticmethod
    def _rc4(key: bytes, data: bytes) -> bytes:
        state = list(range(256))
        j = 0
        for i in range(256):
            j = (j + state[i] + key[i % len(key)]) % 256
            state[i], state[j] = state[j], state[i]
        i = j = 0
        output = bytearray()
        for char in data:
            i = (i + 1) % 256
            j = (j + state[i]) % 256
            state[i], state[j] = state[j], state[i]
            output.append(char ^ state[(state[i] + state[j]) % 256])
        return bytes(output)


class PlaywrightTool:
    """Browser rendering tool. Imported lazily so requests-only mode stays lightweight."""

    def __init__(self, timeout: int = 30, headless: bool = True) -> None:
        self.timeout = timeout
        self.headless = headless

    def fetch(self, url: str, wait_for: str = "networkidle") -> FetchResult:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return self._fetch_with_system_chromium(url)

        started = time.perf_counter()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout * 1000)
                response = page.goto(url, wait_until="domcontentloaded")
                if wait_for == "networkidle":
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(self.timeout * 1000, 15000))
                    except PlaywrightError:
                        pass
                page.wait_for_timeout(500)
                text = page.content()
                final_url = page.url
                status = response.status if response else 200
                headers = response.headers if response else {}
                context.close()
                browser.close()
        except PlaywrightError as exc:
            raise CrawlToolError(f"Playwright 渲染失败: {exc}") from exc

        return FetchResult(
            url=final_url,
            text=text,
            status_code=status,
            content_type=headers.get("content-type", "text/html"),
            transport="playwright",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            headers=dict(headers),
        )

    def _fetch_with_system_chromium(self, url: str) -> FetchResult:
        executable = next(
            (
                path
                for name in (
                    "chromium",
                    "chromium-browser",
                    "google-chrome",
                    "google-chrome-stable",
                )
                if (path := shutil.which(name))
            ),
            "",
        )
        if not executable:
            raise ToolUnavailable(
                "Playwright 未安装且未找到系统 Chromium；运行 "
                "`pip install playwright && playwright install chromium`"
            )

        started = time.perf_counter()
        command = [
            executable,
            "--headless",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--lang=zh-CN",
            f"--user-agent={DEFAULT_USER_AGENT}",
            "--dump-dom",
            url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout + 10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CrawlToolError(f"系统 Chromium 渲染失败: {exc}") from exc
        html = completed.stdout or ""
        if not html.strip():
            message = (completed.stderr or "").strip()[-500:]
            raise CrawlToolError(
                "系统 Chromium 未返回页面内容"
                + (f": {message}" if message else "")
            )
        return FetchResult(
            url=url,
            text=html,
            status_code=200,
            content_type="text/html; charset=utf-8",
            transport="chromium_cli",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def inspect_network(
        self,
        url: str,
        click_next: bool = False,
        max_response_chars: int = 1_000_000,
    ) -> BrowserInspectionResult:
        """Capture JSON/XHR responses and optionally exercise one Next control."""
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ToolUnavailable(
                "Playwright 未安装；运行 `pip install playwright && playwright install chromium`"
            ) from exc

        observations = []
        clicked = False
        next_selector = ""
        started = time.perf_counter()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                page.set_default_timeout(self.timeout * 1000)

                def capture_response(response) -> None:
                    request = response.request
                    content_type = response.headers.get("content-type", "")
                    if request.resource_type not in ("xhr", "fetch") and "json" not in content_type.lower():
                        return
                    try:
                        body = response.text()
                    except PlaywrightError:
                        body = ""
                    observations.append(NetworkObservation(
                        url=response.url,
                        method=request.method,
                        resource_type=request.resource_type,
                        status_code=response.status,
                        content_type=content_type,
                        response_text=body[:max_response_chars],
                    ))

                page.on("response", capture_response)
                response = page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=min(self.timeout * 1000, 15000))
                except PlaywrightError:
                    pass

                if click_next:
                    selectors = (
                        "a[rel='next']",
                        "button[rel='next']",
                        "a.next",
                        "button.next",
                        "[aria-label='Next']",
                        "[aria-label='下一页']",
                    )
                    locator = None
                    for selector in selectors:
                        candidate = page.locator(selector)
                        if candidate.count() > 0:
                            locator = candidate.first
                            next_selector = selector
                            break
                    if locator is None:
                        text_candidate = page.get_by_text(
                            re.compile(r"^(Next|下一页|下页|›|»)$", re.I),
                            exact=True,
                        )
                        if text_candidate.count() > 0:
                            locator = text_candidate.first
                            next_selector = "text=/^(Next|下一页|下页|›|»)$/i"
                    if locator is not None:
                        locator.click()
                        clicked = True
                        try:
                            page.wait_for_load_state(
                                "networkidle",
                                timeout=min(self.timeout * 1000, 15000),
                            )
                        except PlaywrightError:
                            pass

                page.wait_for_timeout(500)
                text = page.content()
                final_url = page.url
                status = response.status if response else 200
                headers = response.headers if response else {}
                context.close()
                browser.close()
        except PlaywrightError as exc:
            raise CrawlToolError(f"Playwright Network 分析失败: {exc}") from exc

        result = FetchResult(
            url=final_url,
            text=text,
            status_code=status,
            content_type=headers.get("content-type", "text/html"),
            transport="playwright_network",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            headers=dict(headers),
        )
        return BrowserInspectionResult(
            page=result,
            responses=observations,
            clicked_next=clicked,
            next_selector=next_selector,
        )

    def paginate_next(
        self,
        url: str,
        next_selector: str = "",
        max_pages: int = 10_000,
    ):
        """Yield rendered pages while repeatedly clicking a Next control."""
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ToolUnavailable(
                "Playwright 未安装；运行 `pip install playwright && playwright install chromium`"
            ) from exc

        max_pages = max(1, max_pages)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            context = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                locale="zh-CN",
                viewport={"width": 1440, "height": 1000},
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout * 1000)
            try:
                response = page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=min(self.timeout * 1000, 15000),
                    )
                except PlaywrightError:
                    pass

                for _ in range(max_pages):
                    started = time.perf_counter()
                    text = page.content()
                    headers = response.headers if response else {}
                    yield FetchResult(
                        url=page.url,
                        text=text,
                        status_code=response.status if response else 200,
                        content_type=headers.get("content-type", "text/html"),
                        transport="playwright_next",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        headers=dict(headers),
                    )

                    locator = self._find_next_locator(page, next_selector)
                    if locator is None:
                        break
                    try:
                        if not locator.is_visible() or not locator.is_enabled():
                            break
                    except PlaywrightError:
                        break
                    before_url = page.url
                    before_text = text
                    response = None
                    try:
                        with page.expect_navigation(
                            wait_until="domcontentloaded",
                            timeout=min(self.timeout * 1000, 10000),
                        ) as navigation:
                            locator.click()
                        response = navigation.value
                    except PlaywrightError:
                        locator.click()
                    try:
                        page.wait_for_load_state(
                            "networkidle",
                            timeout=min(self.timeout * 1000, 15000),
                        )
                    except PlaywrightError:
                        pass
                    page.wait_for_timeout(300)
                    if page.url == before_url and page.content() == before_text:
                        break
            finally:
                context.close()
                browser.close()

    @staticmethod
    def _find_next_locator(page, selector: str = ""):
        from playwright.sync_api import Error as PlaywrightError

        if selector:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    return locator.first
            except PlaywrightError:
                pass
        selectors = (
            "a[rel='next']",
            "button[rel='next']",
            "a.next",
            "button.next",
            "[aria-label='Next']",
            "[aria-label='下一页']",
        )
        for candidate_selector in selectors:
            locator = page.locator(candidate_selector)
            if locator.count() > 0:
                return locator.first
        text_locator = page.get_by_text(
            re.compile(r"^(Next|下一页|下页|›|»)$", re.I),
            exact=True,
        )
        return text_locator.first if text_locator.count() > 0 else None


class AdaptiveFetchTool:
    """Try requests first, then use a browser only when inspection says it is needed."""

    def __init__(
        self,
        timeout: int = 30,
        retries: int = 3,
        delay: float = 0.35,
        browser_enabled: bool = True,
        headless: bool = True,
    ) -> None:
        self.requests = RequestsTool(timeout=timeout, retries=retries, delay=delay)
        self.browser = PlaywrightTool(timeout=timeout, headless=headless)
        self.browser_enabled = browser_enabled

    def fetch(
        self,
        url: str,
        preferred_transport: str = "auto",
        headers: Optional[Dict[str, str]] = None,
    ) -> FetchResult:
        if preferred_transport == "playwright":
            if not self.browser_enabled:
                raise ToolUnavailable("该站点需要浏览器渲染，但当前禁用了 Playwright")
            return self.browser.fetch(url)

        result = self.requests.fetch(url, headers=headers)
        kind = PageInspector.inspect(result)
        if kind in (PageKind.ANTI_BOT_CHALLENGE, PageKind.JAVASCRIPT_RENDERED):
            if self.browser_enabled:
                return self.browser.fetch(url)
        return result
