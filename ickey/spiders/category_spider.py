"""
分类采集 Spider

职责：下载首页 → 解析分类树 → 保存到 categories.json
"""

from typing import List, Dict, Any

import requests

from parsers.category_parser import parse_categories
from storage.json_storage import save_json
from utils.headers import get_headers
from utils.logger import get_logger

logger = get_logger(__name__)

# ICKEY 首页 URL
HOME_URL = "https://www.ickey.cn/"


class CategorySpider:
    """分类采集器"""

    def __init__(self, session: requests.Session):
        """
        Args:
            session: 共享的 requests.Session
        """
        self.session = session

    def crawl(self) -> List[Dict[str, Any]]:
        """
        采集全站分类树。

        Returns:
            分类树列表
        """
        logger.info("开始采集分类数据...")

        # 1. 下载首页
        html = self._fetch_homepage()

        # 2. 解析分类树
        categories = parse_categories(html)
        logger.info(f"解析完成: {len(categories)} 个一级分类")

        # 3. 统计子分类数量
        total_items = sum(
            len(item["items"])
            for cat in categories
            for item in cat.get("sub_categories", [])
        )
        logger.info(f"共 {total_items} 个三级分类")

        return categories

    def run(self, output_path: str) -> List[Dict[str, Any]]:
        """
        执行采集并保存。

        Args:
            output_path: 输出 JSON 文件路径

        Returns:
            分类树列表
        """
        categories = self.crawl()
        save_json(categories, output_path)
        logger.info(f"分类数据已保存到: {output_path}")
        return categories

    def _fetch_homepage(self) -> str:
        """下载首页 HTML"""
        logger.info(f"请求首页: {HOME_URL}")
        resp = self.session.get(HOME_URL, timeout=self.session.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        return resp.text
