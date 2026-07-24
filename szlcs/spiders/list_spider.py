"""
商品列表 Spider

职责：
    遍历 categories.json → 请求列表页 HTML → 解析嵌入式 JSON → 保存到 products.json

URL:
    GET https://list.szlcsc.com/catalog/{catalog_id}.html?page={page}

特点:
    - 列表页使用 Next.js SSR，商品数据嵌入在 <script id="__NEXT_DATA__"> 中
    - 无需 API key，直接解析 JSON
    - 列表数据已包含价格、库存、属性等完整信息

支持:
    - 自动分页
    - 断点续爬（按 catalog_id checkpoint）
    - 反爬虫 Cookie 自动绕过
"""

import time
from typing import List, Dict, Any, Set

import requests

from parsers.list_parser import parse_product_list, parse_total, parse_page_size
from storage.json_storage import (
    save_json,
    load_json,
    load_jsonl,
    JSONLWriter,
    load_checkpoint,
    mark_completed,
)
from utils.session import bypass_anti_bot
from utils.logger import get_logger

logger = get_logger(__name__)

# 列表页配置
LIST_URL_TEMPLATE = "https://list.szlcsc.com/catalog/{catalog_id}.html"
PAGE_SIZE = 30          # 默认每页数量（从页面提取实际值）
REQUEST_DELAY = 0.5     # 请求间隔（秒）


class ListSpider:
    """商品列表采集器"""

    def __init__(self, session: requests.Session):
        self.session = session
        self._anti_bot_bypassed = False

    def crawl(
        self,
        categories_path: str,
        output_path: str,
        checkpoint_path: str,
    ) -> List[Dict[str, Any]]:
        """
        遍历所有分类，采集商品列表。

        Args:
            categories_path: categories.json 路径
            output_path: products.json 输出路径
            checkpoint_path: 断点文件路径

        Returns:
            所有商品列表
        """
        categories = load_json(categories_path)
        if not categories:
            logger.error("分类数据为空，请先运行 category_spider")
            return []

        # 加载断点
        completed_cates: Set[str] = load_checkpoint(checkpoint_path)
        if completed_cates:
            logger.info(f"断点恢复: {len(completed_cates)} 个分类已完成，跳过")

        # 展开三级分类
        all_items = self._flatten_categories(categories)
        logger.info(f"共 {len(all_items)} 个分类待采集")

        # 创建 JSONL 流式写入器（每 50 条或每 10 秒自动 Flush）
        writer = JSONLWriter(output_path, flush_size=50, flush_interval=10)
        all_products: List[Dict[str, Any]] = []

        try:
            for i, item in enumerate(all_items):
                catalog_id = item["catalog_id"]
                catalog_name = item["name"]

                if catalog_id in completed_cates:
                    continue

                logger.info(f"[{i+1}/{len(all_items)}] 采集: {catalog_name} (catalog_id={catalog_id})")

                try:
                    products = self._crawl_one_category(catalog_id)
                    logger.info(f"  → {len(products)} 个商品")

                    if products:
                        writer.extend(products)
                        all_products.extend(products)

                    mark_completed(checkpoint_path, catalog_id)
                    completed_cates.add(catalog_id)

                except Exception as e:
                    logger.error(f"  ✗ 失败 [{catalog_id}]: {e}")
                    # 重置反爬虫状态
                    self._anti_bot_bypassed = False
                    continue

                time.sleep(REQUEST_DELAY)
        finally:
            writer.close()

        logger.info(f"列表采集完成: 共 {len(all_products)} 个商品")
        return all_products

    def _ensure_anti_bot_bypassed(self) -> None:
        """确保反爬虫 Cookie 已绕过"""
        if self._anti_bot_bypassed:
            return

        # 使用一个测试 URL 触发反爬虫绕过
        test_url = LIST_URL_TEMPLATE.format(catalog_id="313")
        if bypass_anti_bot(self.session, test_url):
            self._anti_bot_bypassed = True
            logger.info("反爬虫 Cookie 已就绪")
        else:
            logger.warning("反爬虫绕过未成功，可能影响采集")

    def _crawl_one_category(self, catalog_id: str) -> List[Dict[str, Any]]:
        """采集单个分类下的所有商品（自动分页）"""
        all_products: List[Dict[str, Any]] = []

        # 确保反爬虫绕过
        self._ensure_anti_bot_bypassed()

        # 第 1 页
        html = self._fetch_page(catalog_id, page=1)
        products = parse_product_list(html, catalog_id)
        total = parse_total(html)
        page_size = parse_page_size(html)
        all_products.extend(products)

        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        logger.info(f"  总数={total}, 每页={page_size}, 总页数={total_pages}")

        # 剩余页
        for page in range(2, total_pages + 1):
            time.sleep(REQUEST_DELAY)
            try:
                html = self._fetch_page(catalog_id, page=page)
                page_products = parse_product_list(html, catalog_id)
                all_products.extend(page_products)
            except Exception as e:
                logger.warning(f"  第 {page} 页失败: {e}")
                continue

        return all_products

    def _fetch_page(self, catalog_id: str, page: int) -> str:
        """请求列表页 HTML"""
        url = LIST_URL_TEMPLATE.format(catalog_id=catalog_id)
        params = {"page": str(page)} if page > 1 else {}

        resp = self.session.get(url, params=params, timeout=self.session.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding

        # 检查是否需要重新绕过反爬虫
        if "var _xvasu" in resp.text and "<body></body>" in resp.text:
            logger.debug(f"检测到反爬虫挑战，重新绕过...")
            self._anti_bot_bypassed = False
            self._ensure_anti_bot_bypassed()
            resp = self.session.get(url, params=params, timeout=self.session.timeout)
            resp.raise_for_status()

        return resp.text

    @staticmethod
    def _flatten_categories(categories: List[Dict]) -> List[Dict]:
        """展开分类树为三级分类列表"""
        items = []
        for cat in categories:
            for item in cat.get("items", []):
                if item.get("catalog_id"):
                    items.append(item)
        return items
