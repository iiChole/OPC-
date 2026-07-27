"""
Product Family 发现 Spider

职责：
    从 TI.com 分类导航页发现所有产品家族（destinationId）。

流程：
    1. 从硬编码的一级分类列表出发
    2. 逐一级分类的 overview.html 提取 JSON-LD ItemList → 子分类
    3. 逐子分类访问 products.html → 正则提取 destination-id
    4. 保存到 families.json

数据来源:
    - 子分类: overview.html 中 <script type="application/ld+json"> @type: ItemList
    - destination-id: products.html 中 destination-id="50002" 属性

输出:
    每个 family 一条记录:
    {
        "family_id": 50002,
        "family_name": "Comparators",
        "category": "amplifiers",
        "subcategory": "comparators",
        "products_url": "/product-category/amplifiers/comparators/products.html"
    }
"""

import json
import re
import time
from typing import List, Dict, Optional, Set

import requests

from storage.json_storage import save_json
from utils.logger import get_logger

logger = get_logger(__name__)

REQUEST_DELAY = 0.3

# TI.com 一级分类 slug 列表
# 来源: https://www.ti.com/product-category/overview.html 侧边栏
CATEGORIES = [
    "amplifiers",
    "audio",
    "clocks-timing",
    "data-converters",
    "die-wafer-services",
    "dlp-products",
    "general-purpose-portfolio",
    "interface",
    "isolation",
    "logic-voltage-translation",
    "microcontrollers-processors",
    "motor-drivers",
    "power-management",
    "rf-microwave",
    "sensors",
    "switches-multiplexers",
    "wireless-connectivity",
]


class FamilySpider:
    """产品家族发现器"""

    def __init__(self, session: requests.Session):
        self.session = session

    def run(self, output_path: str) -> List[Dict]:
        """
        发现所有 product family。

        Args:
            output_path: families.json 输出路径

        Returns:
            family 列表
        """
        families: List[Dict] = []
        seen_ids: Set[int] = set()

        for cat_idx, cat in enumerate(CATEGORIES):
            logger.info(f"[{cat_idx+1}/{len(CATEGORIES)}] 分类: {cat}")

            subcategories = self._discover_subcategories(cat)
            logger.info(f"  → {len(subcategories)} 个子分类")

            if subcategories:
                # 有子分类：逐子分类提取 destination-id
                for sub in subcategories:
                    family_id = self._extract_destination_id(sub["products_url"])
                    if family_id is None:
                        logger.warning(
                            f"    ✗ 未找到 destination-id: {sub['name']} ({sub['slug']})"
                        )
                        continue

                    if family_id in seen_ids:
                        continue
                    seen_ids.add(family_id)

                    family = {
                        "family_id": family_id,
                        "family_name": sub["name"],
                        "category": cat,
                        "subcategory": sub["slug"],
                        "products_url": sub["products_url"],
                    }
                    families.append(family)
                    logger.info(f"    ✓ {family_id} = {sub['name']}")
            else:
                # 无子分类：尝试一级分类本身的 products.html
                parent_url = f"/product-category/{cat}/products.html"
                family_id = self._extract_destination_id(parent_url)
                if family_id and family_id not in seen_ids:
                    seen_ids.add(family_id)
                    family = {
                        "family_id": family_id,
                        "family_name": cat.replace("-", " ").title(),
                        "category": cat,
                        "subcategory": "",
                        "products_url": parent_url,
                    }
                    families.append(family)
                    logger.info(f"    ✓ {family_id} = {cat} (一级分类)")

            time.sleep(REQUEST_DELAY)

        save_json(families, output_path)
        logger.info(f"Family 发现完成: 共 {len(families)} 个家族 → {output_path}")
        return families

    def _discover_subcategories(self, category_slug: str) -> List[Dict]:
        """
        从一级分类的 overview.html 提取所有子分类。

        数据来源: JSON-LD (<script type="application/ld+json">)
        {
          "@type": "ItemList",
          "itemListElement": [
            {"name": "Comparators", "item": ".../comparators/overview.html"},
            ...
          ]
        }

        Returns:
            [{"name": "Comparators", "slug": "comparators",
              "products_url": "/product-category/amplifiers/comparators/products.html"}]
        """
        url = f"https://www.ti.com/product-category/{category_slug}/overview.html"
        try:
            resp = self.session.get(url, timeout=30)
            html = resp.text
        except Exception as e:
            logger.debug(f"  获取 {url} 失败: {e}")
            return []

        # 提取所有 JSON-LD script 标签
        ld_json_pattern = re.compile(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            re.DOTALL,
        )
        matches = ld_json_pattern.findall(html)

        subcategories: List[Dict] = []
        seen: Set[str] = set()

        for json_str in matches:
            try:
                data = json.loads(json_str.strip())
            except json.JSONDecodeError:
                continue

            if data.get("@type") != "ItemList":
                continue

            for item in data.get("itemListElement", []):
                name = item.get("name", "")
                item_url = item.get("item", "")

                if not item_url or not name:
                    continue

                # 从 URL 提取 subcategory slug
                # /product-category/{category}/{subcategory}/overview.html
                m = re.search(
                    rf"/product-category/{re.escape(category_slug)}/([^/]+)/overview\.html",
                    item_url,
                )
                if not m:
                    continue

                sub_slug = m.group(1)
                if sub_slug in seen:
                    continue
                seen.add(sub_slug)

                subcategories.append({
                    "name": name,
                    "slug": sub_slug,
                    "products_url": f"/product-category/{category_slug}/{sub_slug}/products.html",
                })

        return subcategories

    def _extract_destination_id(self, products_url: str) -> Optional[int]:
        """
        从 products.html 中提取 destination-id。

        匹配模式: destination-id="50002"
        """
        if not products_url.startswith("http"):
            products_url = "https://www.ti.com" + products_url

        try:
            resp = self.session.get(products_url, timeout=30)
            html = resp.text
        except Exception as e:
            logger.debug(f"  获取 {products_url} 失败: {e}")
            return None

        m = re.search(r'destination-id="(\d+)"', html)
        if m:
            return int(m.group(1))

        return None
