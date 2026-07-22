"""
商品详情页解析器

详情页 HTML → 商品属性字典，纯函数。

解析目标:
    ① table.detail-table-two → attributes (key-value，通用遍历)
    ② <title> → title
    ③ a[href$=".pdf"] → datasheet_url
    ④ img → image_url
"""

import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup


def parse_detail(html: str) -> Dict:
    """
    解析商品详情页，提取所有数据。

    Args:
        html: 详情页 HTML

    Returns:
        {
            "sku": "1003001442049056",
            "title": "CFR0W4J0300A50",
            "attributes": {"电阻值": "30Ω", "精度": "±5%", ...},
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
    """从详情页提取 SKU"""
    # 从 canonical URL 中提取
    canonical = soup.select_one("link[rel='canonical']")
    if canonical:
        href = canonical.get("href", "")
        match = re.search(r"/detail/(\d+)/", href)
        if match:
            return match.group(1)

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

    return None


def _parse_title(soup: BeautifulSoup) -> Optional[str]:
    """从详情页提取商品标题"""
    # <title> 标签
    title_tag = soup.select_one("title")
    if title_tag:
        text = title_tag.get_text(strip=True)
        # 格式: "CFR0W4J0300A50（云汉在库）采购_价格_数据手册-云汉芯城 ICkey.cn"
        # 取括号前或下划线前的部分作为标题
        if "）" in text:
            text = text.split("）")[0] + "）"
        elif "_" in text:
            text = text.split("_")[0]
        if text:
            return text

    # h1
    h1 = soup.select_one("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text:
            return text

    return None


def _parse_attr_table(soup: BeautifulSoup) -> Dict[str, str]:
    """
    解析属性表 — table.detail-table-two。

    结构:
        <table class="detail-table detail-table-two">
            <tr>  <!-- header: 产品属性 | 属性值 -->  </tr>
            <tr>  <td>KEY</td> <td>VALUE</td>  </tr>
            ...
        </table>

    通用遍历，不硬编码字段名。
    """
    attrs: Dict[str, str] = {}

    table = soup.select_one("table.detail-table-two")
    if table is None:
        return attrs

    rows = table.select("tbody tr")
    for row in rows:
        tds = row.select("td.table-title")
        if len(tds) >= 2:
            key = tds[0].get_text(strip=True)
            value = tds[1].get_text(strip=True)
            # 跳过标题行
            if key and key not in ("产品属性", "属性值"):
                attrs[key] = value

    return attrs


def _parse_datasheet(soup: BeautifulSoup) -> Optional[str]:
    """提取 datasheet PDF 链接"""
    # 直接找 PDF 链接
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if href.endswith(".pdf") or "datasheet" in href.lower():
            if href.startswith("//"):
                return "https:" + href
            return href

    # 找 datasheet-wrapper 区域
    wrapper = soup.select_one("[class*='datasheet']")
    if wrapper:
        a = wrapper.select_one("a[href]")
        if a:
            href = a.get("href", "")
            if href:
                return href

    return None


def _parse_image(soup: BeautifulSoup) -> Optional[str]:
    """提取商品主图"""
    # 产品大图
    for sel in ["[class*='product-img'] img", "[class*='gallery'] img", ".detail-img img"]:
        el = soup.select_one(sel)
        if el:
            src = el.get("src", "")
            if src:
                if src.startswith("//"):
                    return "https:" + src
                return src

    # 任何包含 itempic 的图片
    img = soup.select_one("img[src*='itempic']")
    if img:
        src = img.get("src", "")
        if src.startswith("//"):
            return "https:" + src
        return src

    return None
