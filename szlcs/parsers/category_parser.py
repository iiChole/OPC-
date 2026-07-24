"""
分类页解析器

HTML → 分类树，纯函数，不涉及网络请求。

解析目标:
    https://www.szlcsc.com/catalog.html

页面结构:
    section.ml-[20px].w-[100%]
      └── ul.w-[100%]                          ← 一级分类容器
            └── li
                  └── div (一级分类标题 + 链接)
                  └── ul.flex.flex-wrap         ← 二级/三级分类容器
                        └── li
                              └── a (三级分类链接: /catalog/{id}.html)
"""

import re
import urllib.parse
from typing import List, Dict, Any

from bs4 import BeautifulSoup


def parse_categories(html: str) -> List[Dict[str, Any]]:
    """
    解析立创商城分类页，提取分类层级。

    结构说明:
        - 页面以 section 包含所有分类
        - 每个一级分类是一个 div.mt-[15px]（包含分类名和 list.szlcsc.com 链接）
        - 其后的 ul.flex.flex-wrap 包含所有三级分类链接

    Args:
        html: catalog.html 的 HTML 文本

    Returns:
        分类列表，格式:
        [
            {
                "top_category": "电容",
                "catalog_id": "312",
                "items": [
                    {"name": "贴片电容(MLCC)", "catalog_id": "313", "url": "..."},
                    ...
                ]
            },
            ...
        ]
    """
    soup = BeautifulSoup(html, "html.parser")
    categories: List[Dict[str, Any]] = []

    # 分类容器在 section 中
    section = soup.select_one("section.ml-\\[20px\\].w-\\[100\\%\\]")
    if not section:
        # 尝试更宽泛的选择器
        section = soup.select_one("section")
    if not section:
        return categories

    # 一级分类: div.mt-[15px] 中包含分类名和链接
    # 其后的 ul.flex.flex-wrap 中包含三级分类
    top_divs = section.select("div.mt-\\[15px\\]")

    for top_div in top_divs:
        # 提取一级分类名称和链接
        link = top_div.select_one("a[href]")
        if not link:
            continue

        top_name = link.get_text(strip=True)
        # 去掉名称中的数量后缀，如 "电容（991,616）" → "电容"
        top_name_clean = re.sub(r"[（(][\d,]+[）)]$", "", top_name).strip()
        top_href = link.get("href", "")
        top_catalog_id = _extract_catalog_id(top_href) or ""

        # 查找三级分类: 后续的 ul.flex.flex-wrap 中的链接
        items: List[Dict[str, str]] = []

        # 在当前一级分类 div 的父级 li 中查找三级分类
        parent_li = top_div.parent
        if parent_li:
            sub_ul = parent_li.select_one("ul.flex.flex-wrap")
            if sub_ul:
                for a_tag in sub_ul.select("a[href*='/catalog/']"):
                    href = a_tag.get("href", "")
                    item_name = a_tag.get_text(strip=True)
                    item_name_clean = re.sub(r"[（(][\d,]+[）)]$", "", item_name).strip()
                    catalog_id = _extract_catalog_id(href) or ""

                    items.append({
                        "name": item_name_clean,
                        "catalog_id": catalog_id,
                        "url": href,
                    })

        if items:
            categories.append({
                "top_category": top_name_clean,
                "catalog_id": top_catalog_id,
                "items": items,
            })

    return categories


def _extract_catalog_id(url: str) -> str:
    """从 URL 中提取 catalog ID，如 /catalog/313.html → 313"""
    m = re.search(r"/catalog/(\d+)\.html", url)
    if m:
        return m.group(1)
    # 也尝试从查询参数提取
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    return params.get("catalogId", [""])[0]
