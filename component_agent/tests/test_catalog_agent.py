import json
import tempfile
import unittest
from pathlib import Path

from component_agent.agents.catalog import CatalogAgent
from component_agent.catalog.models import ProductSeed
from component_agent.catalog.pagination import select_traversal_mode
from component_agent.models import FetchResult
from component_agent.planning.models import (
    CategoryCandidate,
    CrawlPlan,
    PaginationPlan,
)


class FakeFetchTool:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url, preferred_transport="auto", headers=None):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError(f"unexpected URL: {url}")
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, FetchResult):
            return value
        return FetchResult(
            url=url,
            text=value,
            status_code=200,
            content_type=(
                "application/json"
                if str(value).lstrip().startswith(("{", "["))
                else "text/html"
            ),
            transport="fake",
            elapsed_ms=1,
        )


class FakeNextPaginator:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate_next(self, url, next_selector="", max_pages=10_000):
        self.calls.append({
            "url": url,
            "next_selector": next_selector,
            "max_pages": max_pages,
        })
        yield from self.pages[:max_pages]


def payload(products, **extra):
    value = {"products": products}
    value.update(extra)
    return json.dumps(value)


def make_plan(categories, pagination):
    return CrawlPlan(
        input_url="https://example.com/",
        start_url="https://example.com/",
        site_key="example",
        website_type="distributor",
        status="ready",
        decision={"recommended_handling": {"target_fields": []}},
        homepage={"url": "https://example.com/"},
        categories=categories,
        api_candidates=[],
        pagination=pagination,
        exploration={},
        execution_policy={"exhaustive": True},
        validation_policy={},
        retry_policy={},
        output_contract={},
        workflow_steps=[],
        issues=[],
        diagnostics=[],
    )


class ProductSeedIdentityTests(unittest.TestCase):
    def test_dedup_priority_is_sku_then_product_id_then_url_hash(self):
        sku_seed = ProductSeed(
            site_key="test",
            sku="ABC-1",
            product_id="P1",
            detail_url="https://example.com/p/1",
        )
        product_id_seed = ProductSeed(
            site_key="test",
            product_id="P2",
            detail_url="https://example.com/p/2",
        )
        url_seed = ProductSeed(
            site_key="test",
            detail_url="https://EXAMPLE.com/p/3?b=2&a=1#fragment",
        )

        self.assertTrue(sku_seed.assign_dedup_identity())
        self.assertEqual(sku_seed.dedup_method, "sku")
        self.assertEqual(sku_seed.dedup_key, "sku:ABC-1")
        self.assertTrue(product_id_seed.assign_dedup_identity())
        self.assertEqual(product_id_seed.dedup_method, "product_id")
        self.assertTrue(url_seed.assign_dedup_identity())
        self.assertEqual(url_seed.dedup_method, "url_hash")
        self.assertTrue(url_seed.dedup_key.startswith("url_sha256:"))


class TraversalTests(unittest.TestCase):
    def _recursive_pages(self):
        return {
            "https://example.com/root": json.dumps({
                "categories": [
                    {"name": "A", "id": "a", "url": "/a"},
                    {"name": "B", "id": "b", "url": "/b"},
                ],
                "products": [],
            }),
            "https://example.com/a": payload([
                {"sku": "A1", "title": "Part A"},
            ]),
            "https://example.com/b": payload([
                {"sku": "B1", "title": "Part B"},
            ]),
        }

    def _plan(self):
        return make_plan(
            [CategoryCandidate("Root", "https://example.com/root", "root")],
            PaginationPlan(
                method="page_parameter",
                parameter="page",
                page_size=10,
                product_list_path="$.products",
            ),
        )

    def test_dfs_visits_last_discovered_child_first(self):
        fetch = FakeFetchTool(self._recursive_pages())
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                traversal_mode="dfs",
            ).run(self._plan())

        self.assertEqual(result.status, "complete")
        self.assertEqual(
            fetch.calls,
            [
                "https://example.com/root",
                "https://example.com/b",
                "https://example.com/a",
            ],
        )
        self.assertEqual(len(result.product_seeds), 2)

    def test_bfs_visits_discovered_children_in_order(self):
        fetch = FakeFetchTool(self._recursive_pages())
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
                traversal_mode="bfs",
            ).run(self._plan())

        self.assertEqual(result.status, "complete")
        self.assertEqual(
            fetch.calls,
            [
                "https://example.com/root",
                "https://example.com/a",
                "https://example.com/b",
            ],
        )

    def test_auto_mode_uses_bfs_for_many_roots_or_parallel_preference(self):
        self.assertEqual(select_traversal_mode("auto", 20), "bfs")
        self.assertEqual(
            select_traversal_mode("auto", 1, prefer_parallel=True),
            "bfs",
        )
        self.assertEqual(select_traversal_mode("auto", 2), "dfs")


class PaginationTests(unittest.TestCase):
    def test_page_pagination_returns_all_products(self):
        fetch = FakeFetchTool({
            "https://example.com/cat": payload(
                [{"sku": "A"}, {"sku": "B"}],
                pageSize=2,
                totalCount=3,
            ),
            "https://example.com/cat?page=2": payload(
                [{"sku": "C"}],
                pageSize=2,
                totalCount=3,
            ),
        })
        plan = make_plan(
            [CategoryCandidate("Cat", "https://example.com/cat", "cat")],
            PaginationPlan(
                method="page_parameter",
                parameter="page",
                page_size=2,
                product_list_path="$.products",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
            ).run(plan)

        self.assertEqual(result.status, "complete")
        self.assertEqual({seed.sku for seed in result.product_seeds}, {"A", "B", "C"})

    def test_offset_pagination_returns_all_products(self):
        fetch = FakeFetchTool({
            "https://example.com/cat": payload(
                [{"sku": "A"}, {"sku": "B"}],
                pageSize=2,
                totalCount=3,
            ),
            "https://example.com/cat?offset=2": payload(
                [{"sku": "C"}],
                pageSize=2,
                totalCount=3,
            ),
        })
        plan = make_plan(
            [CategoryCandidate("Cat", "https://example.com/cat", "cat")],
            PaginationPlan(
                method="offset_parameter",
                parameter="offset",
                page_size=2,
                product_list_path="$.products",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
            ).run(plan)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.product_seeds), 3)

    def test_cursor_pagination_returns_all_products(self):
        fetch = FakeFetchTool({
            "https://example.com/api/products": payload(
                [{"sku": "A"}, {"sku": "B"}],
                next_cursor="abc123",
                pageSize=2,
                totalCount=3,
            ),
            "https://example.com/api/products?cursor=abc123": payload(
                [{"sku": "C"}],
                pageSize=2,
                totalCount=3,
            ),
        })
        plan = make_plan(
            [
                CategoryCandidate(
                    "Cat",
                    "https://example.com/api/products",
                    "cat",
                )
            ],
            PaginationPlan(
                method="cursor",
                parameter="cursor",
                page_size=2,
                product_list_path="$.products",
                next_cursor_path="$.next_cursor",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
            ).run(plan)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.product_seeds), 3)

    def test_next_button_pagination_uses_browser_paginator(self):
        paginator = FakeNextPaginator([
            FetchResult(
                "https://example.com/cat",
                payload(
                    [{"sku": "A"}, {"sku": "B"}],
                    pageSize=2,
                    totalCount=3,
                ),
                200,
                "application/json",
                "fake_browser",
            ),
            FetchResult(
                "https://example.com/cat?page=2",
                payload(
                    [{"sku": "C"}],
                    pageSize=2,
                    totalCount=3,
                ),
                200,
                "application/json",
                "fake_browser",
            ),
        ])
        plan = make_plan(
            [CategoryCandidate("Cat", "https://example.com/cat", "cat")],
            PaginationPlan(
                method="next_click",
                next_selector="button.next",
                page_size=2,
                product_list_path="$.products",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=FakeFetchTool({}),
                next_paginator=paginator,
                run_state_dir=directory,
            ).run(plan)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.product_seeds), 3)
        self.assertEqual(paginator.calls[0]["next_selector"], "button.next")

    def test_next_link_pagination_is_supported_without_browser(self):
        first = """
        <article class="product-card" data-sku="A">
          <a href="/product/a">Part A</a>
        </article>
        <a rel="next" href="/cat?page=2">Next</a>
        """
        second = """
        <article class="product-card" data-sku="B">
          <a href="/product/b">Part B</a>
        </article>
        """
        fetch = FakeFetchTool({
            "https://example.com/cat": first,
            "https://example.com/cat?page=2": second,
        })
        plan = make_plan(
            [CategoryCandidate("Cat", "https://example.com/cat", "cat")],
            PaginationPlan(method="next_link"),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
            ).run(plan)

        self.assertEqual(result.status, "complete")
        self.assertEqual({seed.sku for seed in result.product_seeds}, {"A", "B"})


class PersistenceAndHandoffTests(unittest.TestCase):
    def test_accepts_serialized_crawl_plan_handoff(self):
        plan = make_plan(
            [CategoryCandidate("Cat", "https://example.com/cat", "cat")],
            PaginationPlan(
                method="page_parameter",
                page_size=10,
                product_list_path="$.products",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=FakeFetchTool({
                    "https://example.com/cat": payload([{"sku": "A"}]),
                }),
                run_state_dir=directory,
            ).run(plan.to_dict())

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.product_seeds[0].sku, "A")

    def test_duplicate_products_use_priority_identity_and_are_written_once(self):
        fetch = FakeFetchTool({
            "https://example.com/cat": payload([
                {"sku": "S1", "productId": "P1", "url": "/p/1"},
                {"sku": "S1", "productId": "P2", "url": "/p/2"},
                {"productId": "P3", "url": "/p/3"},
                {"url": "/p/4"},
            ], pageSize=10, totalCount=4),
        })
        plan = make_plan(
            [CategoryCandidate("Cat", "https://example.com/cat", "cat")],
            PaginationPlan(
                method="page_parameter",
                page_size=10,
                product_list_path="$.products",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=fetch,
                run_state_dir=directory,
            ).run(plan)
            lines = (
                Path(directory) / "product_seeds.jsonl"
            ).read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.product_seeds), 3)
        self.assertEqual(result.duplicate_product_count, 1)
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            {seed.dedup_method for seed in result.product_seeds},
            {"sku", "product_id", "url_hash"},
        )

    def test_checkpoint_resume_skips_completed_categories(self):
        plan = make_plan(
            [
                CategoryCandidate("A", "https://example.com/a", "a"),
                CategoryCandidate("B", "https://example.com/b", "b"),
            ],
            PaginationPlan(
                method="page_parameter",
                page_size=10,
                product_list_path="$.products",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            first_fetch = FakeFetchTool({
                "https://example.com/a": payload([{"sku": "A1"}]),
                "https://example.com/b": RuntimeError("temporary failure"),
            })
            first = CatalogAgent(
                fetch_tool=first_fetch,
                run_state_dir=directory,
                traversal_mode="bfs",
            ).run(plan)

            second_fetch = FakeFetchTool({
                "https://example.com/b": payload([{"sku": "B1"}]),
            })
            second = CatalogAgent(
                fetch_tool=second_fetch,
                run_state_dir=directory,
                traversal_mode="bfs",
            ).run(plan)

        self.assertEqual(first.status, "replan_required")
        self.assertEqual(second.status, "complete")
        self.assertEqual(second_fetch.calls, ["https://example.com/b"])
        self.assertEqual({seed.sku for seed in second.product_seeds}, {"A1", "B1"})

    def test_request_failure_returns_crawl_plan_handoff(self):
        plan = make_plan(
            [CategoryCandidate("Cat", "https://example.com/cat", "cat")],
            PaginationPlan(method="page_parameter", page_size=10),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=FakeFetchTool({
                    "https://example.com/cat": RuntimeError("connection timeout"),
                }),
                run_state_dir=directory,
            ).run(plan)

        self.assertEqual(result.status, "replan_required")
        self.assertEqual(result.handoff.target_agent, "CrawlPlanAgent")
        self.assertTrue(result.handoff.available)

    def test_count_anomaly_returns_unavailable_validation_handoff(self):
        plan = make_plan(
            [CategoryCandidate("Cat", "https://example.com/cat", "cat")],
            PaginationPlan(
                method="page_parameter",
                page_size=10,
                product_list_path="$.products",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = CatalogAgent(
                fetch_tool=FakeFetchTool({
                    "https://example.com/cat": payload(
                        [{"sku": "A"}],
                        pageSize=10,
                        totalCount=2,
                    ),
                }),
                run_state_dir=directory,
            ).run(plan)

        self.assertEqual(result.status, "validation_required")
        self.assertEqual(result.handoff.target_agent, "ValidationAgent")
        self.assertFalse(result.handoff.available)


if __name__ == "__main__":
    unittest.main()
