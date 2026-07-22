"""
分类页解析器

HTML → 分类树，纯函数，不涉及网络请求。
"""

import urllib.parse
from typing import List, Dict, Any

from bs4 import BeautifulSoup


def parse_categories(html: str) -> List[Dict[str, Any]]:
    """
    解析 ICKEY 首页分类导航，提取三级分类树。

    Args:
        html: 首页 HTML 文本

    Returns:
        分类树列表，格式:
        [
            {
                "top_category": "电阻/电容/磁性器件",
                "sub_categories": [
                    {
                        "name": "电阻",
                        "items": [
                            {"name": "通孔电阻", "cate_id": "010101", "url": "..."},
                            ...
                        ]
                    },
                    ...
                ]
            },
            ...
        ]
    """
    soup = BeautifulSoup(html, "html.parser")
    categories: List[Dict[str, Any]] = []

    for cate_li in soup.select("li.cateMenu"):
        # 一级分类名称
        top_cate_el = cate_li.select_one(".operation-category")
        if not top_cate_el:
            continue
        top_category = top_cate_el.get_text(strip=True)

        # 二级 + 三级菜单容器
        menu_div = cate_li.select_one(".module-menu")
        if not menu_div:
            continue

        sub_categories: List[Dict[str, Any]] = []
        titles = menu_div.select(".item-title h3")
        menus = menu_div.select("ul.main-menu")

        # 配对处理二级分类（h3）与三级分类（ul.main-menu）
        for sub_title, main_menu in zip(titles, menus):
            sec_name = sub_title.get_text(strip=True)
            leaf_items: List[Dict[str, str]] = []

            for a_tag in main_menu.select("a.menu-detail-item"):
                href = a_tag.get("href", "")
                cate_id = _extract_cate_id(href)

                leaf_items.append({
                    "name": a_tag.get_text(strip=True),
                    "cate_id": cate_id,
                    "url": href,
                })

            sub_categories.append({
                "name": sec_name,
                "items": leaf_items,
            })

        categories.append({
            "top_category": top_category,
            "sub_categories": sub_categories,
        })

    return categories


def _extract_cate_id(url: str) -> str:
    """从 URL 查询参数中提取 cate_id"""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    return params.get("cate_id", [""])[0]
