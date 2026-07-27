"""
数据合并模块

将 products.json 与 product_details.json 按 SKU 合并，
输出 products_final.json。
"""

import sys
from typing import List, Dict, Any, Optional

from storage.json_storage import load_json, save_json, load_jsonl
from utils.logger import get_logger

logger = get_logger(__name__)


def merge(
    products_path: str,
    details_path: str,
    output_path: str,
) -> List[Dict[str, Any]]:
    """
    合并商品列表和详情数据。

    Args:
        products_path: products.json 路径
        details_path: product_details.json 路径
        output_path: 输出路径

    Returns:
        合并后的商品列表
    """
    # 根据文件扩展名选择加载方式
    products = load_jsonl(products_path) if products_path.endswith('.jsonl') else load_json(products_path)
    if not products:
        logger.error(f"商品列表为空: {products_path}")
        return []

    details = load_jsonl(details_path) if details_path.endswith('.jsonl') else load_json(details_path)
    if not details:
        logger.error(f"详情数据为空: {details_path}")
        return []

    # 构建 SKU → 属性 的快速查找表
    sku_to_attrs: Dict[str, Dict[str, str]] = {}
    sku_to_detail_title: Dict[str, str] = {}
    for d in details:
        sku = d.get("sku", "")
        if sku:
            sku_to_attrs[sku] = d.get("attributes", {})
            sku_to_detail_title[sku] = d.get("title", "")

    logger.info(f"加载 {len(products)} 个商品, {len(details)} 条详情")
    logger.info(f"可匹配详情 SKU 数: {len(sku_to_attrs)}")

    merged: List[Dict[str, Any]] = []
    matched = 0

    for product in products:
        sku = product.get("sku", "")
        detail_attrs = sku_to_attrs.get(sku, {})
        list_attrs = product.get("attributes", {})
        detail_title = sku_to_detail_title.get(sku, "")

        # 优先用详情属性，但如果详情属性为空则保留列表属性
        if detail_attrs and len(detail_attrs) > 0:
            attrs = detail_attrs
            matched += 1
        else:
            attrs = list_attrs

        merged.append({
            **product,
            "detail_title": detail_title or product.get("title", ""),
            "attributes": attrs,
        })

    logger.info(f"合并完成: {matched}/{len(products)} 个商品匹配到详情")

    save_json(merged, output_path)
    logger.info(f"结果已保存: {output_path}")

    return merged


def main():
    """命令行入口"""
    import os

    # 默认路径
    base = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base, "data")

    products_path = os.path.join(data_dir, "products.json")
    details_path = os.path.join(data_dir, "product_details.json")
    output_path = os.path.join(data_dir, "products_final.json")

    # 支持命令行参数
    if len(sys.argv) >= 2:
        products_path = sys.argv[1]
    if len(sys.argv) >= 3:
        details_path = sys.argv[2]
    if len(sys.argv) >= 4:
        output_path = sys.argv[3]

    merge(products_path, details_path, output_path)


if __name__ == "__main__":
    main()
