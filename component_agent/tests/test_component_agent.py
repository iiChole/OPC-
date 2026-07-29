import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from component_agent.agent import ComponentSearchAgent
from component_agent.intent import analyze_input
from component_agent.models import FetchResult, PageKind
from component_agent.parser import PageInspector, ProductParser
from component_agent.tools import PlaywrightTool


NEXT_HTML = """
<!doctype html><html><head><title>search</title></head><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"searchResult":{"productRecordList":[
  {"productVO":{"productId":"9243","productCode":"C8734",
   "productModel":"STM32F103C8T6","productName":"32-bit MCU",
   "productGradePlateName":"ST(意法半导体)",
   "encapsulationModel":"LQFP-48(7x7)","validStockNumber":123,
   "minBuyNumber":1,"productPriceList":[
      {"startPurchasedNumber":1,"productPrice":7.77}]},
   "paramLinkedMap":{"CPU内核":"ARM Cortex-M3"}}
]}}},"page":"/global"}
</script></body></html>
"""

DETAIL_HTML = """
<!doctype html><html><head><meta property="og:title" content="STM32F103C8T6"></head>
<body><h1>STM32F103C8T6</h1><table>
<tr><td>制造商</td><td>STMicroelectronics</td></tr>
<tr><td>封装</td><td>LQFP-48</td></tr>
</table></body></html>
"""

JSON_LD_HTML = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"STM32F103C8T6",
 "brand":{"@type":"Brand","name":"ST"},
 "offers":{"@type":"Offer","url":"https://example.com/p/1","price":"8.1",
 "priceCurrency":"CNY","availability":"https://schema.org/InStock"}}
</script></head></html>
"""

SZLCSC_CHALLENGE_HTML = """
<html><body>
<script id="renderData" type="application/json">
{"l1":"var arg1='challenge';","l2":"GET"}
</script>
<script>
function getRenderData(){return document.getElementById("renderData").innerHTML}
var renderData=JSON.parse(getRenderData());
</script>
</body></html>
"""


class FakeFetchTool:
    def fetch(self, url, preferred_transport="auto", headers=None):
        html = DETAIL_HTML if "item.szlcsc.com" in url else NEXT_HTML
        return FetchResult(url, html, 200, "text/html", "fake", 1)


class IntentTests(unittest.TestCase):
    def test_explicit_fields(self):
        request = analyze_input("查询: STM32F103C8T6. 需要: 价格, 库存, 封装, 厂商")
        self.assertEqual(request.query, "STM32F103C8T6")
        self.assertEqual(request.fields, ("price", "stock", "package", "manufacturer"))

    def test_unspecified_fields_means_all(self):
        request = analyze_input(query="STM32F103C8T6")
        self.assertIn("attributes", request.fields)
        self.assertIn("price", request.fields)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = ProductParser()

    def test_classifies_next_ssr(self):
        result = FetchResult("https://example.com", NEXT_HTML, 200, "text/html")
        self.assertEqual(PageInspector.inspect(result), PageKind.NEXT_SSR)

    def test_classifies_current_szlcsc_cookie_challenge(self):
        result = FetchResult(
            "https://item.szlcsc.com/9243.html",
            SZLCSC_CHALLENGE_HTML,
            200,
            "text/html",
        )
        self.assertEqual(
            PageInspector.inspect(result),
            PageKind.ANTI_BOT_CHALLENGE,
        )

    def test_parses_next_product(self):
        result = FetchResult("https://so.szlcsc.com/global.html", NEXT_HTML, 200, "text/html")
        products = self.parser.parse_catalog(result, "szlcsc", "立创", "STM32F103C8T6")
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].model, "STM32F103C8T6")
        self.assertEqual(products[0].stock, 123)
        self.assertEqual(products[0].package, "LQFP-48(7x7)")
        self.assertEqual(products[0].price[0]["unit_price"], 7.77)
        self.assertEqual(products[0].detail_url, "https://item.szlcsc.com/9243.html")

    def test_parses_json_ld_offer(self):
        result = FetchResult("https://example.com/search", JSON_LD_HTML, 200, "text/html")
        products = self.parser.parse_catalog(result, "example", "Example", "STM32F103C8T6")
        self.assertEqual(products[0].manufacturer, "ST")
        self.assertEqual(products[0].stock, "in_stock")
        self.assertEqual(products[0].price[0]["currency"], "CNY")

    def test_detail_attribute_fallback(self):
        result = FetchResult("https://example.com/p/1", DETAIL_HTML, 200, "text/html")
        product = self.parser.parse_detail(result, "example", "Example")
        self.assertEqual(product.manufacturer, "STMicroelectronics")
        self.assertEqual(product.package, "LQFP-48")


class BrowserFallbackTests(unittest.TestCase):
    def test_system_chromium_fallback_returns_rendered_html(self):
        completed = SimpleNamespace(
            stdout="<html><body><h1>Rendered</h1></body></html>",
            stderr="",
            returncode=0,
        )
        with patch(
            "component_agent.tools.shutil.which",
            side_effect=lambda name: (
                "/usr/bin/chromium" if name == "chromium" else None
            ),
        ), patch(
            "component_agent.tools.subprocess.run",
            return_value=completed,
        ) as run:
            result = PlaywrightTool(timeout=5)._fetch_with_system_chromium(
                "https://example.com/product"
            )

        self.assertEqual(result.transport, "chromium_cli")
        self.assertIn("Rendered", result.text)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/chromium")
        self.assertIn("--dump-dom", command)


class AgentTests(unittest.TestCase):
    def test_catalog_detail_merge_and_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = ComponentSearchAgent(
                sites=("szlcsc",), output_dir=temp_dir,
                fetch_tool=FakeFetchTool(), max_results_per_site=2,
            )
            report = agent.run(analyze_input(query="STM32F103C8T6"))
            self.assertEqual(report.status, "partial")
            self.assertEqual(len(report.products), 1)
            self.assertEqual(report.products[0].manufacturer, "STMicroelectronics")
            self.assertEqual(report.products[0].stock, 123)
            output = Path(report.output_dir)
            self.assertTrue((output / "products.jsonl").exists())
            self.assertTrue((output / "product_details.jsonl").exists())
            saved = json.loads((output / "products_final.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["result_count"], 1)


if __name__ == "__main__":
    unittest.main()
