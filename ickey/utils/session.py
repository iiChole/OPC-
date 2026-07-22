"""
HTTP Session 管理

创建统一的 requests.Session，配置重试、超时等。
后续可在此扩展代理、Cookie 持久化等。
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .headers import get_headers


def create_session(
    retries: int = 3,
    backoff_factor: float = 1.0,
    timeout: int = 30,
) -> requests.Session:
    """
    创建配置好的 requests.Session。

    Args:
        retries: 最大重试次数
        backoff_factor: 重试退避因子（延迟 = backoff_factor * (2^(retry-1))）
        timeout: 默认请求超时（秒）

    Returns:
        配置好的 Session 实例
    """
    session = requests.Session()

    # 设置默认请求头
    session.headers.update(get_headers())

    # 配置重试策略
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # 保存默认超时供后续使用
    session.timeout = timeout

    return session
