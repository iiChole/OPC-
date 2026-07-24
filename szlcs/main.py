"""
立创商城 (SZLCSC) 电子元器件爬虫 — 主入口

流程:
    category → list → detail → merge

用法:
    python main.py --step all           # 全流程
    python main.py --step category      # 只爬分类
    python main.py --step list          # 只爬列表（需先有 categories.json）
    python main.py --step detail        # 只爬详情（需先有 products.json）
    python main.py --step merge         # 只合并数据
"""

import argparse
import os
import sys

from utils.session import create_session
from utils.logger import get_logger
from spiders import CategorySpider, ListSpider, DetailSpider
from merge import merge

logger = get_logger(__name__)

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# 数据文件路径
CATEGORIES_PATH = os.path.join(DATA_DIR, "categories.json")
PRODUCTS_PATH = os.path.join(DATA_DIR, "products.jsonl")
DETAILS_PATH = os.path.join(DATA_DIR, "product_details.jsonl")
FINAL_PATH = os.path.join(DATA_DIR, "products_final.json")

# 断点文件路径
LIST_CHECKPOINT = os.path.join(DATA_DIR, "checkpoint_list.txt")
DETAIL_CHECKPOINT = os.path.join(DATA_DIR, "checkpoint_detail.txt")


def ensure_data_dir():
    """确保 data 目录存在"""
    os.makedirs(DATA_DIR, exist_ok=True)


def step_category(session):
    """Step 1: 采集分类"""
    logger.info("=" * 50)
    logger.info("Step 1: 采集分类")
    logger.info("=" * 50)
    spider = CategorySpider(session)
    spider.run(CATEGORIES_PATH)


def step_list(session):
    """Step 2: 采集商品列表"""
    logger.info("=" * 50)
    logger.info("Step 2: 采集商品列表")
    logger.info("=" * 50)
    spider = ListSpider(session)
    spider.crawl(CATEGORIES_PATH, PRODUCTS_PATH, LIST_CHECKPOINT)


def step_detail(session):
    """Step 3: 采集商品详情"""
    logger.info("=" * 50)
    logger.info("Step 3: 采集商品详情")
    logger.info("=" * 50)
    spider = DetailSpider(session)
    spider.crawl(PRODUCTS_PATH, DETAILS_PATH, DETAIL_CHECKPOINT)


def step_merge():
    """Step 4: 合并数据"""
    logger.info("=" * 50)
    logger.info("Step 4: 合并数据")
    logger.info("=" * 50)
    merge(PRODUCTS_PATH, DETAILS_PATH, FINAL_PATH)


def main():
    parser = argparse.ArgumentParser(
        description="立创商城 (SZLCSC) 电子元器件爬虫",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --step all        # 全流程采集
  python main.py --step category   # 只采集分类
  python main.py --step list       # 只采集列表
  python main.py --step detail     # 只采集详情
  python main.py --step merge      # 只合并数据
        """,
    )
    parser.add_argument(
        "--step",
        choices=["all", "category", "list", "detail", "merge"],
        default="all",
        help="执行步骤 (默认: all)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="请求重试次数 (默认: 3)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="请求超时秒数 (默认: 30)",
    )

    args = parser.parse_args()

    ensure_data_dir()

    # 创建共享 Session
    session = create_session(retries=args.retries, timeout=args.timeout)

    try:
        if args.step == "category":
            step_category(session)
        elif args.step == "list":
            step_list(session)
        elif args.step == "detail":
            step_detail(session)
        elif args.step == "merge":
            step_merge()
        elif args.step == "all":
            step_category(session)
            step_list(session)
            step_detail(session)
            step_merge()
    except KeyboardInterrupt:
        logger.info("用户中断，已保存当前进度（断点续爬生效中）")
        sys.exit(0)
    except Exception as e:
        logger.error(f"运行出错: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
