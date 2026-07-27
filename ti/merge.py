"""
数据合并模块

职责：
    合并 products.jsonl（列表基础数据） + product_details.jsonl（详细参数） → products_final.json

合并策略:
    - 以 SKU 为 key
    - 基础数据（列表 API 已有）不覆盖详情的更完整数据
    - 详情数据优先
"""

from typing import List, Dict, Any

from storage.json_storage import load_jsonl, save_json
from utils.logger import get_logger

logger = get_logger(__name__)


def merge(
    products_path: str,
    details_path: str,
    output_path: str,
) -> List[Dict[str, Any]]:
    """
    合并产品列表和详情。

    详情数据优先：对于同名字段，详情覆盖列表。
    列表独有的字段（family_id, category 等）保留。

    Args:
        products_path: products.jsonl 路径
        details_path: product_details.jsonl 路径
        output_path: products_final.json 输出路径

    Returns:
        合并后的完整产品列表
    """
    products = load_jsonl(products_path)
    details = load_jsonl(details_path)

    if not products:
        logger.error("产品列表为空")
        return []

    logger.info(f"产品列表: {len(products)} 条")
    logger.info(f"产品详情: {len(details)} 条")

    # 以 SKU 为索引
    detail_map: Dict[str, Dict] = {}
    for d in details:
        sku = d.get("sku", "")
        if sku:
            # 如果同一 SKU 有多条，取 attributes 更完整的那条
            if sku in detail_map:
                existing = detail_map[sku]
                if len(d.get("attributes", {})) > len(existing.get("attributes", {})):
                    detail_map[sku] = d
            else:
                detail_map[sku] = d

    final: List[Dict[str, Any]] = []
    matched = 0

    for p in products:
        sku = p.get("sku", "")
        detail = detail_map.get(sku)

        if detail:
            matched += 1
            # 合并：详情覆盖列表的基础字段，保留列表独有的分类信息
            merged = {
                # 列表基础字段
                "sku": sku,
                "family_id": p.get("family_id"),
                "family_name": p.get("family_name"),
                "category": p.get("category"),
                "subcategory": p.get("subcategory"),
                # 详情数据优先
                "title": detail.get("title") or p.get("title", ""),
                "description": detail.get("description") or p.get("description", ""),
                "status": detail.get("status") or p.get("status", ""),
                "status_id": detail.get("status_id") or p.get("status_id"),
                "opn_list": detail.get("opn_list") or p.get("opn_list", []),
                "sub_families": detail.get("sub_families", []),
                "silo_families": detail.get("silo_families", []),
                "attributes": detail.get("attributes", {}),
                "rating": detail.get("rating") or p.get("rating", {}),
                "functional_safety": detail.get("functional_safety") or p.get("functional_safety", {}),
                "datasheet_pdf": detail.get("datasheet_pdf") if detail else p.get("datasheet_pdf", False),
                "datasheet_html": detail.get("datasheet_html") if detail else p.get("datasheet_html", False),
                "fbd_urls": detail.get("fbd_urls", []),
                "lowest_family_id": detail.get("lowest_family_id"),
            }
            final.append(merged)
        else:
            # 无详情数据，保留列表基础数据
            final.append(p)

    # 补充：有详情但不在列表中的产品（极少见）
    for sku, detail in detail_map.items():
        if sku not in {p.get("sku") for p in products}:
            final.append({
                "sku": sku,
                "title": detail.get("title", ""),
                "description": detail.get("description", ""),
                "status": detail.get("status", ""),
                "status_id": detail.get("status_id"),
                "opn_list": detail.get("opn_list", []),
                "attributes": detail.get("attributes", {}),
                "rating": detail.get("rating", {}),
                "functional_safety": detail.get("functional_safety", {}),
                "datasheet_pdf": detail.get("datasheet_pdf", False),
                "datasheet_html": detail.get("datasheet_html", False),
                "fbd_urls": detail.get("fbd_urls", []),
            })

    save_json(final, output_path)
    logger.info(
        f"合并完成: {len(final)} 个产品 "
        f"(匹配详情: {matched}, 仅列表: {len(products) - matched}, "
        f"仅详情: {len(final) - len(products)})"
    )
    logger.info(f"输出: {output_path}")

    return final
