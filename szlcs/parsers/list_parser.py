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

    productVO 包含 70 个字段，含完整库存、价格、封装、描述等。
    """
    vo = record.get("productVO", {})

    # ---------- 基本信息 ----------
    sku = vo.get("productCode", "") or record.get("lightProductCode", "")
    title = vo.get("productName", "").strip()
    model = vo.get("productModel", "") or record.get("lightProductModel", "")
    brand = vo.get("productGradePlateName", "") or record.get("lightBrandName", "")
    product_id = vo.get("productId", "")

    # ---------- 分类 ----------
    product_type = vo.get("productType", "")          # 商品分类名
    catalog_name = record.get("lightCatalogName", "") # 目录名

    # ---------- 库存 ----------
    stock = vo.get("stockNumber", 0)                  # 总库存
    valid_stock = vo.get("validStockNumber", 0)       # 有效库存
    has_stock = vo.get("hasStockNow", "")             # 是否有现货 (yes/no)
    stock_status = vo.get("productStockStatus", "")   # 库存状态

    # ---------- 价格 ----------
    price_list = vo.get("productPriceList") or []
    price = []
    for pp in price_list:
        if isinstance(pp, dict):
            price.append({
                "num": pp.get("startPurchasedNumber", 1),
                "rmb": pp.get("productPrice") or pp.get("thePrice", 0),
            })

    # 折扣信息
    is_discount = record.get("isMultiDiscount") or vo.get("isPromotionDiscount") or False
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

    coupon_amount = record.get("couponAmount", 0)        # 优惠券金额
    coupon_threshold = record.get("couponThresholdMoney", 0)  # 优惠券门槛

    # ---------- 采购信息 ----------
    moq = vo.get("minBuyNumber", 1)                     # 最小起订量
    max_buy = vo.get("maxBuyNumber", -1)                # 最大购买量 (-1=不限)
    batch_limit = vo.get("batchStockLimit", 0)          # 批量限制
    product_unit = vo.get("productUnit", "")            # 单位 (个/包/盘)
    unseal = vo.get("productUnsealVendition", "")       # 是否支持拆包 (yes/no)

    # ---------- 封装 / 包装 ----------
    package = vo.get("encapsulationModel", "")           # 封装型号 (如 0603)
    min_encap_unit = vo.get("productMinEncapsulationUnit", "")  # 最小包装单位
    min_encap_num = vo.get("productMinEncapsulationNumber", 0)  # 最小包装数量
    convesion_ratio = vo.get("convesionRatio", 1)        # 换算比

    # ---------- 技术参数 ----------
    attributes = {}
    param_map = record.get("paramLinkedMap", {})
    if param_map:
        for key, value in param_map.items():
            if isinstance(value, str):
                attributes[key] = value
            elif isinstance(value, dict):
                attributes[key] = value.get("paramValue", str(value))

    # ---------- 数据手册 ----------
    datasheet_url = ""
    datasheet_list = []
    file_types = vo.get("fileTypeVOList", [])
    for ft in file_types:
        if ft.get("fileType") == "pdf_property":
            for detail in ft.get("detailVOList", []):
                ds_path = detail.get("fileUrl", "")
                if ds_path:
                    url = f"https://datasheet.lcsc.com{ds_path}" if ds_path.startswith("/") else ds_path
                    ds_name = detail.get("fileName", "")
                    datasheet_list.append({"name": ds_name, "url": url})
                    if not datasheet_url:
                        datasheet_url = url

    # ---------- 图片 ----------
    img_url = vo.get("breviaryImageUrl", "") or vo.get("bigImageUrl", "")
    if img_url and img_url.startswith("//"):
        img_url = "https:" + img_url
    big_img_url = vo.get("bigImageUrl", "")
    if big_img_url and big_img_url.startswith("//"):
        big_img_url = "https:" + big_img_url

    # ---------- 详情 URL ----------
    detail_url = f"https://item.szlcsc.com/{product_id}.html" if product_id else ""

    # ---------- 描述 ----------
    description = record.get("lightProductIntro", "") or vo.get("productType", "")
    remark = vo.get("remark", "")                       # 详细备注/描述

    # ---------- SMT / 等级 ----------
    smt_label = vo.get("smtLabel", "")                  # SMT 标签
    product_cycle = vo.get("productCycle", "")          # 产品生命周期
    grade_plate_name = vo.get("productGradePlateName", "")  # 品牌等级名
    grade_plate_id = vo.get("productGradePlateId", "")  # 品牌等级 ID

    # ---------- 销售数据 ----------
    recent_sales = vo.get("recentlySalesCount", 0)      # 近期销量
    is_hot = record.get("isHot", False)                 # 是否热门
    is_present = vo.get("isPresent", False)             # 是否赠品
    is_old_batch = vo.get("isOldBatch", False)          # 是否旧批次
    rohs_label = record.get("rohsLabal", "")            # RoHS 标签

    # ---------- 仓库库存 ----------
    js_stock = vo.get("jsWarehouseStockNumber")          # 江苏仓库存
    gd_stock = vo.get("gdWarehouseStockNumber")          # 广东仓库存
    smt_stock = record.get("smtStockNumber", 0)          # SMT 仓库存
    total_stock = record.get("totalStockNumber", stock)  # 全仓总库存
    transit_num = vo.get("usableTransitNum", 0)          # 在途数量

    return {
        "catalog_id": catalog_id,
        "sku": sku,
        "title": title,
        "model": model,
        "brand": brand,
        "product_id": product_id,
        # 分类
        "product_type": product_type,
        "catalog_name": catalog_name,
        # 库存
        "stock": stock,
        "valid_stock": valid_stock,
        "has_stock": has_stock,
        "stock_status": stock_status,
        "js_stock": js_stock,
        "gd_stock": gd_stock,
        "smt_stock": smt_stock,
        "total_stock": total_stock,
        "transit_num": transit_num,
        # 价格
        "price": price,
        "is_discount": is_discount,
        "coupon_amount": coupon_amount,
        "coupon_threshold": coupon_threshold,
        # 采购
        "moq": moq,
        "max_buy": max_buy,
        "batch_limit": batch_limit,
        "product_unit": product_unit,
        "unseal": unseal,
        # 封装
        "package": package,
        "min_encap_unit": min_encap_unit,
        "min_encap_num": min_encap_num,
        "convesion_ratio": convesion_ratio,
        # 技术参数
        "attributes": attributes,
        # 数据手册
        "datasheet_url": datasheet_url,
        "datasheet_list": datasheet_list,
        # 图片
        "image_url": img_url,
        "big_image_url": big_img_url,
        # 链接
        "detail_url": detail_url,
        # 描述
        "description": description,
        "remark": remark,
        # SMT / 等级
        "smt_label": smt_label,
        "product_cycle": product_cycle,
        "grade_plate_name": grade_plate_name,
        "grade_plate_id": grade_plate_id,
        # 销售
        "recent_sales": recent_sales,
        "is_hot": is_hot,
        "is_present": is_present,
        "is_old_batch": is_old_batch,
        "rohs_label": rohs_label,
    }
