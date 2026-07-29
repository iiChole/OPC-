import unittest

from component_agent.crawl_plan_agent import CrawlPlanAgent
from component_agent.crawl_validation import (
    CrawlExecutionSnapshot,
    CrawlResultValidator,
    CrawlWorkflowGuard,
)
from component_agent.models import (
    BrowserInspectionResult,
    FetchResult,
    NetworkObservation,
)


HOME_WITH_NAV = """
<html><body>
<header><nav>
  <a href="/products/resistors">Resistors</a>
  <a href="/products/capacitors">Capacitors</a>
  <a href="/account">Account</a>
</nav></header>
</body></html>
"""

LIST_WITH_NEXT = """
<html><body>
<article class="product-card"><a href="/product/A">A</a></article>
<article class="product-card"><a href="/product/B">B</a></article>
<a class="next" rel="next" href="/products/resistors?page=2">Next</a>
</body></html>
"""

LIST_PAGE_ONE = """
<html><body>
<article class="product-card"><a href="/product/A">A</a></article>
<article class="product-card"><a href="/product/B">B</a></article>
</body></html>
"""

LIST_PAGE_TWO = """
<html><body>
<article class="product-card"><a href="/product/C">C</a></article>
</body></html>
"""

LIST_WITH_NEXT_BUTTON = """
<html><body>
<article class="product-card"><a href="/product/A">A</a></article>
<button class="next">Next</button>
</body></html>
"""

EMPTY_LIST_PAGE = "<html><body><div class='empty'>No products</div></body></html>"

DYNAMIC_HOME = """
<html><body><div id="root"></div>
<script src="/1.js"></script><script src="/2.js"></script>
<script src="/3.js"></script><script src="/4.js"></script>
<script src="/5.js"></script>
</body></html>
"""


class FakeFetchTool:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url, preferred_transport="auto", headers=None):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"unexpected URL: {url}")
        value = self.pages[url]
        if isinstance(value, FetchResult):
            return value
        return FetchResult(url, value, 200, "text/html", "fake", 1)


class FakeNetworkInspector:
    def __init__(self, inspection):
        self.inspection = inspection
        self.calls = []

    def inspect_network(self, url, click_next=False, max_response_chars=1_000_000):
        self.calls.append({"url": url, "click_next": click_next})
        return self.inspection


class CrawlPlanAgentTests(unittest.TestCase):
    def test_discovers_navigation_categories_and_next_link(self):
        fetch = FakeFetchTool({
            "https://example.com/": HOME_WITH_NAV,
            "https://example.com/products/resistors": LIST_WITH_NEXT,
        })
        plan = CrawlPlanAgent(
            fetch_tool=fetch,
            max_pagination_probes=0,
        ).run("https://example.com/")
        result = plan.to_dict()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            {category["name"] for category in result["categories"]},
            {"Resistors", "Capacitors"},
        )
        self.assertEqual(result["pagination"]["method"], "next_link")
        self.assertEqual(
            result["pagination"]["next_url"],
            "https://example.com/products/resistors?page=2",
        )
        self.assertTrue(result["execution_policy"]["exhaustive"])
        self.assertFalse(result["product_crawl_performed"])

    def test_discovers_category_tree_from_embedded_json(self):
        html = """
        <html><body><script type="application/json">
        {"categories":[
          {"name":"MCU","id":"10","url":"/category/mcu",
           "children":[{"name":"ARM","id":"11","url":"/category/arm"}]}
        ]}
        </script></body></html>
        """
        plan = CrawlPlanAgent(
            fetch_tool=FakeFetchTool({"https://example.com/": html}),
            max_category_probes=0,
            max_pagination_probes=0,
        ).run("https://example.com/")

        names = {category.name for category in plan.categories}
        self.assertEqual(names, {"MCU", "ARM"})
        self.assertTrue(all(category.source == "embedded_json" for category in plan.categories))

    def test_dynamic_site_uses_network_product_api_and_cursor(self):
        api_body = """
        {"products":[
          {"productId":"1","partNumber":"A"},
          {"productId":"2","partNumber":"B"}
        ],"next_cursor":"abc123","pageSize":2,"totalCount":5}
        """
        inspection = BrowserInspectionResult(
            page=FetchResult(
                "https://www.lcsc.com/",
                DYNAMIC_HOME,
                200,
                "text/html",
                "playwright_network",
                2,
            ),
            responses=[
                NetworkObservation(
                    url="https://www.lcsc.com/api/products",
                    resource_type="xhr",
                    status_code=200,
                    content_type="application/json",
                    response_text=api_body,
                )
            ],
        )
        network = FakeNetworkInspector(inspection)
        plan = CrawlPlanAgent(
            fetch_tool=FakeFetchTool({"https://www.lcsc.com/": DYNAMIC_HOME}),
            network_inspector=network,
            max_category_probes=0,
            max_pagination_probes=0,
        ).run("https://www.lcsc.com/")

        product_apis = [api for api in plan.api_candidates if api.purpose == "products"]
        self.assertEqual(len(product_apis), 1)
        self.assertEqual(product_apis[0].url, "https://www.lcsc.com/api/products")
        self.assertEqual(plan.pagination.method, "cursor")
        self.assertEqual(plan.pagination.next_cursor_sample, "abc123")
        self.assertTrue(plan.exploration["network_analysis_performed"])

    def test_probes_page_parameter_and_records_last_page_rule(self):
        fetch = FakeFetchTool({
            "https://example.com/": HOME_WITH_NAV,
            "https://example.com/products/resistors": LIST_PAGE_ONE,
            "https://example.com/products/resistors?page=2": LIST_PAGE_TWO,
        })
        plan = CrawlPlanAgent(
            fetch_tool=fetch,
            max_pagination_probes=2,
        ).run("https://example.com/")

        self.assertEqual(plan.pagination.method, "page_parameter")
        self.assertEqual(plan.pagination.parameter, "page")
        self.assertTrue(plan.pagination.probes[0].accepted)
        self.assertIn(
            "returned_count_less_than_page_size",
            plan.pagination.stop_conditions,
        )
        self.assertTrue(any("小于" in item for item in plan.pagination.evidence))

    def test_falls_back_to_offset_when_page_parameter_does_not_advance(self):
        fetch = FakeFetchTool({
            "https://example.com/": HOME_WITH_NAV,
            "https://example.com/products/resistors": LIST_PAGE_ONE,
            "https://example.com/products/resistors?page=2": LIST_PAGE_ONE,
            "https://example.com/products/resistors?offset=2": LIST_PAGE_TWO,
        })
        plan = CrawlPlanAgent(
            fetch_tool=fetch,
            max_pagination_probes=2,
        ).run("https://example.com/")

        self.assertEqual(plan.pagination.method, "offset_parameter")
        self.assertEqual(plan.pagination.parameter, "offset")
        self.assertFalse(plan.pagination.probes[0].accepted)
        self.assertTrue(plan.pagination.probes[1].accepted)

    def test_empty_page_probe_confirms_page_end_condition(self):
        fetch = FakeFetchTool({
            "https://example.com/": HOME_WITH_NAV,
            "https://example.com/products/resistors": LIST_PAGE_ONE,
            "https://example.com/products/resistors?page=2": EMPTY_LIST_PAGE,
        })
        plan = CrawlPlanAgent(
            fetch_tool=fetch,
            max_pagination_probes=1,
        ).run("https://example.com/")

        self.assertEqual(plan.pagination.method, "page_parameter")
        self.assertEqual(plan.pagination.probes[0].product_count, 0)
        self.assertTrue(plan.pagination.probes[0].accepted)
        self.assertIn("empty_product_list", plan.pagination.stop_conditions)

    def test_detects_next_button_css_selector(self):
        fetch = FakeFetchTool({
            "https://example.com/": HOME_WITH_NAV,
            "https://example.com/products/resistors": LIST_WITH_NEXT_BUTTON,
        })
        plan = CrawlPlanAgent(
            fetch_tool=fetch,
            max_pagination_probes=0,
        ).run("https://example.com/")

        self.assertEqual(plan.pagination.method, "next_click")
        self.assertEqual(plan.pagination.next_selector, "button.next")

    def test_output_contract_publishes_only_categories_and_final_products(self):
        plan = CrawlPlanAgent(
            fetch_tool=FakeFetchTool({"https://example.com/": HOME_WITH_NAV}),
            max_category_probes=0,
            max_pagination_probes=0,
        ).run("https://example.com/")
        contract = plan.output_contract

        self.assertEqual(
            set(contract["published_outputs"]),
            {"categories.json", "products_final.json"},
        )
        self.assertFalse(contract["publish_internal_outputs"])
        self.assertTrue(contract["retain_internal_outputs"])
        self.assertIn("run_state/crawl_plan.json", contract["internal_outputs"])


class CrawlValidationTests(unittest.TestCase):
    def test_valid_result_publishes_final_products(self):
        guard = CrawlWorkflowGuard(
            CrawlResultValidator(("sku", "title", "attributes"))
        )

        def execute(attempt, recovery):
            return CrawlExecutionSnapshot(
                products=[
                    {"sku": "A", "title": "Part A", "attributes": {"Voltage": "5V"}},
                    {"sku": "B", "title": "Part B", "attributes": {"Voltage": "3V"}},
                ],
                discovered_product_count=2,
                reported_product_count=2,
                categories=[{"top_category": "IC"}],
            )

        result = guard.run(execute)
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(len(result.final_output["products_final.json"]), 2)
        self.assertIn("categories.json", result.final_output)

    def test_first_failure_is_diagnosed_and_full_workflow_retried(self):
        guard = CrawlWorkflowGuard(
            CrawlResultValidator(("sku", "title", "attributes"))
        )
        calls = []

        def execute(attempt, recovery):
            calls.append((attempt, recovery))
            if attempt == 1:
                return CrawlExecutionSnapshot(
                    products=[{"sku": "A", "title": "Part A", "attributes": {}}],
                    discovered_product_count=2,
                    reported_product_count=2,
                    failed_tasks=[{
                        "task_id": "detail-B",
                        "message": "timeout",
                        "url": "https://example.com/product/B",
                    }],
                )
            self.assertIsNotNone(recovery)
            self.assertTrue(recovery.retry_full_workflow)
            return CrawlExecutionSnapshot(
                products=[
                    {"sku": "A", "title": "Part A", "attributes": {"V": "5V"}},
                    {"sku": "B", "title": "Part B", "attributes": {"V": "3V"}},
                ],
                discovered_product_count=2,
                reported_product_count=2,
            )

        result = guard.run(execute)
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            result.attempts[0]["recovery"]["action"],
            "diagnose_and_retry_workflow",
        )

    def test_second_failure_pauses_and_preserves_internal_state(self):
        guard = CrawlWorkflowGuard(
            CrawlResultValidator(("sku", "title", "attributes"))
        )

        def execute(attempt, recovery):
            return CrawlExecutionSnapshot(
                products=[{"sku": "A", "title": "Part A", "attributes": {}}],
                discovered_product_count=2,
                reported_product_count=2,
                issues=[{"message": "pagination cursor repeated", "url": "https://example.com/api"}],
            )

        result = guard.run(execute)
        self.assertEqual(result.status, "paused")
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.final_output, {})
        self.assertTrue(result.internal_state["feedback_required"])
        self.assertEqual(
            result.attempts[-1]["recovery"]["action"],
            "pause_task_and_report",
        )
        self.assertEqual(len(result.internal_state["partial_products"]), 1)


if __name__ == "__main__":
    unittest.main()
