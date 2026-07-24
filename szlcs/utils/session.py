"""
HTTP Session 管理

创建统一的 requests.Session，配置重试、超时等。
同时提供立创商城 (szlcsc.com) 的反爬虫 Cookie 绕过功能。

list.szlcsc.com 使用 RC4 + Base64 的 JS 挑战来验证浏览器。
此模块实现了对应的 Cookie 生成算法。
"""

import re
import base64
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .headers import get_headers
from .logger import get_logger

logger = get_logger(__name__)

# RC4 加密密钥（经过 JS 数组 shuffle 后 deobf(0x81) 的值）
_RC4_KEY = b"tg09It3*9h"


def _rc4_encrypt(key: bytes, data: bytes) -> bytes:
    """
    标准 RC4 加密算法。

    Args:
        key: 加密密钥
        data: 待加密数据

    Returns:
        加密后的字节串
    """
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) % 256
        S[i], S[j] = S[j], S[i]

    i = j = 0
    result = []
    for char in data:
        i = (i + 1) % 256
        j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        result.append(char ^ S[(S[i] + S[j]) % 256])

    return bytes(result)


def generate_anti_bot_cookie(html: str) -> Optional[tuple]:
    """
    从反爬虫 JS 挑战页面中提取参数并生成绕过 Cookie。

    挑战页面格式:
        var _xvasu = 1104102766;
        var _xvtsc = 300;
        var _xvpfs = "tws2_";
        var _xvpts = 1784880230.125;

    Cookie 算法:
        tws2_{_xvasu} = Base64(RC4(key, "{_xvpts}:{_xvasu}"))

    Args:
        html: 挑战页面的 HTML 文本

    Returns:
        (cookie_name, cookie_value) 或 None（如果提取失败）
    """
    xvpts_match = re.search(r"var _xvpts\s*=\s*([\d.]+);", html)
    xvasu_match = re.search(r"var _xvasu\s*=\s*(\d+);", html)

    if not xvpts_match or not xvasu_match:
        return None

    xvpts = xvpts_match.group(1)
    xvasu = xvasu_match.group(1)

    data = f"{xvpts}:{xvasu}"
    encrypted = _rc4_encrypt(_RC4_KEY, data.encode())
    cookie_val = base64.b64encode(encrypted).decode()
    cookie_name = f"tws2_{xvasu}"

    logger.debug(f"Anti-bot cookie generated: {cookie_name}={cookie_val[:20]}...")
    return cookie_name, cookie_val


def _is_challenge_page(html: str) -> bool:
    """判断是否为反爬虫 JS 挑战页面"""
    return "var _xvasu" in html and "<body></body>" in html


def bypass_anti_bot(session: requests.Session, url: str, timeout: int = 30) -> bool:
    """
    检测并绕过 list.szlcsc.com 的反爬虫 JS 挑战。

    流程:
        1. 请求目标 URL
        2. 如果返回挑战页面，提取参数并生成 Cookie
        3. 设置 Cookie 后重新请求验证

    Args:
        session: requests.Session 实例
        url: 目标 URL
        timeout: 请求超时（秒）

    Returns:
        是否成功绕过
    """
    try:
        resp = session.get(url, timeout=timeout)

        if not _is_challenge_page(resp.text):
            return True  # 无需绕过

        cookie = generate_anti_bot_cookie(resp.text)
        if not cookie:
            logger.warning("无法从挑战页面提取 Cookie 参数")
            return False

        cookie_name, cookie_val = cookie
        session.cookies.set(cookie_name, cookie_val, domain=".szlcsc.com")
        time.sleep(0.1)  # 短暂延迟

        # 验证绕过是否成功
        resp2 = session.get(url, timeout=timeout)
        if _is_challenge_page(resp2.text):
            logger.warning("反爬虫绕过失败，Cookie 未生效")
            return False

        logger.debug("反爬虫 Cookie 绕过成功")
        return True

    except Exception as e:
        logger.error(f"反爬虫绕过异常: {e}")
        return False


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
