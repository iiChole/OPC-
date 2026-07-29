import tempfile
import unittest
from pathlib import Path

from component_agent.catalog.models import CategoryTask
from component_agent.models import FetchResult
from component_agent.orchestration.full_site import FullSiteCrawlCoordinator
from component_agent.orchestration.robots import RobotsAwareFetchTool, RobotsPolicy
from component_agent.sites.icgoo import ICGooCatalogParser, ICGooSiteAdapter


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.icgoo.net/catalog/100/</loc></url>
  <url><loc>https://www.icgoo.net/about/</loc></url>
</urlset>
"""

CATALOG_HTML = """
<html><head><title>显示驱动器</title></head><body>
<div>当前“显示驱动器”共 2 条标准型号。</div>
<table class="main_table">
  <tr><th>序号</th><th>型号</th><th>分类</th><th>编码</th><th>创建时间</th></tr>
  <tr><td>1</td><td><a href="/search/ABC-1/1/">ABC-1</a></td><td>显示驱动器</td><td>P-1</td><td>2026-01-01</td></tr>
  <tr><td>2</td><td><a href="/search/ABC-2/1/">ABC-2</a></td><td>显示驱动器</td><td>P-2</td><td>2026-01-02</td></tr>
</table>
</body></html>
"""


class FakeFetchTool:
    browser = None

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url, preferred_transport="auto", headers=None):
        self.calls.append(url)
        return self.responses[url]


class FakeRobotsChecker:
    def check(self, target_url):
        return RobotsPolicy(
            robots_url="https://www.icgoo.net/robots.txt",
            target_url=target_url,
            user_agent="MultiAgentCrawler/1.0",
            allowed=True,
            status_code=200,
            reason="allowed",
            sitemap_urls=["https://www.icgoo.net/sitemap.xml"],
            disallow_rules=["/search/", "/partno-detail?*"],
        )


class ICGooParserTests(unittest.TestCase):
    def test_table_rows_become_individual_catalog_only_seeds(self):
        url = "https://www.icgoo.net/catalog/100/"
        page = ICGooCatalogParser().parse(
            FetchResult(url=url, text=CATALOG_HTML, status_code=200),
            CategoryTask(name="catalog_100", url=url, identifier="100"),
            "icgoo",
        )

        self.assertEqual(page.raw_product_count, 2)
        self.assertEqual(page.total_count, 2)
        self.assertEqual([item.sku for item in page.products], ["ABC-1", "ABC-2"])
        self.assertTrue(all(not item.detail_url for item in page.products))
        self.assertTrue(all(item.extra["catalog_only"] for item in page.products))
        self.assertIn("/search/ABC-1/1/", page.products[0].extra["robots_disallowed_detail_url"])


class RobotsFetchTests(unittest.TestCase):
    def test_disallowed_url_is_blocked_before_delegate(self):
        delegate = FakeFetchTool({})
        policy = FakeRobotsChecker().check("https://www.icgoo.net/")
        policy.allows = lambda url: "/search/" not in url
        tool = RobotsAwareFetchTool(delegate, policy)

        with self.assertRaisesRegex(RuntimeError, "robots.txt"):
            tool.fetch("https://www.icgoo.net/search/ABC-1/1/")
        self.assertEqual(delegate.calls, [])


class FullSiteCoordinatorTests(unittest.TestCase):
    def test_icgoo_runs_end_to_end_without_manual_agent_scheduling(self):
        sitemap_url = "https://www.icgoo.net/sitemap.xml"
        catalog_url = "https://www.icgoo.net/catalog/100/"
        fetch = FakeFetchTool({
            sitemap_url: FetchResult(
                url=sitemap_url,
                text=SITEMAP,
                status_code=200,
                content_type="text/xml",
                transport="fake",
            ),
            catalog_url: FetchResult(
                url=catalog_url,
                text=CATALOG_HTML,
                status_code=200,
                content_type="text/html",
                transport="fake",
            ),
        })

        with tempfile.TemporaryDirectory() as directory:
            result = FullSiteCrawlCoordinator(
                run_state_root=directory,
                fetch_tool=fetch,
                robots_checker=FakeRobotsChecker(),
                adapter=ICGooSiteAdapter(),
                max_categories=1,
                max_workflow_attempts=1,
            ).run("https://www.icgoo.net/")

            self.assertEqual(result.status, "complete")
            self.assertEqual(fetch.calls, [sitemap_url, catalog_url])
            self.assertFalse(any("/search/" in url for url in fetch.calls))
            self.assertEqual(result.attempts[0]["catalog"]["product_seed_count"], 2)
            self.assertEqual(result.attempts[0]["product"]["completed_count"], 2)
            self.assertTrue(result.attempts[0]["validation"]["valid"])
            run_dir = Path(result.run_state_dir)
            self.assertTrue((run_dir / "crawl_plan.json").exists())
            self.assertTrue((run_dir / "products_final.json").exists())
            self.assertTrue((run_dir / "crawl_summary.json").exists())


if __name__ == "__main__":
    unittest.main()
