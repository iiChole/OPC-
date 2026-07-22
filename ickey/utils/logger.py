"""
日志模块

统一的日志配置：同时输出到控制台和文件。
支持不同级别，方便调试和监控。
"""

import logging
import sys
from pathlib import Path


def get_logger(
    name: str,
    log_file: str = "spider.log",
    level: int = logging.INFO,
) -> logging.Logger:
    """
    创建并配置 logger 实例。

    Args:
        name: logger 名称（通常传 __name__）
        log_file: 日志文件路径
        level: 日志级别

    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    logger.setLevel(level)

    # 日志格式
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # 文件 handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
