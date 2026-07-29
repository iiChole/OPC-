"""Enumerate every catalog category and produce resumable ProductSeed records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..catalog.checkpoint import (
    CheckpointStore,
    JsonlJournal,
    ProductSeedJournal,
)
from ..catalog.models import (
    AgentHandoff,
    CatalogCheckpoint,
    CatalogIssue,
    CatalogResult,
    CategoryTask,
    NextPaginator,
    PaginationState,
    ProductSeed,
    plan_fingerprint,
)
from ..catalog.pagination import (
    advance_pagination,
    count_anomaly_reason,
    initial_pagination_state,
    request_url,
    select_traversal_mode,
)
from ..catalog.parser import CatalogParser
from ..models import FetchResult, utc_now
from ..planning.models import CrawlPlan, FetchTool
from ..planning.page_analysis import same_site


@dataclass
class _RunContext:
    plan: CrawlPlan
    checkpoint: CatalogCheckpoint
    checkpoint_store: CheckpointStore
    seed_journal: ProductSeedJournal
    task_journal: JsonlJournal
    issue_journal: JsonlJournal
    seeds: List[ProductSeed]
    seen_product_keys: Set[str]
    issues: List[CatalogIssue]
    duplicate_product_count: int = 0
    skipped_category_count: int = 0


class CatalogAgent:
    """Traverse categories and pagination without fetching product details."""

    def __init__(
        self,
        fetch_tool: Optional[FetchTool] = None,
        next_paginator: Optional[NextPaginator] = None,
        parser: Optional[CatalogParser] = None,
        run_state_dir: Path | str = "run_state",
        traversal_mode: str = "auto",
        prefer_parallel: bool = False,
        bfs_threshold: int = 20,
        max_pages_per_category: int = 10_000,
        timeout: int = 30,
        retries: int = 2,
        delay: float = 0.35,
        browser_enabled: bool = True,
        headless: bool = True,
    ) -> None:
        if fetch_tool is None:
            from ..tools import AdaptiveFetchTool

            fetch_tool = AdaptiveFetchTool(
                timeout=timeout,
                retries=retries,
                delay=delay,
                browser_enabled=browser_enabled,
                headless=headless,
            )
        self.fetch_tool = fetch_tool
        browser = getattr(fetch_tool, "browser", None)
        self.next_paginator = (
            next_paginator
            or (browser if hasattr(browser, "paginate_next") else None)
        )
        self.parser = parser or CatalogParser()
        self.run_state_dir = Path(run_state_dir)
        self.requested_traversal_mode = traversal_mode
        self.prefer_parallel = prefer_parallel
        self.bfs_threshold = max(1, bfs_threshold)
        self.max_pages_per_category = max(1, max_pages_per_category)

    def run(self, plan: CrawlPlan | Dict[str, Any]) -> CatalogResult:
        if isinstance(plan, dict):
            plan = CrawlPlan.from_dict(plan)
        paths = self._state_paths()
        checkpoint_store = CheckpointStore(paths["checkpoint"])
        seed_journal = ProductSeedJournal(paths["seeds"])
        task_journal = JsonlJournal(paths["tasks"])
        issue_journal = JsonlJournal(paths["issues"])
        seed_journal.ensure_exists()
        task_journal.ensure_exists()
        issue_journal.ensure_exists()

        roots = [
            CategoryTask.from_candidate(candidate)
            for candidate in plan.categories
        ]
        if not roots:
            roots = [CategoryTask(
                name="catalog_root",
                url=plan.start_url,
                source="crawl_plan_fallback",
            )]
        traversal_mode = select_traversal_mode(
            self.requested_traversal_mode,
            len(roots),
            prefer_parallel=self.prefer_parallel,
            bfs_threshold=self.bfs_threshold,
        )
        fingerprint = plan_fingerprint(_fingerprint_payload(plan))
        issues: List[CatalogIssue] = []

        if plan.status == "failed":
            issue = CatalogIssue(
                code="crawl_plan_not_ready",
                message="CrawlPlan 状态为 failed，CatalogAgent 未开始枚举",
                url=plan.start_url,
                retryable=True,
            )
            issues.append(issue)
            issue_journal.append(_issue_event(issue))
            handoff = AgentHandoff(
                target_agent="CrawlPlanAgent",
                reason="crawl_plan_not_ready",
                available=True,
                payload={
                    "action": "rebuild_crawl_plan",
                    "site_key": plan.site_key,
                    "start_url": plan.start_url,
                },
            )
            task_journal.append(_handoff_event(handoff))
            return CatalogResult(
                status="replan_required",
                traversal_mode=traversal_mode,
                product_seeds=[],
                categories=[],
                completed_category_count=0,
                skipped_category_count=0,
                duplicate_product_count=0,
                checkpoint_path=str(paths["checkpoint"]),
                product_seed_path=str(paths["seeds"]),
                issues=issues,
                handoff=handoff,
            )

        checkpoint = self._load_or_create_checkpoint(
            checkpoint_store,
            plan,
            roots,
            traversal_mode,
            fingerprint,
            issues,
            issue_journal,
        )
        traversal_mode = checkpoint.traversal_mode
        seeds, seen_product_keys, journal_duplicates = self._load_seed_journal(
            seed_journal,
            plan.site_key,
        )
        checkpoint.seen_product_keys = sorted(
            set(checkpoint.seen_product_keys) | seen_product_keys
        )
        context = _RunContext(
            plan=plan,
            checkpoint=checkpoint,
            checkpoint_store=checkpoint_store,
            seed_journal=seed_journal,
            task_journal=task_journal,
            issue_journal=issue_journal,
            seeds=seeds,
            seen_product_keys=seen_product_keys,
            issues=issues,
            duplicate_product_count=journal_duplicates,
        )
        checkpoint_store.save(checkpoint)
        task_journal.append({
            "event": "catalog_run_started",
            "agent": "CatalogAgent",
            "site_key": plan.site_key,
            "traversal_mode": traversal_mode,
            "pending_category_count": len(checkpoint.pending_categories),
            "completed_category_count": len(checkpoint.completed_category_keys),
            "resumed_product_seed_count": len(seeds),
            "timestamp": utc_now(),
        })

        while checkpoint.pending_categories:
            category = self._peek_category(checkpoint)
            if category.key in checkpoint.completed_category_keys:
                self._remove_pending_category(checkpoint, category.key)
                context.skipped_category_count += 1
                checkpoint_store.save(checkpoint)
                continue

            state = checkpoint.category_states.get(category.key)
            if state is None:
                state = initial_pagination_state(plan, category)
                checkpoint.category_states[category.key] = state
            task_journal.append({
                "event": "category_started",
                "agent": "CatalogAgent",
                "category": category.to_dict(),
                "pagination": state.to_dict(),
                "timestamp": utc_now(),
            })

            if state.mode == "next_click":
                handoff = self._crawl_next_click(context, category, state)
            else:
                handoff = self._crawl_request_pages(context, category, state)
            if handoff is not None:
                return self._result_for_handoff(context, handoff)

            anomaly = count_anomaly_reason(state)
            if anomaly:
                handoff = self._validation_handoff(
                    context,
                    category,
                    state,
                    code="category_count_mismatch",
                    message=anomaly,
                )
                return self._result_for_handoff(context, handoff)

            state.complete = True
            if not state.stop_reason:
                state.stop_reason = "pagination_complete"
            if category.key not in checkpoint.completed_category_keys:
                checkpoint.completed_category_keys.append(category.key)
            self._remove_pending_category(checkpoint, category.key)
            record = checkpoint.category_records.setdefault(
                category.key,
                category.to_dict(),
            )
            record.update({
                "status": "complete",
                "pages_seen": state.pages_seen,
                "product_count": state.product_count,
                "reported_total": state.reported_total,
                "stop_reason": state.stop_reason,
            })
            checkpoint_store.save(checkpoint)
            task_journal.append({
                "event": "category_completed",
                "agent": "CatalogAgent",
                "category_key": category.key,
                "pages_seen": state.pages_seen,
                "product_count": state.product_count,
                "stop_reason": state.stop_reason,
                "timestamp": utc_now(),
            })

        task_journal.append({
            "event": "catalog_run_completed",
            "agent": "CatalogAgent",
            "site_key": plan.site_key,
            "product_seed_count": len(context.seeds),
            "completed_category_count": len(checkpoint.completed_category_keys),
            "timestamp": utc_now(),
        })
        return self._build_result(context, status="complete")

    def _crawl_request_pages(
        self,
        context: _RunContext,
        category: CategoryTask,
        state: PaginationState,
    ) -> Optional[AgentHandoff]:
        remaining_pages = self.max_pages_per_category - state.pages_seen
        if remaining_pages <= 0:
            return self._validation_handoff(
                context,
                category,
                state,
                code="max_pages_reached",
                message="分类达到最大分页数但未满足末页条件",
            )

        for _ in range(remaining_pages):
            url = request_url(state, context.plan.pagination)
            result, error = self._safe_fetch(url)
            if error is not None:
                return self._replan_handoff(
                    context,
                    category,
                    state,
                    url,
                    error,
                )
            assert result is not None
            try:
                page = self.parser.parse(
                    result,
                    category,
                    context.plan.site_key,
                    product_list_path=context.plan.pagination.product_list_path,
                    next_cursor_path=context.plan.pagination.next_cursor_path,
                )
            except Exception as exc:
                return self._validation_handoff(
                    context,
                    category,
                    state,
                    code="product_api_parse_failed",
                    message=f"商品列表解析失败: {exc}",
                    url=result.url,
                )

            self._register_child_categories(context, category, page.child_categories)
            added, duplicates = self._persist_product_seeds(context, page.products)
            transition = advance_pagination(
                state,
                page,
                context.plan.pagination,
            )
            context.checkpoint_store.save(context.checkpoint)
            context.task_journal.append({
                "event": "catalog_page_completed",
                "agent": "CatalogAgent",
                "category_key": category.key,
                "url": result.url,
                "pagination_mode": state.mode,
                "page_number": state.page_number,
                "offset": state.offset,
                "cursor": state.cursor,
                "raw_product_count": page.raw_product_count,
                "new_product_seed_count": added,
                "duplicate_product_count": duplicates,
                "stop_reason": transition.reason,
                "timestamp": utc_now(),
            })
            if transition.anomaly:
                return self._validation_handoff(
                    context,
                    category,
                    state,
                    code=transition.reason or "pagination_anomaly",
                    message="分页未正常推进或在网站报告总数前提前结束",
                    url=result.url,
                )
            if transition.complete:
                return None

        return self._validation_handoff(
            context,
            category,
            state,
            code="max_pages_reached",
            message="分类达到最大分页数但未满足末页条件",
        )

    def _crawl_next_click(
        self,
        context: _RunContext,
        category: CategoryTask,
        state: PaginationState,
    ) -> Optional[AgentHandoff]:
        if self.next_paginator is None:
            return self._replan_handoff(
                context,
                category,
                state,
                state.base_url,
                "Next 按钮分页需要 Playwright paginate_next 工具",
            )

        resume_pages = state.pages_seen
        yielded_pages = 0
        try:
            pages = self.next_paginator.paginate_next(
                state.base_url,
                next_selector=context.plan.pagination.next_selector,
                max_pages=self.max_pages_per_category,
            )
            for page_index, result in enumerate(pages, start=1):
                yielded_pages = page_index
                if page_index <= resume_pages:
                    continue
                try:
                    page = self.parser.parse(
                        result,
                        category,
                        context.plan.site_key,
                        product_list_path=(
                            context.plan.pagination.product_list_path
                        ),
                        next_cursor_path=(
                            context.plan.pagination.next_cursor_path
                        ),
                    )
                except Exception as exc:
                    return self._validation_handoff(
                        context,
                        category,
                        state,
                        code="product_api_parse_failed",
                        message=f"商品列表解析失败: {exc}",
                        url=result.url,
                    )
                self._register_child_categories(
                    context,
                    category,
                    page.child_categories,
                )
                added, duplicates = self._persist_product_seeds(
                    context,
                    page.products,
                )
                transition = advance_pagination(
                    state,
                    page,
                    context.plan.pagination,
                )
                context.checkpoint_store.save(context.checkpoint)
                context.task_journal.append({
                    "event": "catalog_page_completed",
                    "agent": "CatalogAgent",
                    "category_key": category.key,
                    "url": result.url,
                    "pagination_mode": "next_click",
                    "page_number": page_index,
                    "raw_product_count": page.raw_product_count,
                    "new_product_seed_count": added,
                    "duplicate_product_count": duplicates,
                    "stop_reason": transition.reason,
                    "timestamp": utc_now(),
                })
                if transition.anomaly:
                    return self._validation_handoff(
                        context,
                        category,
                        state,
                        code=transition.reason or "pagination_anomaly",
                        message="Next 按钮分页出现重复页面或提前结束",
                        url=result.url,
                    )
                if transition.complete:
                    return None
        except Exception as exc:
            return self._replan_handoff(
                context,
                category,
                state,
                state.base_url,
                str(exc),
            )

        if yielded_pages < resume_pages:
            return self._validation_handoff(
                context,
                category,
                state,
                code="next_resume_position_missing",
                message="恢复时网站可遍历页数少于 checkpoint 已完成页数",
                url=state.base_url,
            )
        if yielded_pages >= self.max_pages_per_category and not state.complete:
            return self._validation_handoff(
                context,
                category,
                state,
                code="max_pages_reached",
                message="Next 按钮分页达到最大页数但仍未结束",
                url=state.base_url,
            )
        state.complete = True
        state.stop_reason = "next_link_or_control_absent"
        context.checkpoint_store.save(context.checkpoint)
        return None

    def _register_child_categories(
        self,
        context: _RunContext,
        parent: CategoryTask,
        candidates,
    ) -> None:
        checkpoint = context.checkpoint
        seen = set(checkpoint.seen_category_keys)
        for candidate in candidates:
            if candidate.url and not same_site(context.plan.start_url, candidate.url):
                continue
            child = CategoryTask.from_candidate(
                candidate,
                parent_key=parent.key,
                depth=parent.depth + 1,
            )
            if child.key == parent.key or child.key in seen:
                continue
            seen.add(child.key)
            checkpoint.seen_category_keys.append(child.key)
            checkpoint.category_records[child.key] = child.to_dict()
            if checkpoint.traversal_mode == "bfs":
                checkpoint.pending_categories.append(child)
            else:
                current_index = next(
                    (
                        index
                        for index, task in enumerate(checkpoint.pending_categories)
                        if task.key == parent.key
                    ),
                    len(checkpoint.pending_categories),
                )
                checkpoint.pending_categories.insert(current_index, child)

    def _persist_product_seeds(
        self,
        context: _RunContext,
        seeds: List[ProductSeed],
    ) -> Tuple[int, int]:
        new_seeds: List[ProductSeed] = []
        duplicates = 0
        for seed in seeds:
            if not seed.dedup_key:
                seed.assign_dedup_identity()
            if not seed.dedup_key:
                issue = CatalogIssue(
                    code="product_identity_missing",
                    message="商品缺少 SKU、Product ID 和详情 URL，无法生成去重键",
                    url=seed.source_url,
                    details={"title": seed.title},
                )
                self._record_issue(context, issue)
                continue
            if seed.dedup_key in context.seen_product_keys:
                duplicates += 1
                continue
            context.seen_product_keys.add(seed.dedup_key)
            new_seeds.append(seed)

        context.seed_journal.append(new_seeds)
        context.seeds.extend(new_seeds)
        context.duplicate_product_count += duplicates
        context.checkpoint.seen_product_keys = sorted(context.seen_product_keys)
        return len(new_seeds), duplicates

    def _replan_handoff(
        self,
        context: _RunContext,
        category: CategoryTask,
        state: PaginationState,
        url: str,
        error: str,
    ) -> AgentHandoff:
        issue = CatalogIssue(
            code="catalog_request_failed",
            message=error,
            category_key=category.key,
            url=url,
            retryable=True,
            details={"pagination": state.to_dict()},
        )
        self._record_issue(context, issue)
        context.checkpoint_store.save(context.checkpoint)
        handoff = AgentHandoff(
            target_agent="CrawlPlanAgent",
            reason="catalog_request_failed",
            available=True,
            payload={
                "action": "rebuild_crawl_plan",
                "site_key": context.plan.site_key,
                "start_url": context.plan.start_url,
                "failed_category": category.to_dict(),
                "failed_url": url,
                "pagination_state": state.to_dict(),
                "checkpoint_path": str(context.checkpoint_store.path),
                "diagnostic_focus": [
                    "request_transport",
                    "page_structure",
                    "javascript_rendering",
                    "product_api",
                    "pagination_rule",
                ],
            },
        )
        context.task_journal.append(_handoff_event(handoff))
        return handoff

    def _validation_handoff(
        self,
        context: _RunContext,
        category: CategoryTask,
        state: PaginationState,
        code: str,
        message: str,
        url: str = "",
    ) -> AgentHandoff:
        issue = CatalogIssue(
            code=code,
            message=message,
            category_key=category.key,
            url=url or state.base_url,
            retryable=True,
            details={"pagination": state.to_dict()},
        )
        self._record_issue(context, issue)
        context.checkpoint_store.save(context.checkpoint)
        handoff = AgentHandoff(
            target_agent="ValidationAgent",
            reason="catalog_data_anomaly",
            available=False,
            payload={
                "action": "validate_catalog_coverage",
                "site_key": context.plan.site_key,
                "category": category.to_dict(),
                "pagination_state": state.to_dict(),
                "issue": issue.to_dict(),
                "checkpoint_path": str(context.checkpoint_store.path),
                "possible_causes": [
                    "category_discovery_incomplete",
                    "pagination_not_advancing",
                    "product_api_schema_or_response_error",
                ],
            },
        )
        context.task_journal.append(_handoff_event(handoff))
        return handoff

    def _safe_fetch(
        self,
        url: str,
    ) -> Tuple[Optional[FetchResult], Optional[str]]:
        try:
            result = self._fetch(url)
        except Exception as exc:
            return None, str(exc)
        if result.status_code >= 400:
            return result, f"HTTP {result.status_code}"
        return result, None

    def _fetch(self, url: str) -> FetchResult:
        return self.fetch_tool.fetch(url, preferred_transport="auto")

    def _record_issue(
        self,
        context: _RunContext,
        issue: CatalogIssue,
    ) -> None:
        context.issues.append(issue)
        context.checkpoint.issues.append(issue.to_dict())
        context.issue_journal.append(_issue_event(issue))

    def _result_for_handoff(
        self,
        context: _RunContext,
        handoff: AgentHandoff,
    ) -> CatalogResult:
        status = (
            "replan_required"
            if handoff.target_agent == "CrawlPlanAgent"
            else "validation_required"
        )
        return self._build_result(context, status=status, handoff=handoff)

    def _build_result(
        self,
        context: _RunContext,
        status: str,
        handoff: Optional[AgentHandoff] = None,
    ) -> CatalogResult:
        categories = list(context.checkpoint.category_records.values())
        return CatalogResult(
            status=status,
            traversal_mode=context.checkpoint.traversal_mode,
            product_seeds=list(context.seeds),
            categories=categories,
            completed_category_count=len(
                context.checkpoint.completed_category_keys
            ),
            skipped_category_count=context.skipped_category_count,
            duplicate_product_count=context.duplicate_product_count,
            checkpoint_path=str(context.checkpoint_store.path),
            product_seed_path=str(context.seed_journal.path),
            issues=list(context.issues),
            handoff=handoff,
        )

    def _load_or_create_checkpoint(
        self,
        store: CheckpointStore,
        plan: CrawlPlan,
        roots: List[CategoryTask],
        traversal_mode: str,
        fingerprint: str,
        issues: List[CatalogIssue],
        issue_journal: JsonlJournal,
    ) -> CatalogCheckpoint:
        try:
            checkpoint = store.load()
        except (OSError, ValueError) as exc:
            issue = CatalogIssue(
                code="checkpoint_load_failed",
                message=str(exc),
                retryable=True,
            )
            issues.append(issue)
            issue_journal.append(_issue_event(issue))
            checkpoint = None
        if (
            checkpoint is not None
            and checkpoint.site_key == plan.site_key
            and checkpoint.plan_fingerprint == fingerprint
        ):
            return checkpoint
        if checkpoint is not None:
            issue = CatalogIssue(
                code="checkpoint_plan_mismatch",
                message="checkpoint 与当前 CrawlPlan 不一致，重新建立分类调度状态",
                retryable=False,
            )
            issues.append(issue)
            issue_journal.append(_issue_event(issue))

        unique_roots = []
        seen = set()
        records = {}
        for root in roots:
            if root.key in seen:
                continue
            seen.add(root.key)
            unique_roots.append(root)
            records[root.key] = root.to_dict()
        return CatalogCheckpoint(
            site_key=plan.site_key,
            plan_fingerprint=fingerprint,
            traversal_mode=traversal_mode,
            pending_categories=unique_roots,
            seen_category_keys=sorted(seen),
            category_records=records,
        )

    @staticmethod
    def _load_seed_journal(
        journal: ProductSeedJournal,
        site_key: str,
    ) -> Tuple[List[ProductSeed], Set[str], int]:
        seeds = []
        seen: Set[str] = set()
        duplicates = 0
        for seed in journal.load(site_key):
            if not seed.dedup_key:
                continue
            if seed.dedup_key in seen:
                duplicates += 1
                continue
            seen.add(seed.dedup_key)
            seeds.append(seed)
        return seeds, seen, duplicates

    @staticmethod
    def _peek_category(checkpoint: CatalogCheckpoint) -> CategoryTask:
        if checkpoint.traversal_mode == "bfs":
            return checkpoint.pending_categories[0]
        return checkpoint.pending_categories[-1]

    @staticmethod
    def _remove_pending_category(
        checkpoint: CatalogCheckpoint,
        category_key: str,
    ) -> None:
        checkpoint.pending_categories = [
            category
            for category in checkpoint.pending_categories
            if category.key != category_key
        ]

    def _state_paths(self) -> Dict[str, Path]:
        return {
            "checkpoint": self.run_state_dir / "checkpoints.json",
            "seeds": self.run_state_dir / "product_seeds.jsonl",
            "tasks": self.run_state_dir / "tasks.jsonl",
            "issues": self.run_state_dir / "issues.jsonl",
        }


def _fingerprint_payload(plan: CrawlPlan) -> Dict[str, Any]:
    return {
        "site_key": plan.site_key,
        "start_url": plan.start_url,
        "website_type": plan.website_type,
        "categories": [category.to_dict() for category in plan.categories],
        "api_candidates": [
            candidate.to_dict()
            for candidate in plan.api_candidates
        ],
        "pagination": plan.pagination.to_dict(),
    }


def _issue_event(issue: CatalogIssue) -> Dict[str, Any]:
    return {
        "event": "catalog_issue",
        "agent": "CatalogAgent",
        "issue": issue.to_dict(),
        "timestamp": utc_now(),
    }


def _handoff_event(handoff: AgentHandoff) -> Dict[str, Any]:
    return {
        "event": "agent_handoff",
        "agent": "CatalogAgent",
        "handoff": handoff.to_dict(),
        "timestamp": utc_now(),
    }


__all__ = ["CatalogAgent", "CatalogResult", "ProductSeed"]
