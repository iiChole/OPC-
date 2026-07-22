"""
商品列表解析器

将 get-search-result API 返回的 JSON → 商品摘要列表。
纯函数，不涉及网络请求。
"""

from typing import List, Dict, Any, Optional


def parse_product_list(data: dict, cate_id: str) -> List[Dict[str, Any]]:
    """
    解析 API 返回的 JSON，提取商品摘要。

    Args:
        data: get-search-result API 返回的完整 JSON
        cate_id: 当前分类 ID

    Returns:
        商品列表，格式:
        [
            {
                "cate_id": "010101",
                "sku": "1003001442049056",
                "title": "30Ω ±5% 250mW",
                "stock": 3657,
                "price": [{"num": 1, "rmb": 0.0029}, ...],
                "manufacturer": "UNI-ROYAL",
                "moq": 1,
                "package": "插件,D2.2XL6.5MM",
                "image_url": "//www.ickey.cn/...",
                "detail_url": "//www.ickey.cn/detail/1003001442049056/...",
                "description": "通孔电阻器 30Ω ±5% 1/4W ±450ppm/°C",
            },
            ...
        ]
    """
    products_raw = data.get("result", {}).get("products", [])
    return [_parse_one(p, cate_id) for p in products_raw]


def parse_total(data: dict) -> int:
    """
    从 API 响应中提取商品总数。

    Args:
        data: get-search-result API 返回的完整 JSON

    Returns:
        商品总数
    """
    return data.get("result", {}).get("total", 0)


def _parse_one(item: dict, cate_id: str) -> Dict[str, Any]:
    """将单条 API 商品记录转为标准格式"""
    # 价格：合并阶梯数量与价格
    nums = item.get("nums") or []
    rmb_prices = item.get("calc_sale_rmb_price") or []
    price = []
    for i in range(min(len(nums), len(rmb_prices))):
        price.append({"num": nums[i], "rmb": rmb_prices[i]})

    # 图片 URL 补全协议
    img_url = item.get("img_url") or ""
    if img_url.startswith("//"):
        img_url = "https:" + img_url

    # 详情 URL 补全协议
    detail_url = item.get("url") or ""
    if detail_url.startswith("//"):
        detail_url = "https:" + detail_url

    return {
        "cate_id": cate_id,
        "sku": item.get("sku", ""),
        "title": item.get("title", "").strip(),
        "stock": item.get("stock", 0),
        "price": price,
        "manufacturer": item.get("pro_maf") or item.get("std_mfr_name", ""),
        "moq": item.get("moq", 1),
        "package": item.get("reference_package", ""),
        "image_url": img_url,
        "detail_url": detail_url,
        "description": item.get("short_desc", ""),
    }
