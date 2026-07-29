"""Product detail checkpoint and append-only normalized product storage."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional

from .models import NormalizedProduct, ProductCheckpoint


class ProductCheckpointStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> Optional[ProductCheckpoint]:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"Product checkpoint 格式无效: {self.path}")
        return ProductCheckpoint.from_dict(value)

    def save(self, checkpoint: ProductCheckpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    checkpoint.to_dict(),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


class ProductDetailJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def load_latest(self, site_key: str = "") -> Dict[str, NormalizedProduct]:
        if not self.path.exists():
            return {}
        products: Dict[str, NormalizedProduct] = {}
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"详情日志第 {line_number} 行格式无效"
                    ) from exc
                if not isinstance(value, dict):
                    continue
                product = NormalizedProduct.from_dict(value)
                if site_key and product.site_key != site_key:
                    continue
                if product.dedup_key:
                    products[product.dedup_key] = product
        return products

    def append(self, products: Iterable[NormalizedProduct]) -> int:
        values = list(products)
        if not values:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for product in values:
                handle.write(json.dumps(
                    product.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return len(values)


__all__ = ["ProductCheckpointStore", "ProductDetailJournal"]
