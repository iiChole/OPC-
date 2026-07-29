"""Atomic checkpoint and append-only ProductSeed persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import CatalogCheckpoint, ProductSeed


class CheckpointStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> Optional[CatalogCheckpoint]:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"checkpoint 格式无效: {self.path}")
        return CatalogCheckpoint.from_dict(value)

    def save(self, checkpoint: CatalogCheckpoint) -> None:
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


class ProductSeedJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def load(self, site_key: str = "") -> List[ProductSeed]:
        if not self.path.exists():
            return []
        seeds: List[ProductSeed] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"ProductSeed 日志第 {line_number} 行格式无效"
                    ) from exc
                if not isinstance(value, dict):
                    continue
                seed = ProductSeed.from_dict(value)
                if site_key and seed.site_key != site_key:
                    continue
                seeds.append(seed)
        return seeds

    def append(self, seeds: Iterable[ProductSeed]) -> int:
        values = list(seeds)
        if not values:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for seed in values:
                handle.write(json.dumps(
                    seed.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return len(values)


class JsonlJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, value: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


__all__ = ["CheckpointStore", "JsonlJournal", "ProductSeedJournal"]
