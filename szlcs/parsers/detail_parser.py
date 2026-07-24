"""
商品详情页解析器

详情页 HTML → 商品属性字典，纯函数。

由于 item.szlcsc.com 使用阿里云 WAF 保护，详情页可能无法直接通过
requests 访问。此模块支持两种数据源:

1. 详情页 HTML（优先）: 从 item.szlcsc.com/{productId}.html 解析
2. 列表页数据（回退）: 列表页的 paramLinkedMap 已经包含了丰富的属性数据

解析目标:
    ① table 中的属性表 → attributes (key-value)
    ② <title> → title
    ③ a[href$=".pdf"] → datasheet_url
    ④ img → image_url
"""

import re
from typing import Dict, Optional

from bs4 import BeautifulSoup


def parse_detail(html: str) -> Dict:
    """
    解析商品详情页，提取所有数据。

    Args:
        html: 详情页 HTML

    Returns:
        {
            "sku": "C6119867",
            "title": "100nF ±10% 50V",
            "attributes": {"容值": "100nF", "精度": "±10%", ...},
            "datasheet_url": "...",
            "image_url": "...",
        }
    """
    soup = BeautifulSoup(html, "html.parser")

    sku = _parse_sku(soup)
    title = _parse_title(soup)
    attributes = _parse_attr_table(soup)
    datasheet_url = _parse_datasheet(soup)
    image_url = _parse_image(soup)

    return {
        "sku": sku,
        "title": title,
        "attributes": attributes,
        "datasheet_url": datasheet_url,
        "image_url": image_url,
    }


# ---------- 子解析函数 ----------

def _parse_sku(soup: BeautifulSoup) -> Optional[str]:
    """从详情页提取 SKU (productCode)"""
    # 从 canonical URL 中提取
    canonical = soup.select_one("link[rel='canonical']")
    if canonical:
        href = canonical.get("href", "")
        m = re.search(r"/product(?:s)?/(\d+)\.html", href)
        if m:
            return m.group(1)

    # 从 meta 标签
    for sel in ["[data-sku]", "meta[itemprop='sku']"]:
        el = soup.select_one(sel)
        if el:
            if el.name == "meta":
                val = el.get("content", "")
            else:
                val = el.get("data-sku", "") or el.get_text(strip=True)
            if val:
                return val

    # 从 __NEXT_DATA__ JSON
    import json
    next_data = soup.select_one("#__NEXT_DATA__")
    if next_data:
        try:
            data = json.loads(next_data.get_text(strip=True))
            product_code = (
                data.get("props", {})
                .get("pageProps", {})
                .get("productDetail", {})
                .get("productCode", "")
            )
            if product_code:
                return product_code
        except json.JSONDecodeError:
            pass

    return None


def _parse_title(soup: BeautifulSoup) -> Optional[str]:
    """从详情页提取商品标题"""
    # <title> 标签
    title_tag = soup.select_one("title")
    if title_tag:
        text = title_tag.get_text(strip=True)
        # 格式: "CGA0603X7R104K500JT_立创商城"
        # 取下划线或分隔符前的部分
        for sep in ["_", "-", "|", "("]:
            if sep in text:
                text = text.split(sep)[0]
        if text:
            return text.strip()

    # h1
    h1 = soup.select_one("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text:
            return text

    return None


def _parse_attr_table(soup: BeautifulSoup) -> Dict[str, str]:
    """
    解析属性表。

    立创商城详情页属性表通常是 <table> 或 <div> 中
    以 key-value 配对形式展示。

    通用遍历，不硬编码字段名。
    """
    attrs: Dict[str, str] = {}

    # 查找属性表格
    for table in soup.select("table"):
        rows = table.select("tr")
        for row in rows:
            tds = row.select("td")
            if len(tds) >= 2:
                key = tds[0].get_text(strip=True)
                value = tds[1].get_text(strip=True)
                if key and key not in ("产品属性", "属性值", "属性", "值"):
                    attrs[key] = value

    # 也尝试从 __NEXT_DATA__ 提取属性
    if not attrs:
        import json
        next_data = soup.select_one("#__NEXT_DATA__")
        if next_data:
            try:
                data = json.loads(next_data.get_text(strip=True))
                param_map = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("productDetail", {})
                    .get("paramLinkedMap", {})
                )
                if param_map:
                    for key, value in param_map.items():
                        if isinstance(value, str):
                            attrs[key] = value
                        elif isinstance(value, dict):
                            attrs[key] = value.get("paramValue", str(value))
            except json.JSONDecodeError:
                pass

    return attrs


def _parse_datasheet(soup: BeautifulSoup) -> Optional[str]:
    """提取 datasheet PDF 链接"""
    # 直接找 PDF 链接
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if href.endswith(".pdf") or "datasheet" in href.lower():
            if href.startswith("//"):
                return "https:" + href
            if href.startswith("/"):
                return "https://datasheet.lcsc.com" + href
            return href

    # 从 __NEXT_DATA__ 提取
    import json
    next_data = soup.select_one("#__NEXT_DATA__")
    if next_data:
        try:
            data = json.loads(next_data.get_text(strip=True))
            file_types = (
                data.get("props", {})
                .get("pageProps", {})
                .get("productDetail", {})
                .get("fileTypeVOList", [])
            )
            for ft in file_types:
                if ft.get("fileType") == "pdf_property":
                    details = ft.get("detailVOList", [])
                    if details:
                        path = details[0].get("fileUrl", "")
                        if path:
                            return f"https://datasheet.lcsc.com{path}" if path.startswith("/") else path
                        break
        except json.JSONDecodeError:
            pass

    return None


def _parse_image(soup: BeautifulSoup) -> Optional[str]:
    """提取商品主图"""
    # 产品大图
    for sel in ["[class*='product-img'] img", "[class*='gallery'] img", ".detail-img img", "img[class*='main']"]:
        el = soup.select_one(sel)
        if el:
            src = el.get("src", "")
            if src:
                if src.startswith("//"):
                    return "https:" + src
                return src

    # 任何包含 alimg 的图片
    img = soup.select_one("img[src*='alimg']")
    if img:
        src = img.get("src", "")
        if src.startswith("//"):
            return "https:" + src
        return src

    return None
