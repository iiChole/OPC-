"""
HTTP 请求头管理 — TI.com

TI.com API 反爬较轻，只需基本浏览器伪装 + Referer。
"""

from typing import Dict, Optional

_BASE_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "content-type": "application/json",
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
