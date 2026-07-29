import io
import json
import sys
import unittest
from unittest.mock import patch

from component_agent.__main__ import main
from component_agent.decision_agent import WebsiteDecisionAgent, WebsiteType


class WebsiteDecisionAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = WebsiteDecisionAgent()

    def test_classifies_all_five_website_types(self):
        cases = {
            "https://www.digikey.com/en/products": WebsiteType.DISTRIBUTOR,
            "https://www.ti.com/product/TPS5430": WebsiteType.MANUFACTURER,
            "https://item.szlcsc.com/123.html": WebsiteType.MARKETPLACE_ECOMMERCE,
            "https://www.hqew.com/": WebsiteType.SUPPLIER_MARKETPLACE,
            "https://bom.ai/": WebsiteType.BOM_INTELLIGENCE,
        }
        for site, expected_type in cases.items():
            with self.subTest(site=site):
                decision = self.agent.decide(site)
                self.assertEqual(decision.website_type, expected_type)
                self.assertEqual(decision.matched_by, "domain")

    def test_all_representative_sites_are_registered(self):
        cases = {
            "digikey.com": WebsiteType.DISTRIBUTOR,
            "mouser.com": WebsiteType.DISTRIBUTOR,
            "arrow.com": WebsiteType.DISTRIBUTOR,
            "futureelectronics.com": WebsiteType.DISTRIBUTOR,
            "element14.com": WebsiteType.DISTRIBUTOR,
            "analog.com": WebsiteType.MANUFACTURER,
            "ti.com": WebsiteType.MANUFACTURER,
            "lcsc.com": WebsiteType.MARKETPLACE_ECOMMERCE,
            "ickey.cn": WebsiteType.MARKETPLACE_ECOMMERCE,
            "ic.net.cn": WebsiteType.SUPPLIER_MARKETPLACE,
            "hqew.com": WebsiteType.SUPPLIER_MARKETPLACE,
            "正能量电子网": WebsiteType.SUPPLIER_MARKETPLACE,
            "bom.ai": WebsiteType.BOM_INTELLIGENCE,
        }
        for site, expected_type in cases.items():
            with self.subTest(site=site):
                self.assertEqual(self.agent.decide(site).website_type, expected_type)

    def test_alias_supports_sites_without_a_confirmed_domain_rule(self):
        decision = self.agent.decide("正能量电子网")
        self.assertEqual(decision.website_type, WebsiteType.SUPPLIER_MARKETPLACE)
        self.assertEqual(decision.matched_by, "alias")

    def test_manufacturer_does_not_request_price_or_stock(self):
        result = self.agent.decide("Analog Devices").to_dict()
        handling = result["recommended_handling"]
        self.assertEqual(result["classification"]["type"], "manufacturer")
        self.assertEqual(handling["intentionally_omitted_fields"], ["price", "stock"])
        self.assertNotIn("price", handling["target_fields"])

    def test_decision_result_explicitly_disables_crawling(self):
        result = self.agent.decide("https://www.mouser.com/").to_dict()
        self.assertTrue(result["decision_only"])
        self.assertFalse(result["crawl_performed"])
        self.assertFalse(result["structure_analysis"]["page_inspected"])
        self.assertFalse(result["recommended_handling"]["execute"])

    def test_supported_product_sites_route_to_full_catalog_without_comparison(self):
        distributor = self.agent.decide("digikey.com").to_dict()
        supplier_marketplace = self.agent.decide("hqew.com").to_dict()

        self.assertEqual(
            distributor["recommended_handling"]["route_to"],
            "full_catalog_workflow",
        )
        self.assertEqual(
            supplier_marketplace["recommended_handling"]["route_to"],
            "full_catalog_workflow",
        )
        self.assertTrue(any(
            "不执行跨供应商" in step
            for step in supplier_marketplace["recommended_handling"]["strategy"]
        ))

    def test_unknown_domain_is_not_forced_into_a_category(self):
        result = self.agent.decide("https://ti.com.example.org/products").to_dict()
        self.assertEqual(result["status"], "unclassified")
        self.assertEqual(result["classification"]["type"], "unknown")
        self.assertEqual(result["debug"]["matched_by"], "none")

    def test_cli_decision_mode_never_constructs_crawler(self):
        stdout = io.StringIO()
        sys.modules.pop("component_agent.agent", None)
        with patch.object(sys, "argv", ["component_agent", "--decide-site", "ti.com"]):
            with patch("sys.stdout", stdout):
                exit_code = main()

        self.assertNotIn("component_agent.agent", sys.modules)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["classification"]["type"], "manufacturer")
        self.assertFalse(payload["crawl_performed"])


if __name__ == "__main__":
    unittest.main()
