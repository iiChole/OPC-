"""Validate crawl results and coordinate the bounded recovery workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence


@dataclass
class CrawlExecutionSnapshot:
    products: Sequence[Dict[str, Any]]
    discovered_product_count: int
    reported_product_count: Optional[int] = None
    failed_tasks: Sequence[Dict[str, Any]] = ()
    unfinished_task_count: int = 0
    issues: Sequence[Dict[str, Any]] = ()
    categories: Sequence[Dict[str, Any]] = ()


@dataclass
class CrawlValidationReport:
    valid: bool
    saved_product_count: int
    unique_product_count: int
    discovered_product_count: int
    reported_product_count: Optional[int]
    duplicate_product_count: int
    count_checks: Dict[str, bool]
    field_completeness: Dict[str, float]
    missing_fields: Dict[str, Dict[str, Any]]
    failed_tasks: List[Dict[str, Any]]
    unfinished_task_count: int
    diagnoses: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "saved_product_count": self.saved_product_count,
            "unique_product_count": self.unique_product_count,
            "discovered_product_count": self.discovered_product_count,
            "reported_product_count": self.reported_product_count,
            "duplicate_product_count": self.duplicate_product_count,
            "count_checks": self.count_checks,
            "field_completeness": self.field_completeness,
            "missing_fields": self.missing_fields,
            "failed_tasks": self.failed_tasks,
            "unfinished_task_count": self.unfinished_task_count,
            "diagnoses": self.diagnoses,
        }


@dataclass
class CrawlRecoveryDecision:
    action: str
    attempt: int
    max_attempts: int
    publish_final_output: bool
    pause_task: bool
    retry_full_workflow: bool
    recovery_directives: List[str] = field(default_factory=list)
    feedback: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "publish_final_output": self.publish_final_output,
            "pause_task": self.pause_task,
            "retry_full_workflow": self.retry_full_workflow,
            "recovery_directives": self.recovery_directives,
            "feedback": self.feedback,
        }


@dataclass
class GuardedCrawlResult:
    status: str
    attempts: List[Dict[str, Any]]
    categories: List[Dict[str, Any]]
    products: List[Dict[str, Any]]
    final_output: Dict[str, Any]
    internal_state: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "final_output": self.final_output,
            "internal_state": self.internal_state,
        }


class CrawlResultValidator:
    """Validate exhaustive product counts and required product fields."""

    def __init__(
        self,
        required_fields: Sequence[str],
        field_completeness_threshold: float = 1.0,
    ) -> None:
        self.required_fields = tuple(dict.fromkeys(
            str(field).strip() for field in required_fields if str(field).strip()
        ))
        self.field_completeness_threshold = min(
            1.0,
            max(0.0, field_completeness_threshold),
        )

    def validate(self, snapshot: CrawlExecutionSnapshot) -> CrawlValidationReport:
        products = list(snapshot.products)
        product_keys = [_product_key(product, index) for index, product in enumerate(products)]
        unique_count = len(set(product_keys))
        saved_count = len(products)
        duplicate_count = saved_count - unique_count

        count_checks = {
            "saved_equals_discovered": saved_count == snapshot.discovered_product_count,
            "unique_equals_saved": unique_count == saved_count,
            "reported_equals_saved": (
                snapshot.reported_product_count is None
                or snapshot.reported_product_count == saved_count
            ),
            "no_failed_tasks": not snapshot.failed_tasks,
            "no_unfinished_tasks": snapshot.unfinished_task_count == 0,
        }

        missing_fields: Dict[str, Dict[str, Any]] = {}
        field_completeness: Dict[str, float] = {}
        for field_name in self.required_fields:
            missing_keys = [
                product_keys[index]
                for index, product in enumerate(products)
                if _is_missing(product.get(field_name))
            ]
            complete_count = saved_count - len(missing_keys)
            completeness = complete_count / saved_count if saved_count else 0.0
            field_completeness[field_name] = round(completeness, 6)
            if missing_keys:
                missing_fields[field_name] = {
                    "missing_count": len(missing_keys),
                    "sample_product_keys": missing_keys[:20],
                }

        field_checks_passed = all(
            completeness >= self.field_completeness_threshold
            for completeness in field_completeness.values()
        )
        valid = bool(products) and all(count_checks.values()) and field_checks_passed
        diagnoses = _diagnose(
            snapshot=snapshot,
            saved_count=saved_count,
            unique_count=unique_count,
            count_checks=count_checks,
            missing_fields=missing_fields,
        )
        return CrawlValidationReport(
            valid=valid,
            saved_product_count=saved_count,
            unique_product_count=unique_count,
            discovered_product_count=snapshot.discovered_product_count,
            reported_product_count=snapshot.reported_product_count,
            duplicate_product_count=duplicate_count,
            count_checks=count_checks,
            field_completeness=field_completeness,
            missing_fields=missing_fields,
            failed_tasks=list(snapshot.failed_tasks),
            unfinished_task_count=snapshot.unfinished_task_count,
            diagnoses=diagnoses,
        )


class CrawlRecoveryController:
    """Retry the full workflow once; pause and report after the second failure."""

    def __init__(self, max_workflow_attempts: int = 2) -> None:
        self.max_workflow_attempts = max(1, max_workflow_attempts)

    def decide(
        self,
        report: CrawlValidationReport,
        attempt: int,
    ) -> CrawlRecoveryDecision:
        if report.valid:
            return CrawlRecoveryDecision(
                action="complete",
                attempt=attempt,
                max_attempts=self.max_workflow_attempts,
                publish_final_output=True,
                pause_task=False,
                retry_full_workflow=False,
            )

        directives = _recovery_directives(report.diagnoses)
        feedback = {
            "count_checks": report.count_checks,
            "missing_fields": report.missing_fields,
            "failed_tasks": report.failed_tasks,
            "unfinished_task_count": report.unfinished_task_count,
            "diagnoses": report.diagnoses,
        }
        if attempt < self.max_workflow_attempts:
            return CrawlRecoveryDecision(
                action="diagnose_and_retry_workflow",
                attempt=attempt,
                max_attempts=self.max_workflow_attempts,
                publish_final_output=False,
                pause_task=False,
                retry_full_workflow=True,
                recovery_directives=directives,
                feedback=feedback,
            )
        return CrawlRecoveryDecision(
            action="pause_task_and_report",
            attempt=attempt,
            max_attempts=self.max_workflow_attempts,
            publish_final_output=False,
            pause_task=True,
            retry_full_workflow=False,
            recovery_directives=directives,
            feedback=feedback,
        )


class CrawlWorkflowGuard:
    """Run, validate, diagnose, retry once, then pause on repeated failure."""

    def __init__(
        self,
        validator: CrawlResultValidator,
        recovery_controller: Optional[CrawlRecoveryController] = None,
    ) -> None:
        self.validator = validator
        self.recovery_controller = recovery_controller or CrawlRecoveryController()

    def run(
        self,
        execute_workflow: Callable[
            [int, Optional[CrawlRecoveryDecision]],
            CrawlExecutionSnapshot,
        ],
    ) -> GuardedCrawlResult:
        attempts: List[Dict[str, Any]] = []
        previous_decision: Optional[CrawlRecoveryDecision] = None
        last_snapshot: Optional[CrawlExecutionSnapshot] = None

        for attempt in range(1, self.recovery_controller.max_workflow_attempts + 1):
            snapshot = execute_workflow(attempt, previous_decision)
            last_snapshot = snapshot
            report = self.validator.validate(snapshot)
            decision = self.recovery_controller.decide(report, attempt)
            attempts.append({
                "attempt": attempt,
                "validation": report.to_dict(),
                "recovery": decision.to_dict(),
            })
            if decision.publish_final_output:
                products = list(snapshot.products)
                categories = list(snapshot.categories)
                return GuardedCrawlResult(
                    status="complete",
                    attempts=attempts,
                    categories=categories,
                    products=products,
                    final_output={
                        "categories.json": categories,
                        "products_final.json": products,
                    },
                    internal_state={
                        "attempts": attempts,
                        "issues": list(snapshot.issues),
                    },
                )
            if decision.pause_task:
                break
            previous_decision = decision

        snapshot = last_snapshot or CrawlExecutionSnapshot([], 0)
        return GuardedCrawlResult(
            status="paused",
            attempts=attempts,
            categories=list(snapshot.categories),
            products=list(snapshot.products),
            final_output={},
            internal_state={
                "attempts": attempts,
                "partial_categories": list(snapshot.categories),
                "partial_products": list(snapshot.products),
                "issues": list(snapshot.issues),
                "feedback_required": True,
            },
        )


def _product_key(product: Dict[str, Any], index: int) -> str:
    extra = product.get("extra") if isinstance(product.get("extra"), dict) else {}
    candidates = (
        product.get("product_id"),
        extra.get("raw_id"),
        product.get("sku"),
        product.get("model"),
        product.get("detail_url"),
    )
    for value in candidates:
        if value not in (None, ""):
            return str(value).strip().upper()
    return f"__MISSING_KEY__:{index}"


def _is_missing(value: Any) -> bool:
    return value in (None, "", [], {})


def _diagnose(
    snapshot: CrawlExecutionSnapshot,
    saved_count: int,
    unique_count: int,
    count_checks: Dict[str, bool],
    missing_fields: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    diagnoses: List[Dict[str, Any]] = []
    if not count_checks["saved_equals_discovered"]:
        diagnoses.append({
            "code": "catalog_or_detail_count_mismatch",
            "message": (
                f"发现 {snapshot.discovered_product_count} 个商品，但保存 {saved_count} 个；"
                "可能存在详情任务遗漏、保存失败或分页未完成"
            ),
        })
    if not count_checks["reported_equals_saved"]:
        diagnoses.append({
            "code": "reported_total_not_reached",
            "message": (
                f"网站报告总数 {snapshot.reported_product_count}，实际保存 {saved_count}；"
                "可能存在分类、分页或 cursor 遍历缺口"
            ),
        })
    if unique_count != saved_count:
        diagnoses.append({
            "code": "duplicate_products",
            "message": f"检测到 {saved_count - unique_count} 条重复商品",
        })
    if missing_fields:
        diagnoses.append({
            "code": "required_fields_incomplete",
            "message": "部分商品缺少必需字段或详细参数",
            "fields": missing_fields,
        })
    if snapshot.unfinished_task_count:
        diagnoses.append({
            "code": "unfinished_tasks",
            "message": f"仍有 {snapshot.unfinished_task_count} 个任务未完成",
        })
    for issue in list(snapshot.failed_tasks) + list(snapshot.issues):
        text = " ".join(str(value) for value in issue.values()).lower()
        status = str(issue.get("status_code", ""))
        if "429" in text or status == "429":
            code = "rate_limited"
        elif "403" in text or "captcha" in text or "challenge" in text:
            code = "anti_bot_or_access_denied"
        elif "timeout" in text or "connection" in text or "network" in text:
            code = "network_instability"
        elif "cursor" in text or "pagination" in text or "分页" in text:
            code = "pagination_not_advancing"
        elif "selector" in text or "parse" in text or "解析" in text:
            code = "page_structure_changed"
        else:
            code = "task_failure"
        diagnoses.append({
            "code": code,
            "message": str(issue.get("message") or issue),
            "task": issue.get("task_id", ""),
            "url": issue.get("url", ""),
        })
    if not diagnoses and not snapshot.products:
        diagnoses.append({
            "code": "empty_result",
            "message": "抓取结果为空，需重新检查分类入口、产品 API 和访问限制",
        })
    return _deduplicate_diagnoses(diagnoses)


def _recovery_directives(diagnoses: Sequence[Dict[str, Any]]) -> List[str]:
    directives: List[str] = []
    codes = {diagnosis.get("code") for diagnosis in diagnoses}
    if codes & {"catalog_or_detail_count_mismatch", "reported_total_not_reached"}:
        directives.extend([
            "重新运行 CrawlPlanAgent，重新发现分类树和分页规则",
            "从 checkpoint 重置未完成分类、分页和详情任务",
        ])
    if "pagination_not_advancing" in codes:
        directives.append("切换 page/offset/cursor/Next 控件策略并检测重复页面签名")
    if "required_fields_incomplete" in codes:
        directives.append("重新抓取缺失字段商品详情，并重新分析详情页/API 字段映射")
    if "rate_limited" in codes:
        directives.append("降低并发、增加请求间隔，并从 checkpoint 恢复")
    if "anti_bot_or_access_denied" in codes:
        directives.append("切换 Playwright，重新分析 Network API 和访问挑战")
    if "page_structure_changed" in codes:
        directives.append("重新分析 DOM、CSS selector 和 Network 产品 API")
    if "network_instability" in codes:
        directives.append("增加超时和退避后重试失败 URL")
    if not directives:
        directives.append("保留成功 checkpoint，重跑失败和不完整阶段")
    return list(dict.fromkeys(directives))


def _deduplicate_diagnoses(
    diagnoses: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    unique: List[Dict[str, Any]] = []
    seen = set()
    for diagnosis in diagnoses:
        key = (
            diagnosis.get("code", ""),
            diagnosis.get("task", ""),
            diagnosis.get("url", ""),
            diagnosis.get("message", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(diagnosis)
    return unique
