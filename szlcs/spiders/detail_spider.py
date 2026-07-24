"""
商品详情 Spider

职责：
    遍历 products.json → 逐商品请求详情页 HTML → 解析属性表 → 保存到 product_details.json

注意:
    item.szlcsc.com 使用阿里云 WAF 保护，可能无法直接通过 requests 访问。
    当详情页无法访问时，将使用列表页中已嵌入的属性数据（paramLinkedMap）作为回退。

    如果设置了 use_playwright=True，将使用 Playwright 浏览器绕过 WAF。

支持:
    - 断点续爬（按 SKU checkpoint）
"""

import time
from typing import List, Dict, Any, Set, Optional

import requests

from parsers.detail_parser import parse_detail
from storage.json_storage import (
    load_json,
    load_jsonl,
    JSONLWriter,
    load_checkpoint,
    mark_completed,
)
from utils.logger import get_logger

logger = get_logger(__name__)

REQUEST_DELAY = 0.5  # 详情页请求间隔（秒）


class DetailSpider:
    """商品详情采集器"""

    def __init__(self, session: requests.Session, use_playwright: bool = False):
        """
        Args:
            session: 共享的 requests.Session
            use_playwright: 是否使用 Playwright 浏览器绕过 WAF
        """
        self.session = session
        self.use_playwright = use_playwright
        self._browser = None
        self._context = None

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
        products = load_jsonl(products_path) if products_path.endswith('.jsonl') else load_json(products_path)
        if not products:
            logger.error("商品列表为空，请先运行 list_spider")
            return []

        completed_skus: Set[str] = load_checkpoint(checkpoint_path)
        if completed_skus:
            logger.info(f"断点恢复: {len(completed_skus)} 个商品已完成")

        # 创建 JSONL 流式写入器
        writer = JSONLWriter(output_path, flush_size=50, flush_interval=10)
        all_details: List[Dict[str, Any]] = []
        total = len(products)

        try:
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
                    detail = self._crawl_one_detail(detail_url, product)

                    # 确保 SKU 回填
                    if not detail.get("sku") and sku:
                        detail["sku"] = sku

                    writer.append(detail)
                    all_details.append(detail)

                    if sku:
                        mark_completed(checkpoint_path, sku)
                        completed_skus.add(sku)

                except Exception as e:
                    logger.error(f"  ✗ 失败 [{sku}]: {e}")
                    continue

                time.sleep(REQUEST_DELAY)
        finally:
            writer.close()

        logger.info(f"详情采集完成: 共 {len(all_details)} 条")
        return all_details

    def _crawl_one_detail(self, url: str, product: Dict[str, Any]) -> Dict[str, Any]:
        """
        下载并解析单个商品详情。

        优先使用 requests 直接请求（带反爬虫 Cookie），
        失败时回退到列表页中已有的属性数据。
        """
        html = self._fetch_detail_html(url)

        if html:
            return parse_detail(html)
        else:
            # 回退: 使用列表页已有的属性数据
            logger.debug(f"  详情页无法访问，使用列表页属性数据")
            return {
                "sku": product.get("sku", ""),
                "title": product.get("model", "") or product.get("title", ""),
                "attributes": product.get("attributes", {}),
                "datasheet_url": product.get("datasheet_url", ""),
                "image_url": product.get("image_url", ""),
            }

    def _fetch_detail_html(self, url: str) -> Optional[str]:
        """
        获取详情页 HTML。

        Returns:
            HTML 文本，或 None（如果被 WAF 拦截）
        """
        # 方案 1: requests 直接请求
        try:
            resp = self.session.get(url, timeout=self.session.timeout)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding

            # 检测是否被 WAF 拦截
            if "aliyun_waf" in resp.text or "renderData" in resp.text:
                logger.debug(f"  被阿里云 WAF 拦截: {url}")
                return None

            if "var _xvasu" in resp.text and "<body></body>" in resp.text:
                logger.debug(f"  被 JS 挑战拦截: {url}")
                return None

            return resp.text

        except Exception as e:
            logger.debug(f"  请求详情页异常: {e}")
            return None
