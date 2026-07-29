from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agents.decision import WebsiteDecisionAgent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="电子元器件网站决策与查询工具")
    parser.add_argument(
        "--decide-site",
        default="",
        metavar="URL_OR_NAME",
        help="仅分析并归类网站，不发起网络请求或执行爬取",
    )
    parser.add_argument(
        "--plan-site",
        default="",
        metavar="URL",
        help="有限探索指定网站并输出内部全量抓取计划，不抓取商品详情",
    )
    parser.add_argument(
        "--crawl-site",
        default="",
        metavar="URL",
        help="端到端运行决策、robots、计划、目录、商品和验证 Agent",
    )
    parser.add_argument("--input", default="", help="自然语言输入，例如：查询: STM32F103C8T6. 需要: 价格, 库存")
    parser.add_argument("--query", default="", help="型号或查询词")
    parser.add_argument("--fields", default="", help="逗号分隔字段；不传则抓取全部信息")
    parser.add_argument("--sites", default="ickey,szlcsc,ti", help="逗号分隔站点")
    parser.add_argument("--output-dir", default=str(Path(__file__).parent / "data"))
    parser.add_argument(
        "--run-state-root",
        default=str(Path(__file__).parent / "run_state"),
        help="端到端爬取的 checkpoint、日志和最终输出根目录",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--max-results", type=int, default=10)
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
    parser.add_argument("--no-browser", action="store_true", help="禁用 Playwright 回退")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.decide_site:
        decision = WebsiteDecisionAgent().decide(args.decide_site)
        print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.crawl_site:
        try:
            from .orchestration.full_site import FullSiteCrawlCoordinator

            result = FullSiteCrawlCoordinator(
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
            ).run(args.crawl_site)
        except (ValueError, RuntimeError, ImportError) as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.status == "complete" else 1

    if args.plan_site:
        try:
            from .agents.crawl_plan import CrawlPlanAgent

            plan = CrawlPlanAgent(
                timeout=args.timeout,
                retries=args.retries,
                delay=args.delay,
                browser_enabled=not args.no_browser,
                headless=not args.headed,
            ).run(args.plan_site)
        except (ValueError, RuntimeError, ImportError) as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0 if plan.status != "failed" else 1

    from .agent import ComponentSearchAgent
    from .intent import analyze_input

    fields = [item.strip() for item in args.fields.replace("，", ",").split(",") if item.strip()]
    sites = [item.strip() for item in args.sites.split(",") if item.strip()]
    try:
        request = analyze_input(args.input, query=args.query, fields=fields or None)
        agent = ComponentSearchAgent(
            sites=sites,
            output_dir=args.output_dir,
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
            browser_enabled=not args.no_browser,
            headless=not args.headed,
            max_results_per_site=args.max_results,
        )
        report = agent.run(request)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.products else 1


if __name__ == "__main__":
    sys.exit(main())
