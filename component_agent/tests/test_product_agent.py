import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from component_agent.agents.product import ProductAgent
from component_agent.catalog.models import ProductSeed
from component_agent.models import FetchResult
from component_agent.planning.models import CrawlPlan, PaginationPlan
from component_agent.product.checkpoint import ProductDetailJournal


def make_plan(detail_fetch=None, required_fields=None):
    return CrawlPlan(
        input_url="https://example.com/",
        start_url="https://example.com/",
        site_key="example",
        website_type="distributor",
        status="ready",
        decision={
            "recognized_site": {"name": "Example Parts"},
            "recommended_handling": {
                "target_fields": list(required_fields or []),
            },
        },
        homepage={"url": "https://example.com/"},
        categories=[],
        api_candidates=[],
        pagination=PaginationPlan(),
        exploration={},
        execution_policy={
            "exhaustive": True,
            "detail_fetch": {
                "preferred_transport": "auto",
                "max_concurrency": 4,
                "request_interval_seconds": 0,
                **(detail_fetch or {}),
            },
        },
        validation_policy={
            "required_fields": list(required_fields or []),
        },
        retry_policy={},
        output_contract={},
        workflow_steps=[],
        issues=[],
        diagnostics=[],
    )


def make_seed(
    sku,
    detail_url=None,
    title="",
    source_url="https://example.com/category",
    **values,
):
    seed = ProductSeed(
        site_key="example",
        sku=sku,
        title=title,
        detail_url=detail_url or f"https://example.com/product/{sku}",
        source_url=source_url,
        **values,
    )
    seed.assign_dedup_identity()
    return seed


def fetch_result(url, value, result_url=None, status_code=200):
    return FetchResult(
        url=result_url or url,
        text=value,
        status_code=status_code,
        content_type=(
            "application/json"
            if str(value).lstrip().startswith(("{", "["))
            else "text/html"
        ),
        transport="fake",
        elapsed_ms=1,
    )


class FakeFetchTool:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self._lock = threading.Lock()

    def fetch(self, url, preferred_transport="auto", headers=None):
        with self._lock:
            self.calls.append({
                "url": url,
                "preferred_transport": preferred_transport,
                "headers": dict(headers or {}),
            })
            value = self.responses[url]
            if isinstance(value, list):
                value = value.pop(0)
        if callable(value):
            value = value(url)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, FetchResult):
            return value
        return fetch_result(url, value)


class ProductParsingTests(unittest.TestCase):
    def test_html_extracts_normalized_fields_and_table_attributes(self):
        url = "https://example.com/product/C1"
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Product",
            "sku": "C1",
            "mpn": "ABC-123",
            "name": "ABC-123 precision amplifier",
            "brand": {"name": "Analog Devices"},
            "package": "SOIC-8",
            "description": "Low-noise amplifier",
            "image": "/images/c1.jpg",
            "offers": {
              "price": "1.25",
              "priceCurrency": "USD",
              "availability": "https://schema.org/InStock",
              "inventoryLevel": {"value": 321}
            },
            "subjectOf": {
              "@type": "CreativeWork",
              "name": "ABC-123 Datasheet",
              "url": "/datasheets/abc-123.pdf"
            }
          }
          </script>
        </head><body>
          <h1>ABC-123 precision amplifier</h1>
          <table>
            <tr><th>Supply Voltage</th><td>5 V</td></tr>
            <tr><th>Bandwidth</th><td>10 MHz</td></tr>
          </table>
          <a href="/datasheets/abc-123.pdf">Datasheet</a>
        </body></html>
        """
        seed = make_seed(
            "C1",
            url,
            title="catalog title",
            attributes={"Catalog field": "kept"},
            extra={"catalog_marker": "kept"},
        )
        fetch = FakeFetchTool({
            url: fetch_result(
                url,
                html,
                result_url="https://example.com/product/C1?resolved=1",
            )
        })

        with tempfile.TemporaryDirectory() as directory:
            result = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            ).run([seed], make_plan())

        product = result.products[0]
        self.assertEqual(result.status, "complete")
        self.assertEqual(product.part_number, "ABC-123")
        self.assertEqual(product.title, "ABC-123 precision amplifier")
        self.assertEqual(product.manufacturer, "Analog Devices")
        self.assertEqual(product.package, "SOIC-8")
        self.assertEqual(product.stock, 321)
        self.assertEqual(product.price[0]["unit_price"], "1.25")
        self.assertEqual(product.attributes["Catalog field"], "kept")
        self.assertEqual(product.attributes["Supply Voltage"], "5 V")
        self.assertEqual(product.attributes["Bandwidth"], "10 MHz")
        self.assertEqual(
            product.datasheet_url,
            "https://example.com/datasheets/abc-123.pdf",
        )
        self.assertEqual(
            product.source_url,
            "https://example.com/product/C1?resolved=1",
        )
        self.assertEqual(
            product.catalog_source_url,
            "https://example.com/category",
        )
        self.assertEqual(product.extra["catalog_marker"], "kept")

    def test_json_normalizes_fields_and_preserves_site_specific_fields(self):
        url = "https://example.com/api/product/C2"
        payload = json.dumps({
            "data": {
                "product": {
                    "sku": "C2",
                    "productId": "P-2",
                    "partNumber": "XYZ-2",
                    "title": "XYZ-2 regulator",
                    "manufacturerName": "Example Semi",
                    "packageType": "QFN-16",
                    "stockQuantity": 42,
                    "unitPrice": 0.85,
                    "minimumOrderQuantity": 5,
                    "specifications": [
                        {"name": "Input voltage", "value": "12 V"},
                        {"name": "Output current", "value": "2 A"},
                    ],
                    "lifecycleState": "Active",
                    "warehouseCode": "SZ-01",
                }
            }
        })
        fetch = FakeFetchTool({url: payload})

        with tempfile.TemporaryDirectory() as directory:
            result = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            ).run([make_seed("C2", url)], make_plan())

        product = result.products[0]
        self.assertEqual(product.product_id, "P-2")
        self.assertEqual(product.part_number, "XYZ-2")
        self.assertEqual(product.manufacturer, "Example Semi")
        self.assertEqual(product.package, "QFN-16")
        self.assertEqual(product.stock, 42)
        self.assertEqual(product.moq, 5)
        self.assertEqual(product.price[0]["unit_price"], 0.85)
        self.assertEqual(product.attributes["Input voltage"], "12 V")
        self.assertEqual(
            product.extra["site_fields"]["lifecycleState"],
            "Active",
        )
        self.assertEqual(
            product.extra["site_fields"]["warehouseCode"],
            "SZ-01",
        )

    def test_sparse_json_product_is_kept_with_empty_dict_defaults(self):
        url = "https://example.com/product/sparse"
        fetch = FakeFetchTool({
            url: json.dumps({
                "title": "Sparse product",
                "websiteOnlyFlag": True,
            })
        })

        with tempfile.TemporaryDirectory() as directory:
            result = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            ).run([make_seed("SPARSE", url)], make_plan())

        product = result.products[0]
        self.assertEqual(result.status, "complete")
        self.assertEqual(product.title, "Sparse product")
        self.assertEqual(product.attributes, {})
        self.assertIsInstance(product.extra, dict)
        self.assertTrue(product.extra["site_fields"]["websiteOnlyFlag"])

    def test_missing_required_fields_create_issue_but_do_not_discard_product(self):
        url = "https://example.com/product/C3"
        fetch = FakeFetchTool({url: "<html><body><h1>C3</h1></body></html>"})

        with tempfile.TemporaryDirectory() as directory:
            result = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            ).run(
                [make_seed("C3", url)],
                make_plan(required_fields=["manufacturer", "package"]),
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.products), 1)
        self.assertEqual(result.products[0].title, "C3")
        self.assertEqual(
            result.products[0].missing_fields,
            ["manufacturer", "package"],
        )
        self.assertEqual(result.issues[0].code, "detail_fields_missing")

    def test_missing_detail_url_is_saved_as_failed_fallback(self):
        seed = {
            "site_key": "example",
            "sku": "NO-URL",
            "title": "Catalog-only product",
            "detail_url": "",
            "attributes": "invalid",
            "extra": "invalid",
        }
        fetch = FakeFetchTool({})

        with tempfile.TemporaryDirectory() as directory:
            result = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            ).run([seed], make_plan())
            detail_path = Path(result.detail_output_path)
            records = [
                json.loads(line)
                for line in detail_path.read_text(encoding="utf-8").splitlines()
            ]

        product = result.products[0]
        self.assertEqual(result.status, "partial")
        self.assertEqual(product.fetch_status, "failed")
        self.assertEqual(product.title, "Catalog-only product")
        self.assertEqual(product.attributes, {})
        self.assertEqual(product.extra, {})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["fetch_status"], "failed")
        self.assertEqual(fetch.calls, [])


class ProductExecutionTests(unittest.TestCase):
    def test_serialized_handoff_and_crawl_plan_transport_are_supported(self):
        url = "https://example.com/product/C4"
        seed = make_seed(
            "C4",
            url,
            extra={
                "detail_request": {
                    "preferred_transport": "browser",
                    "headers": {"X-Detail": "yes"},
                }
            },
        )
        fetch = FakeFetchTool({
            url: json.dumps({"sku": "C4", "title": "Product C4"})
        })
        plan = make_plan({
            "preferred_transport": "requests",
            "headers": {"X-Plan": "yes"},
        })

        with tempfile.TemporaryDirectory() as directory:
            result = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            ).run(
                {"product_seeds": [seed.to_dict()]},
                plan.to_dict(),
            )

        self.assertEqual(result.products[0].title, "Product C4")
        self.assertEqual(fetch.calls[0]["preferred_transport"], "playwright")
        self.assertEqual(fetch.calls[0]["headers"]["X-Plan"], "yes")
        self.assertEqual(fetch.calls[0]["headers"]["X-Detail"], "yes")
        self.assertEqual(
            fetch.calls[0]["headers"]["Referer"],
            "https://example.com/category",
        )

    def test_concurrency_is_bounded_and_parallel(self):
        class ConcurrentFetchTool:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def fetch(self, url, preferred_transport="auto", headers=None):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.04)
                    sku = url.rsplit("/", 1)[-1]
                    return fetch_result(
                        url,
                        json.dumps({"sku": sku, "title": f"Product {sku}"}),
                    )
                finally:
                    with self.lock:
                        self.active -= 1

        fetch = ConcurrentFetchTool()
        seeds = [make_seed(f"C{index}") for index in range(8)]
        plan = make_plan({"max_concurrency": 3})

        with tempfile.TemporaryDirectory() as directory:
            result = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                max_concurrency=5,
                request_interval_seconds=0,
            ).run(seeds, plan)

        self.assertEqual(result.max_concurrency, 3)
        self.assertGreater(fetch.max_active, 1)
        self.assertLessEqual(fetch.max_active, 3)
        self.assertEqual(result.completed_count, 8)

    def test_request_start_interval_is_enforced_globally(self):
        class TimedFetchTool:
            def __init__(self):
                self.started = []
                self.lock = threading.Lock()

            def fetch(self, url, preferred_transport="auto", headers=None):
                with self.lock:
                    self.started.append(time.monotonic())
                sku = url.rsplit("/", 1)[-1]
                return fetch_result(
                    url,
                    json.dumps({"sku": sku, "title": sku}),
                )

        fetch = TimedFetchTool()
        plan = make_plan({
            "max_concurrency": 3,
            "request_interval_seconds": 0.03,
        })

        with tempfile.TemporaryDirectory() as directory:
            ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            ).run([make_seed(f"T{index}") for index in range(3)], plan)

        starts = sorted(fetch.started)
        gaps = [right - left for left, right in zip(starts, starts[1:])]
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(gap >= 0.02 for gap in gaps), gaps)

    def test_output_order_matches_product_seed_order(self):
        delays = {"A": 0.06, "B": 0.01, "C": 0.03}

        def delayed(url):
            sku = url.rsplit("/", 1)[-1]
            time.sleep(delays[sku])
            return fetch_result(
                url,
                json.dumps({"sku": sku, "title": f"Product {sku}"}),
            )

        fetch = FakeFetchTool({
            f"https://example.com/product/{sku}": delayed
            for sku in ("A", "B", "C")
        })

        with tempfile.TemporaryDirectory() as directory:
            result = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                max_concurrency=3,
                request_interval_seconds=0,
            ).run(
                [make_seed("A"), make_seed("B"), make_seed("C")],
                make_plan({"max_concurrency": 3}),
            )

        self.assertEqual([product.sku for product in result.products], ["A", "B", "C"])

    def test_checkpoint_resume_skips_successful_products(self):
        url = "https://example.com/product/RESUME"
        fetch = FakeFetchTool({
            url: json.dumps({"sku": "RESUME", "title": "Resume product"})
        })
        seed = make_seed("RESUME", url)
        plan = make_plan()

        with tempfile.TemporaryDirectory() as directory:
            agent = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            )
            first = agent.run([seed], plan)
            second = agent.run([seed], plan)

        self.assertEqual(first.completed_count, 1)
        self.assertEqual(second.completed_count, 1)
        self.assertEqual(second.skipped_count, 1)
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual(second.products[0].title, "Resume product")

    def test_failed_product_is_retried_and_latest_result_has_no_duplicate(self):
        url = "https://example.com/product/RETRY"
        fetch = FakeFetchTool({
            url: [
                RuntimeError("temporary failure"),
                json.dumps({"sku": "RETRY", "title": "Recovered product"}),
            ]
        })
        seed = make_seed("RETRY", url)
        plan = make_plan()

        with tempfile.TemporaryDirectory() as directory:
            agent = ProductAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                request_interval_seconds=0,
            )
            first = agent.run([seed], plan)
            second = agent.run([seed], plan)
            journal = ProductDetailJournal(second.detail_output_path)
            latest = journal.load_latest("example")
            line_count = len(
                Path(second.detail_output_path)
                .read_text(encoding="utf-8")
                .splitlines()
            )

        self.assertEqual(first.status, "partial")
        self.assertEqual(second.status, "complete")
        self.assertEqual(len(second.products), 1)
        self.assertEqual(second.products[0].title, "Recovered product")
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[seed.dedup_key].fetch_status, "complete")
        self.assertEqual(line_count, 2)
        self.assertEqual(len(fetch.calls), 2)


if __name__ == "__main__":
    unittest.main()
