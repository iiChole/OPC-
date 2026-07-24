"""
商品列表解析器

将 list.szlcsc.com/catalog/{id}.html 中的 __NEXT_DATA__ JSON
解析为商品摘要列表。纯函数，不涉及网络请求。

数据来源:
    <script id="__NEXT_DATA__" type="application/json">
    {
      "props": {
        "pageProps": {
          "catalogResResult": {
            "searchResult": {
              "productRecordList": [...],
              "totalCount": 1500,
              "pageSize": 30
            }
          }
        }
      }
    }
"""

import json
from typing import List, Dict, Any, Optional

from bs4 import BeautifulSoup


def parse_product_list(html: str, catalog_id: str) -> List[Dict[str, Any]]:
    """
    解析列表页 HTML，提取商品摘要。

    Args:
        html: 列表页 HTML 文本
        catalog_id: 当前分类 ID

    Returns:
        商品列表，格式:
        [
            {
                "catalog_id": "313",
                "sku": "C6119867",
                "title": "100nF ±10% 50V",
                "model": "CGA0603X7R104K500JT",
                "brand": "HRE(芯声)",
                "stock": 2619289,
                "price": [{"num": 1, "rmb": 0.0123}, ...],
                "moq": 1,
                "package": "0603",
                "image_url": "https://...",
                "detail_url": "https://item.szlcsc.com/7062573.html",
                "description": "贴片电容(MLCC)",
            },
            ...
        ]
    """
    soup = BeautifulSoup(html, "html.parser")
    next_data_el = soup.select_one("#__NEXT_DATA__")

    if not next_data_el:
        return []

    try:
        data = json.loads(next_data_el.get_text(strip=True))
    except json.JSONDecodeError:
        return []

    product_list = (
        data.get("props", {})
        .get("pageProps", {})
        .get("catalogResResult", {})
        .get("searchResult", {})
        .get("productRecordList", [])
    )

    return [_parse_one(p, catalog_id) for p in product_list]


def parse_total(html: str) -> int:
    """
    从列表页中提取商品总数。

    Args:
        html: 列表页 HTML 文本

    Returns:
        商品总数
    """
    soup = BeautifulSoup(html, "html.parser")
    next_data_el = soup.select_one("#__NEXT_DATA__")
    if not next_data_el:
        return 0

    try:
        data = json.loads(next_data_el.get_text(strip=True))
    except json.JSONDecodeError:
        return 0

    return (
        data.get("props", {})
        .get("pageProps", {})
        .get("catalogResResult", {})
        .get("searchResult", {})
        .get("totalCount", 0)
    )


def parse_page_size(html: str) -> int:
    """
    从列表页中提取每页数量。

    Args:
        html: 列表页 HTML 文本

    Returns:
        每页商品数
    """
    soup = BeautifulSoup(html, "html.parser")
    next_data_el = soup.select_one("#__NEXT_DATA__")
    if not next_data_el:
        return 30

    try:
        data = json.loads(next_data_el.get_text(strip=True))
    except json.JSONDecodeError:
        return 30

    return (
        data.get("props", {})
        .get("pageProps", {})
        .get("catalogResResult", {})
        .get("searchResult", {})
        .get("pageSize", 30)
    )


def _parse_one(record: dict, catalog_id: str) -> Dict[str, Any]:
    """
    将单条 __NEXT_DATA__ 商品记录转为标准格式。

    原始数据结构:
        {
            "productVO": { ... },
            "lightProductCode": "C6119867",
            "lightBrandName": "HRE(芯声)",
            "lightProductModel": "CGA0603X7R104K500JT",
            "lightProductIntro": "...",
            "paramLinkedMap": {...},
            "priceDiscount": [...],
            ...
        }
    """
    vo = record.get("productVO", {})

    # SKU: 使用 productCode (如 C6119867)
    sku = vo.get("productCode", "") or record.get("lightProductCode", "")

    # 名称: productName
    title = vo.get("productName", "").strip()

    # 型号
    model = vo.get("productModel", "") or record.get("lightProductModel", "")

    # 品牌/制造商
    brand = vo.get("productGradePlateName", "") or record.get("lightBrandName", "")

    # 库存
    stock = vo.get("stockNumber", 0)

    # 价格: 从 productPriceList 提取阶梯价
    price_list = vo.get("productPriceList") or []
    price = []
    for pp in price_list:
        if isinstance(pp, dict):
            price.append({
                "num": pp.get("startPurchasedNumber", 1),
                "rmb": pp.get("productPrice") or pp.get("thePrice", 0),
            })

    # 折扣价（如有，优先使用）
    discount_list = record.get("priceDiscount")
    if discount_list and isinstance(discount_list, list):
        discount_price = []
        for dp in discount_list:
            if isinstance(dp, dict):
                discount_price.append({
                    "num": dp.get("startPurchasedNumber", 1),
                    "rmb": dp.get("discountPrice", 0),
                })
        if discount_price:
            price = discount_price

    # MOQ
    moq = vo.get("minBuyNumber", 1)

    # 封装/包装
    package = vo.get("encapsulationModel", "")

    # 图片
    img_url = vo.get("breviaryImageUrl", "") or vo.get("bigImageUrl", "")
    if img_url and img_url.startswith("//"):
        img_url = "https:" + img_url

    # 详情 URL
    product_id = vo.get("productId", "")
    detail_url = f"https://item.szlcsc.com/{product_id}.html" if product_id else ""

    # 描述
    description = vo.get("productType", "") or record.get("lightCatalogName", "")

    # 属性: 从 paramLinkedMap 提取
    attributes: Dict[str, str] = {}
    param_map = record.get("paramLinkedMap", {})
    if param_map:
        for key, value in param_map.items():
            if isinstance(value, str):
                attributes[key] = value
            elif isinstance(value, dict):
                attributes[key] = value.get("paramValue", str(value))

    # 数据手册
    datasheet_url = ""
    file_types = vo.get("fileTypeVOList", [])
    for ft in file_types:
        if ft.get("fileType") == "pdf_property":
            details = ft.get("detailVOList", [])
            if details:
                ds_path = details[0].get("fileUrl", "")
                if ds_path:
                    datasheet_url = f"https://datasheet.lcsc.com{ds_path}" if ds_path.startswith("/") else ds_path
                break

    return {
        "catalog_id": catalog_id,
        "sku": sku,
        "title": title,
        "model": model,
        "brand": brand,
        "stock": stock,
        "price": price,
        "moq": moq,
        "package": package,
        "image_url": img_url,
        "detail_url": detail_url,
        "description": description,
        "attributes": attributes,
        "datasheet_url": datasheet_url,
    }
