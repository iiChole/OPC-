from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .models import CrawlIssue, CrawlReport, PageDiagnostic, ProductRecord


class CrawlStorage:
    """Keep the same catalog/list/detail/final split used by the existing crawlers."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def query_dir(self, query: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", query).strip("._") or "query"
        path = self.base_dir / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(
        self,
        report: CrawlReport,
        catalog_products: Iterable[ProductRecord],
        detail_products: Iterable[ProductRecord],
    ) -> Path:
        target = self.query_dir(report.request.query)
        report.output_dir = str(target.resolve())
        self._save_json(
            target / "catalogs.json",
            [diagnostic.to_dict() for diagnostic in report.diagnostics if diagnostic.stage == "catalog"],
        )
        self._save_jsonl(target / "products.jsonl", [item.to_dict() for item in catalog_products])
        self._save_jsonl(target / "product_details.jsonl", [item.to_dict() for item in detail_products])
        self._save_json(target / "products_final.json", report.to_dict())
        self._save_jsonl(target / "issues.jsonl", [issue.to_dict() for issue in report.issues])
        return target

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _save_jsonl(path: Path, items: Iterable[Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
