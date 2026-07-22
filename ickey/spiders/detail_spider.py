"""
商品详情 Spider

职责：
    遍历 products.json → 逐商品请求详情页 HTML → 解析属性表 → 保存到 product_details.json

支持:
    - 断点续爬（按 SKU checkpoint）
"""

import time
from typing import List, Dict, Any, Set

import requests

from parsers.detail_parser import parse_detail
from storage.json_storage import (
    load_json,
    append_to_json_file,
    load_checkpoint,
    mark_completed,
)
from utils.logger import get_logger

logger = get_logger(__name__)

REQUEST_DELAY = 0.5  # 详情页请求间隔（秒）


class DetailSpider:
    """商品详情采集器"""

    def __init__(self, session: requests.Session):
        self.session = session

    def crawl(
        self,
        products_path: str,
        output_path: str,
        checkpoint_path: str,
    ) -> List[Dict[str, Any]]:
        """
        遍历 products.json，采集每个商品的详情。

        Args:
            products_path: products.json 路径
            output_path: product_details.json 输出路径
            checkpoint_path: 断点文件路径（按 SKU）

        Returns:
            所有商品详情列表
        """
        products = load_json(products_path)
        if not products:
            logger.error("商品列表为空，请先运行 list_spider")
            return []

        completed_skus: Set[str] = load_checkpoint(checkpoint_path)
        if completed_skus:
            logger.info(f"断点恢复: {len(completed_skus)} 个商品已完成")

        all_details: List[Dict[str, Any]] = []
        total = len(products)

        for i, product in enumerate(products):
            sku = product.get("sku", "")
            detail_url = product.get("detail_url", "")

            if not detail_url:
                logger.warning(f"[{i+1}/{total}] 跳过: SKU={sku} (无详情URL)")
                continue

            if sku and sku in completed_skus:
                continue

            logger.info(f"[{i+1}/{total}] 采集详情: SKU={sku}")

            try:
                detail = self._crawl_one_detail(detail_url)

                # 确保 SKU 回填
                if not detail.get("sku") and sku:
                    detail["sku"] = sku

                append_to_json_file(detail, output_path)
                all_details.append(detail)

                if sku:
                    mark_completed(checkpoint_path, sku)
                    completed_skus.add(sku)

            except Exception as e:
                logger.error(f"  ✗ 失败 [{sku}]: {e}")
                continue

            time.sleep(REQUEST_DELAY)

        logger.info(f"详情采集完成: 共 {len(all_details)} 条")
        return all_details

    def _crawl_one_detail(self, url: str) -> Dict[str, Any]:
        """下载并解析单个商品详情"""
        if url.startswith("//"):
            url = "https:" + url

        resp = self.session.get(url, timeout=self.session.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding

        return parse_detail(resp.text)
