"""One-command entry point for the end-to-end multi-agent site workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .orchestration.full_site import FullSiteCrawlCoordinator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="端到端运行网站多 Agent 爬取流程",
    )
    parser.add_argument("url", help="目标网站 URL")
    parser.add_argument(
        "--run-state-root",
        default=str(Path(__file__).parent / "run_state"),
        help="checkpoint、日志和最终输出根目录",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--max-pages-per-category", type=int, default=100000)
    parser.add_argument(
        "--max-categories",
        type=int,
        default=0,
        help="0 表示全量；正数用于端到端样本运行",
    )
    parser.add_argument("--max-workflow-attempts", type=int, default=2)
    parser.add_argument(
        "--field-completeness-threshold",
        type=float,
        default=1.0,
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--headed", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    coordinator = FullSiteCrawlCoordinator(
        run_state_root=args.run_state_root,
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        browser_enabled=not args.no_browser,
        headless=not args.headed,
        max_concurrency=args.max_concurrency,
        max_pages_per_category=args.max_pages_per_category,
        max_categories=args.max_categories,
        max_workflow_attempts=args.max_workflow_attempts,
        field_completeness_threshold=args.field_completeness_threshold,
    )
    result = coordinator.run(args.url)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["build_parser", "main"]
