"""
商品详情 Spider

职责：
    遍历 products.jsonl → 按 family 分组 → 批量调 alternate-gpn API (技术参数)
    → 逐产品抓取产品页 HTML → 提取 JSON-LD (价格/库存)
    → 保存到 product_details.jsonl

数据来源:
    1. selectionmodel API → 技术参数表
    2. 产品页 JSON-LD → OPN 价格 + 库存状态

反爬策略:
    - 随机 UA 轮换
    - 指数退避 + 随机抖动延迟
    - Session Cookie 复用
    - 请求间隔 1-3 秒

支持:
    - 批量 API 请求（每批 BATCH_SIZE 个型号）
    - 断点续爬（按 SKU checkpoint）
    - JSONL 流式写入
"""

import json
import random
import re
import time
from typing import List, Dict, Any, Set, Optional
from collections import defaultdict

import requests

from storage.json_storage import (
    load_jsonl,
    JSONLWriter,
    load_checkpoint,
    mark_completed,
)
from utils.logger import get_logger

logger = get_logger(__name__)

API_BASE = "https://www.ti.com/selectionmodel/api/gpn/result-list"

# 批量 API 请求大小
BATCH_SIZE = 10

# 反爬延迟配置
API_DELAY_MIN = 1.0   # API 调用后最小等待（秒）
API_DELAY_MAX = 2.0   # API 调用后最大等待（秒）
PAGE_DELAY_MIN = 1.0  # 产品页抓取最小间隔（秒）
PAGE_DELAY_MAX = 2.5  # 产品页抓取最大间隔（秒）

# UA 轮换池
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _jitter(base_min: float, base_max: float) -> float:
    """带抖动的随机延迟"""
    return base_min + random.random() * (base_max - base_min)


class DetailSpider:
    """商品详情采集器（技术参数 + 价格库存）"""

    def __init__(self, session: requests.Session):
        self.session = session

    def crawl(
        self,
        products_path: str,
        output_path: str,
        checkpoint_path: str,
    ) -> List[Dict[str, Any]]:
        """
        遍历 products.jsonl，批量采集每个商品的详细参数 + 库存价格。

        Args:
            products_path: products.jsonl 路径
            output_path: product_details.jsonl 输出路径
            checkpoint_path: 断点文件路径（按 SKU）

        Returns:
            所有商品详情列表
        """
        products = load_jsonl(products_path)
        if not products:
            logger.error("产品列表为空，请先运行 list_spider")
            return []

        completed_skus: Set[str] = load_checkpoint(checkpoint_path)
        if completed_skus:
            logger.info(f"断点恢复: {len(completed_skus)} 个产品已完成")

        # 按 family_id 分组
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for p in products:
            groups[str(p.get("family_id", "__unknown__"))].append(p)

        writer = JSONLWriter(output_path, flush_size=50, flush_interval=10)
        all_details: List[Dict[str, Any]] = []
        total = len(products)
        done = len(completed_skus)
        remaining = total - done

        logger.info(f"总产品: {total}, 已完成: {done}, 剩余: {remaining}")
        logger.info(f"按 {len(groups)} 个 family 分组，API 每批 {BATCH_SIZE} 个")
        logger.info(f"反爬延迟: API {API_DELAY_MIN}-{API_DELAY_MAX}s, 页面 {PAGE_DELAY_MIN}-{PAGE_DELAY_MAX}s")

        try:
            for family_id, group in groups.items():
                pending = [p for p in group if p.get("sku") not in completed_skus]
                if not pending:
                    continue

                family_name = group[0].get("family_name", "")
                logger.info(
                    f"family {family_id} ({family_name}): "
                    f"{len(pending)} 个待采集"
                )

                for batch_start in range(0, len(pending), BATCH_SIZE):
                    batch = pending[batch_start : batch_start + BATCH_SIZE]
                    part_list = ",".join(p["sku"] for p in batch)

                    # Step A: 批量 API 获取技术参数
                    try:
                        details = self._fetch_batch_api(family_id, part_list)
                    except Exception as e:
                        logger.error(
                            f"  ✗ API 批次失败 [family={family_id}]: {e}"
                        )
                        # 逐条 API 重试
                        details = []
                        for p in batch:
                            try:
                                d = self._fetch_batch_api(family_id, p["sku"])
                                if d:
                                    details.extend(d)
                            except Exception as e2:
                                logger.error(f"    ✗ 单条 API 失败 [{p['sku']}]: {e2}")

                    # API 调用后延迟
                    time.sleep(_jitter(API_DELAY_MIN, API_DELAY_MAX))

                    # Step B: 逐产品抓取页面 HTML → 提取库存/价格
                    for detail in details:
                        sku = detail.get("sku", "")
                        if not sku:
                            continue

                        try:
                            offers = self._fetch_offers(sku)
                            detail["offers"] = offers
                        except Exception as e:
                            logger.debug(f"  库存提取失败 [{sku}]: {e}")
                            detail["offers"] = []

                        writer.append(detail)
                        all_details.append(detail)
                        mark_completed(checkpoint_path, sku)
                        completed_skus.add(sku)

                        # 页面抓取后延迟（反爬）
                        time.sleep(_jitter(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

        finally:
            writer.close()

        logger.info(f"详情采集完成: 共 {len(all_details)} 条")
        return all_details

    # ---------- API 层 ----------

    def _fetch_batch_api(
        self, family_id: str, part_list: str
    ) -> List[Dict[str, Any]]:
        """
        批量调 selectionmodel API 获取技术参数。
        """
        params = {
            "destinationId": family_id,
            "destinationType": "GPT",
            "mode": "alternate-gpn",
            "locale": "en-US",
            "partList": part_list,
        }

        headers = {
            "accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "Referer": f"https://www.ti.com/product/{part_list.split(',')[0]}",
            "User-Agent": _random_ua(),
        }

        resp = self.session.get(API_BASE, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        return [self._parse_api_detail(r) for r in data.get("results", [])]

    # ---------- 页面层（库存/价格）----------

    def _fetch_offers(self, gpn: str) -> List[Dict[str, Any]]:
        """
        从产品页 HTML 的 JSON-LD 中提取 OPN 价格和库存。
        """
        url = f"https://www.ti.com/product/{gpn}"
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.ti.com/",
            "User-Agent": _random_ua(),
        }

        resp = self.session.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        html = resp.text

        return self._parse_jsonld_offers(html)

    def _parse_jsonld_offers(self, html: str) -> List[Dict[str, Any]]:
        """
        解析 JSON-LD structured data 中的 offers 数据。

        返回格式:
            [{
                "opn": "TLV3211QDCKRQ1",
                "price": "0.414",
                "currency": "USD",
                "availability": "in_stock",
                "sku": "TLV3211QDCKRQ1",
            }]
        """
        offers: List[Dict[str, Any]] = []

        # 匹配所有 JSON-LD script 块
        ld_blocks = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )

        for block in ld_blocks:
            try:
                data = json.loads(block.strip())
            except json.JSONDecodeError:
                continue

            self._extract_offers_recursive(data, offers)

        # 去重（按 opn）
        seen: Set[str] = set()
        unique: List[Dict[str, Any]] = []
        for o in offers:
            opn = o.get("opn", "")
            if opn and opn not in seen:
                seen.add(opn)
                unique.append(o)

        return unique

    def _extract_offers_recursive(
        self, obj: Any, results: List[Dict], depth: int = 0
    ) -> Optional[str]:
        """
        递归遍历 JSON-LD，合并嵌套 Offer 的 SKU + 价格 + 库存。

        TI 的 JSON-LD 结构:
            offers[i] → {price, itemOffered: {sku, offers: {availability}}}
            外层有 SKU 无库存，内层 (itemOffered.offers) 有库存无 SKU。
        """
        if depth > 8:
            return None

        if isinstance(obj, dict):
            if obj.get("@type") == "Offer":
                return obj.get("availability", "")

            # 不直接处理 Offer（交给上面），遍历子元素
            avail = None
            for v in obj.values():
                a = self._extract_offers_recursive(v, results, depth + 1)
                if a:
                    avail = a
            return avail

        elif isinstance(obj, list):
            # 处理 offers 数组: 每个元素是外层 Offer
            for item in obj:
                if isinstance(item, dict) and item.get("@type") == "Offer":
                    self._process_offer(item, results)
            return None

        return None

    def _process_offer(self, offer: Dict, results: List[Dict]) -> None:
        """
        处理单个外层 Offer: 提取 SKU + 价格，
        并从 itemOffered.offers 中提取 availability。
        """
        offered = offer.get("itemOffered", {})
        if not isinstance(offered, dict):
            return

        opn = offered.get("sku") or offered.get("mpn") or ""
        if not opn:
            return

        # 从内层 offers 提取 availability（可能是 dict 或 list）
        inner_offers = offered.get("offers", {})
        availability = ""
        if isinstance(inner_offers, dict):
            availability = inner_offers.get("availability", "")
        elif isinstance(inner_offers, list) and len(inner_offers) > 0:
            inner = inner_offers[0]
            if isinstance(inner, dict):
                availability = inner.get("availability", "")
        # 也检查外层 availability
        if not availability:
            availability = offer.get("availability", "")

        if "inStock" in str(availability):
            stock = "in_stock"
        elif "OutOfStock" in str(availability):
            stock = "out_of_stock"
        elif "PreOrder" in str(availability):
            stock = "pre_order"
        else:
            stock = "unknown"

        results.append({
            "opn": opn,
            "price": offer.get("price", ""),
            "currency": offer.get("priceCurrency", ""),
            "availability": stock,
            "sku": opn,
        })

    # ---------- 数据解析 ----------

    def _parse_api_detail(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析 selectionmodel API 返回 → 扁平化属性字典。
        """
        gpn = raw.get("genericPartNumber", "")
        loc = raw.get("localization", {}).get("en-US", {})

        # 技术参数表
        attributes: Dict[str, str] = {}
        for param in raw.get("paramList", []):
            name = param.get("name", "")
            value_dict = param.get("value", {})
            base_values = value_dict.get("base", [])

            if not name:
                continue

            value = ", ".join(str(v) for v in base_values) if base_values else ""
            attr = param.get("attr", "")
            key = f"{name}{'_' + attr if attr else ''}"
            attributes[key] = value

        sub_families = [sf.get("name", "") for sf in loc.get("subFamilies", [])]
        silo_families = [sf.get("name", "") for sf in loc.get("siloFamilies", [])]

        return {
            "sku": gpn,
            "title": loc.get("title", ""),
            "description": loc.get("productDesc", ""),
            "status": raw.get("productStatus", {}).get("name", ""),
            "status_id": raw.get("productStatus", {}).get("id"),
            "opn_list": raw.get("opnList", []),
            "sub_families": sub_families,
            "silo_families": silo_families,
            "attributes": attributes,
            "rating": raw.get("rating", {}),
            "functional_safety": raw.get("functionalSafety", {}),
            "datasheet_pdf": raw.get("datasheetPDF", False),
            "datasheet_html": raw.get("datasheetHTML", False),
            "fbd_urls": raw.get("fbdURLs", []),
            "lowest_family_id": raw.get("lowestFamilyId"),
            "offers": [],  # 待 Step B 填充
        }
