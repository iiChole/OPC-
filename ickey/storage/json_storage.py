"""
JSON 数据存储

支持全量写入、增量追加、断点 checkpoint 管理。
"""

import json
import os
from typing import Any, Set


def save_json(data: Any, filepath: str) -> None:
    """
    保存数据为 JSON 文件（覆盖写入）。

    Args:
        data: 要保存的数据
        filepath: 目标文件路径
    """
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(filepath: str) -> Any:
    """
    从 JSON 文件加载数据。

    Args:
        filepath: JSON 文件路径

    Returns:
        解析后的数据，文件不存在时返回 None
    """
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def append_to_json_file(item: dict, filepath: str) -> None:
    """
    增量追加一条记录到 JSON 数组文件。

    文件格式为 JSON 数组 [...]，追加时会：
    1. 读取现有数组
    2. 追加新元素
    3. 写回文件

    Args:
        item: 单条记录
        filepath: JSON 数组文件路径
    """
    data = load_json(filepath)
    if data is None:
        data = []
    data.append(item)
    save_json(data, filepath)


# ---------- 断点续爬 checkpoint ----------

def load_checkpoint(checkpoint_path: str) -> Set[str]:
    """
    加载已完成的任务 ID 集合。

    文件格式: 每行一个 ID

    Args:
        checkpoint_path: checkpoint 文件路径

    Returns:
        已完成 ID 的 set
    """
    if not os.path.exists(checkpoint_path):
        return set()
    with open(checkpoint_path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_checkpoint(checkpoint_path: str, completed_ids: Set[str]) -> None:
    """
    保存已完成的任务 ID 集合。

    Args:
        checkpoint_path: checkpoint 文件路径
        completed_ids: 已完成 ID 的 set
    """
    os.makedirs(os.path.dirname(checkpoint_path) or ".", exist_ok=True)
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        for cid in sorted(completed_ids):
            f.write(cid + "\n")


def mark_completed(checkpoint_path: str, item_id: str) -> None:
    """
    标记一个 ID 为已完成（追加写入一行）。

    Args:
        checkpoint_path: checkpoint 文件路径
        item_id: 已完成的 ID
    """
    os.makedirs(os.path.dirname(checkpoint_path) or ".", exist_ok=True)
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(item_id + "\n")
