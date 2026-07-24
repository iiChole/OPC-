"""
HTTP 请求头管理

统一管理所有请求的 Headers，方便后续扩展（如添加 Referer、Cookie 等）。
"""

from typing import Dict, Optional

# 基础请求头 — 模拟正常浏览器访问
_BASE_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
}


def get_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    获取请求头，允许调用方追加自定义字段。

    Args:
        extra: 额外的请求头字段，会覆盖默认值

    Returns:
        合并后的请求头字典
    """
    headers = _BASE_HEADERS.copy()
    if extra:
        headers.update(extra)
    return headers
