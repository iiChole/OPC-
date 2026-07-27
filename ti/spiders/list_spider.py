"""
产品列表 Spider

职责：
    遍历 families.json → 逐 family 调 parametric API → 提取产品列表 → 保存到 products.jsonl

API:
    GET /selectionmodel/api/gpn/result-list
        ?destinationId={id}
        &destinationType=GPT
        &mode=parametric
        &locale=en-US

    一次性返回该 family 的所有产品 (无需翻页)。

支持:
    - 断点续爬（按 family_id checkpoint）
    - JSONL 流式写入
"""

import time
from typing import List, Dict, Any, Set

import requests

from storage.json_storage import (
    load_json,
    JSONLWriter,
    load_checkpoint,
    mark_completed,
)
from utils.headers import get_headers
from utils.logger import get_logger

logger = get_logger(__name__)

API_BASE = "https://www.ti.com/selectionmodel/api/gpn/result-list"
REQUEST_DELAY = 0.3


class ListSpider:
    """产品列表采集器"""

    def __init__(self, session: requests.Session):
        self.session = session

    def crawl(
        self,
        families_path: str,
        output_path: str,
        checkpoint_path: str,
    ) -> List[Dict[str, Any]]:
        """
        遍历 families.json，采集每个 family 的产品列表。

        Args:
            families_path: families.json 路径
            output_path: products.jsonl 输出路径
            checkpoint_path: 断点文件路径（按 family_id）

        Returns:
            所有产品列表
        """
        families = load_json(families_path)
        if not families:
            logger.error("Family 列表为空，请先运行 family_spider")
            return []

        completed_families: Set[str] = load_checkpoint(checkpoint_path)
        if completed_families:
            logger.info(f"断点恢复: {len(completed_families)} 个 family 已完成")

        writer = JSONLWriter(output_path, flush_size=50, flush_interval=10)
        all_products: List[Dict[str, Any]] = []
        total_families = len(families)

        try:
            for i, family in enumerate(families):
                family_id = str(family.get("family_id", ""))
                family_name = family.get("family_name", "")

                if not family_id:
                    continue

                if family_id in completed_families:
                    continue

                logger.info(
                    f"[{i+1}/{total_families}] family {family_id} ({family_name})"
                )

                try:
                    products = self._fetch_family_products(
                        family_id, family
                    )

                    for p in products:
                        writer.append(p)
                        all_products.append(p)

                    mark_completed(checkpoint_path, family_id)
                    completed_families.add(family_id)
                    logger.info(f"  ✓ {len(products)} 个产品")

                except Exception as e:
                    logger.error(f"  ✗ 失败 [{family_id}]: {e}")
                    continue

                time.sleep(REQUEST_DELAY)
        finally:
            writer.close()

        logger.info(f"产品列表采集完成: 共 {len(all_products)} 个产品")
        return all_products

    def _fetch_family_products(
        self, family_id: str, family: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        调用 parametric API 获取 family 的所有产品。

        Args:
            family_id: destinationId
            family: family 元数据

        Returns:
            产品基础信息列表
        """
        params = {
            "destinationId": family_id,
            "destinationType": "GPT",
            "mode": "parametric",
            "locale": "en-US",
        }

        headers = get_headers(
            {"Referer": "https://www.ti.com/product-category/products.html"}
        )

        resp = self.session.get(API_BASE, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])

        products: List[Dict[str, Any]] = []
        for r in results:
            gpn = r.get("genericPartNumber", "")
            loc = r.get("localization", {}).get("en-US", {})
            status = r.get("productStatus", {})

            product = {
                "sku": gpn,
                "title": loc.get("title", ""),
                "description": loc.get("productDesc", ""),
                "family_id": family.get("family_id"),
                "family_name": family.get("family_name"),
                "category": family.get("category"),
                "subcategory": family.get("subcategory"),
                "status": status.get("name", ""),
                "status_id": status.get("id"),
                "opn_list": r.get("opnList", []),
                "datasheet_pdf": r.get("datasheetPDF", False),
                "datasheet_html": r.get("datasheetHTML", False),
                "rating": r.get("rating", {}),
                "functional_safety": r.get("functionalSafety", {}),
            }
            products.append(product)

        return products
