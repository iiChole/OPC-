from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

from .models import ALL_FIELDS, CrawlRequest


FIELD_ALIASES = {
    "price": ("price", "价格", "报价"),
    "stock": ("stock", "库存", "现货"),
    "package": ("package", "封装", "包装"),
    "manufacturer": ("manufacturer", "maker", "厂商", "制造商", "品牌"),
    "model": ("model", "型号"),
    "sku": ("sku", "商品编号"),
    "title": ("title", "标题", "名称"),
    "description": ("description", "描述", "简介"),
    "moq": ("moq", "起订量", "最小采购量"),
    "attributes": ("attributes", "参数", "属性"),
    "datasheet_url": ("datasheet", "数据手册", "规格书"),
    "image_url": ("image", "图片"),
}


def analyze_input(
    text: str = "",
    query: str = "",
    fields: Optional[Sequence[str]] = None,
) -> CrawlRequest:
    """Accept either CLI arguments or text such as `查询: xxx. 需要: 价格, 库存`."""
    resolved_query = query.strip() or _extract_query(text)
    if not resolved_query:
        raise ValueError("无法识别查询型号，请使用 --query 或输入 `查询: 型号`")

    if fields:
        resolved_fields = _normalize_fields(fields)
    else:
        resolved_fields = _extract_fields(text)
    return CrawlRequest(query=resolved_query, fields=resolved_fields or ALL_FIELDS)


def _extract_query(text: str) -> str:
    patterns = (
        r"(?:查询|query)\s*[:：]\s*([^\n。；;]+)",
        r"(?:型号|model)\s*[:：]\s*([^\n。；;]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = re.split(r"\s*(?:需要|fields?)\s*[:：]", match.group(1), flags=re.I)[0]
            return value.strip(" ,，.。")
    return text.strip() if text.strip() and len(text.strip().split()) == 1 else ""


def _extract_fields(text: str) -> Tuple[str, ...]:
    match = re.search(r"(?:需要|fields?)\s*[:：]\s*([^\n。；;]+)", text, re.I)
    if not match:
        return ALL_FIELDS
    value = match.group(1).strip()
    if any(marker in value.lower() for marker in ("全部", "所有", "all")):
        return ALL_FIELDS
    tokens = [token for token in re.split(r"[,，、/\s]+", value) if token]
    return _normalize_fields(tokens)


def _normalize_fields(values: Iterable[str]) -> Tuple[str, ...]:
    normalized: List[str] = []
    for value in values:
        lowered = value.strip().lower()
        if not lowered:
            continue
        if lowered in ALL_FIELDS:
            normalized.append(lowered)
            continue
        for field, aliases in FIELD_ALIASES.items():
            if any(lowered == alias.lower() for alias in aliases):
                normalized.append(field)
                break
    return tuple(dict.fromkeys(normalized))
