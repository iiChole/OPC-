"""
商品列表 Spider

职责：
    遍历 categories.json → 请求列表 API → 解析商品 → 保存到 products.json

API:
    GET https://search.ickey.cn/cate-search/get-search-result
    参数: cate_id, page, page_size (max 50), _csrf, v_, v

支持:
    - 自动分页
    - 断点续爬（按 cate_id checkpoint）
"""

import re
import time
from typing import List, Dict, Any, Set, Optional, Tuple

import requests

from parsers.list_parser import parse_product_list, parse_total
from storage.json_storage import (
    save_json,
    load_json,
    load_jsonl,
    JSONLWriter,
    load_checkpoint,
    mark_completed,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# API 配置
LIST_API = "https://search.ickey.cn/cate-search/get-search-result"
CATE_PAGE_URL = "https://search.ickey.cn/cate-search"
PAGE_SIZE = 50          # API 最大每页数量
REQUEST_DELAY = 0.5         # 请求间隔（秒）
MAX_CONSECUTIVE_FAILURES = 20  # 连续失败上限（超过后跳过该分类）
TOKEN_REFRESH_INTERVAL = 10   # 每 N 页主动刷新 token（防止过期）


class TokenExpiredError(Exception):
    """API token 过期异常"""
    pass


class ListSpider:
    """商品列表采集器"""

    def __init__(self, session: requests.Session):
        self.session = session
        self._csrf: str = ""
        self._v_: str = ""

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

        # 刷新 tokens（使用默认分类获取初始 token）
        self._refresh_tokens()

        # 加载断点
        completed_cates: Set[str] = load_checkpoint(checkpoint_path)
        if completed_cates:
            logger.info(f"断点恢复: {len(completed_cates)} 个分类已完成，跳过")

        # 展开三级分类
        all_items = self._flatten_categories(categories)
        logger.info(f"共 {len(all_items)} 个三级分类待采集")

        # JSONL 流式写入器（信号安全 + 批量 Flush）
        writer = JSONLWriter(output_path, flush_size=50, flush_interval=10)
        all_products: List[Dict[str, Any]] = []

        try:
            for i, item in enumerate(all_items):
                cate_id = item["cate_id"]
                cate_name = item["name"]

                if cate_id in completed_cates:
                    continue

                logger.info(f"[{i+1}/{len(all_items)}] 采集: {cate_name} (cate_id={cate_id})")

                try:
                    products = self._crawl_one_category(cate_id)
                    logger.info(f"  → {len(products)} 个商品")

                    if products:
                        writer.extend(products)
                        all_products.extend(products)

                    mark_completed(checkpoint_path, cate_id)
                    completed_cates.add(cate_id)

                except Exception as e:
                    logger.error(f"  ✗ 失败 [{cate_id}]: {e}")
                    try:
                        self._refresh_tokens(cate_id)
                    except Exception:
                        pass
                    continue

                time.sleep(REQUEST_DELAY)
        finally:
            writer.close()

        logger.info(f"列表采集完成: 共 {len(all_products)} 个商品")
        return all_products

    def _crawl_one_category(self, cate_id: str) -> List[Dict[str, Any]]:
        """采集单个分类下的所有商品，token 过期自动刷新"""
        all_products: List[Dict[str, Any]] = []
        consecutive_failures = 0

        # 第 1 页
        try:
            data = self._fetch_api(cate_id, page=1)
        except TokenExpiredError:
            self._refresh_tokens(cate_id)
            data = self._fetch_api(cate_id, page=1)

        products = parse_product_list(data, cate_id)
        total = parse_total(data)
        all_products.extend(products)

        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total > 0 else 1
        logger.info(f"  总数={total}, 总页数={total_pages}")

        # 剩余页
        for page in range(2, total_pages + 1):
            time.sleep(REQUEST_DELAY)

            # 每 N 页主动刷新 token，防止过期
            if page > 2 and page % TOKEN_REFRESH_INTERVAL == 0:
                try:
                    self._refresh_tokens(cate_id)
                    logger.debug(f"  主动刷新 token (page={page})")
                except Exception:
                    pass

            try:
                data = self._fetch_api(cate_id, page=page)
                page_products = parse_product_list(data, cate_id)
                if page_products:
                    all_products.extend(page_products)
                    consecutive_failures = 0
                else:
                    # 空列表：可能是 token 过期导致
                    consecutive_failures += 1
                    logger.debug(f"  第 {page} 页返回空数据 ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.warning(f"  连续 {MAX_CONSECUTIVE_FAILURES} 页无数据，停止分页")
                        break

            except TokenExpiredError:
                logger.info(f"  第 {page} 页 Token 过期，刷新后重试...")
                consecutive_failures = 0
                try:
                    self._refresh_tokens(cate_id)
                    data = self._fetch_api(cate_id, page=page)
                    page_products = parse_product_list(data, cate_id)
                    if page_products:
                        all_products.extend(page_products)
                    else:
                        consecutive_failures += 1
                except Exception as e:
                    logger.warning(f"  Token 刷新后仍失败: {e}")
                    consecutive_failures += 1

            except Exception as e:
                consecutive_failures += 1
                logger.warning(f"  第 {page} 页失败 ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(f"  连续失败达到上限，停止分页（已采集 {len(all_products)} 个）")
                    break
                continue

        return all_products

    def _fetch_api(self, cate_id: str, page: int) -> dict:
        """调用列表 API，token 过期时抛出 TokenExpiredError"""
        params = {
            "cate_id": cate_id,
            "page": str(page),
            "page_size": str(PAGE_SIZE),
            "_csrf": self._csrf,
            "v_": self._v_,
            "v": str(int(time.time() * 1000)),
        }
        resp = self.session.get(LIST_API, params=params, timeout=self.session.timeout)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            msg = data.get("msg", "unknown")
            # token 过期特征：success=false 且返回特定错误信息
            if "csrf" in str(msg).lower() or "token" in str(msg).lower() or "login" in str(msg).lower():
                raise TokenExpiredError(f"Token 过期: {msg}")
            # 其他错误也可能是 token 相关，一并处理
            if len(str(data.get("result", {}))) < 50:
                raise TokenExpiredError(f"疑似 Token 过期: {msg}")
            raise RuntimeError(f"API error: {msg}")

        return data

    def _refresh_tokens(self, cate_id: str = "010101") -> None:
        """从列表首页获取 _csrf 和 v_（可按分类获取对应 token）"""
        resp = self.session.get(CATE_PAGE_URL, params={"cate_id": cate_id}, timeout=self.session.timeout)
        resp.raise_for_status()
        html = resp.text

        m = re.search(r"var v_\s*=\s*'([^']+)'", html)
        if m:
            self._v_ = m.group(1)

        m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
        if m:
            self._csrf = m.group(1)

        logger.debug(f"Tokens refreshed (cate={cate_id}): v_={self._v_[:20]}..., _csrf={self._csrf[:20]}...")

    @staticmethod
    def _flatten_categories(categories: List[Dict]) -> List[Dict]:
        """展开分类树为三级分类列表"""
        items = []
        for cat in categories:
            for sub in cat.get("sub_categories", []):
                for item in sub.get("items", []):
                    if item.get("cate_id"):
                        items.append(item)
        return items
