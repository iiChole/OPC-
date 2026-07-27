"""
商品详情 Spider

职责：
    遍历 products.jsonl → 按 family 分组 → 批量调 alternate-gpn API → 解析参数表 → 保存到 product_details.jsonl

API:
    GET /selectionmodel/api/gpn/result-list
        ?destinationId={id}
        &destinationType=GPT
        &mode=alternate-gpn
        &locale=en-US
        &partList=PN1,PN2,...,PN10

支持:
    - 批量请求（每批最多 BATCH_SIZE 个型号）
    - 断点续爬（按 SKU checkpoint）
    - JSONL 流式写入
"""

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
from utils.headers import get_headers
from utils.logger import get_logger

logger = get_logger(__name__)

API_BASE = "https://www.ti.com/selectionmodel/api/gpn/result-list"
REQUEST_DELAY = 0.3
BATCH_SIZE = 10  # 每批请求的型号数量（控制 URL 长度）


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
        遍历 products.jsonl，批量采集每个商品的详细参数。

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
        logger.info(f"按 {len(groups)} 个 family 分组，每批 {BATCH_SIZE} 个")

        try:
            for family_id, group in groups.items():
                # 过滤已完成的
                pending = [p for p in group if p.get("sku") not in completed_skus]
                if not pending:
                    continue

                family_name = group[0].get("family_name", "")
                logger.info(
                    f"family {family_id} ({family_name}): "
                    f"{len(pending)} 个待采集"
                )

                # 分批请求
                for batch_start in range(0, len(pending), BATCH_SIZE):
                    batch = pending[batch_start : batch_start + BATCH_SIZE]
                    part_list = ",".join(p["sku"] for p in batch)

                    try:
                        details = self._fetch_batch(family_id, part_list)

                        for detail in details:
                            sku = detail.get("sku", "")
                            writer.append(detail)
                            all_details.append(detail)
                            if sku:
                                mark_completed(checkpoint_path, sku)
                                completed_skus.add(sku)

                    except Exception as e:
                        logger.error(
                            f"  ✗ 批次失败 [family={family_id}, "
                            f"batch={batch_start//BATCH_SIZE + 1}]: {e}"
                        )
                        # 逐条重试
                        for p in batch:
                            try:
                                detail = self._fetch_batch(
                                    family_id, p["sku"]
                                )
                                if detail:
                                    sku = detail[0].get("sku", "")
                                    writer.append(detail[0])
                                    all_details.append(detail[0])
                                    if sku:
                                        mark_completed(checkpoint_path, sku)
                                        completed_skus.add(sku)
                            except Exception as e2:
                                logger.error(f"    ✗ 单条失败 [{p['sku']}]: {e2}")

                    time.sleep(REQUEST_DELAY)
        finally:
            writer.close()

        logger.info(f"详情采集完成: 共 {len(all_details)} 条")
        return all_details

    def _fetch_batch(
        self, family_id: str, part_list: str
    ) -> List[Dict[str, Any]]:
        """
        批量获取产品详细参数。

        Args:
            family_id: destinationId
            part_list: 逗号分隔的型号列表

        Returns:
            产品详情列表
        """
        params = {
            "destinationId": family_id,
            "destinationType": "GPT",
            "mode": "alternate-gpn",
            "locale": "en-US",
            "partList": part_list,
        }

        headers = get_headers(
            {"Referer": f"https://www.ti.com/product/{part_list.split(',')[0]}"}
        )

        resp = self.session.get(API_BASE, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        return [self._parse_detail(r) for r in results]

    def _parse_detail(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析单个产品的 API 返回数据 → 扁平化属性字典。
        """
        gpn = raw.get("genericPartNumber", "")
        loc = raw.get("localization", {}).get("en-US", {})

        # 解析参数表: 每个参数取 base 值
        attributes: Dict[str, str] = {}
        for param in raw.get("paramList", []):
            name = param.get("name", "")
            value_dict = param.get("value", {})
            base_values = value_dict.get("base", [])

            if not name:
                continue

            # 拼接多个 base 值
            value = ", ".join(str(v) for v in base_values) if base_values else ""

            # 如果有 attr (min/max/typ)，生成带后缀的 key
            attr = param.get("attr", "")
            key = f"{name}{'_' + attr if attr else ''}"

            attributes[key] = value

        # 解析分类层级
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
        }
