"""
JSON 数据存储

支持:
    - JSON 文件全量读写
    - JSON Lines (.jsonl) 流式追加（批量缓冲 + 定时 Flush）
    - 断点 checkpoint 管理
    - 信号捕获（SIGINT/SIGTERM）确保异常退出时数据不丢失
"""

import json
import os
import signal
import time
import atexit
from typing import Any, Set, List, Optional


# ---------- 标准 JSON 读写 ----------

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


# ---------- JSON Lines (.jsonl) 读写 ----------

def save_jsonl(items: list, filepath: str) -> None:
    """
    保存数据为 JSON Lines 文件（覆盖写入）。

    每行一个 JSON 对象，方便增量追加和按行解析。

    Args:
        items: 数据列表
        filepath: 目标文件路径
    """
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def load_jsonl(filepath: str) -> List[dict]:
    """
    从 JSON Lines 文件加载数据。

    按行读取，每一行是一个独立的 JSON 对象。
    容错处理：跳过空行和解析失败的行。

    Args:
        filepath: .jsonl 文件路径

    Returns:
        解析后的数据列表，文件不存在时返回空列表
    """
    if not os.path.exists(filepath):
        return []
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


# ---------- JSONL 流式写入器（批量缓冲 + 定时 Flush + 信号安全）----------

# 全局注册表，用于信号处理时遍历所有活跃的 Writer
_writers: List["JSONLWriter"] = []
_signal_registered = False


def _signal_handler(signum, frame):
    """全局信号处理器：强制 Flush 所有活跃 Writer 后退出"""
    for w in _writers:
        try:
            w.flush()
        except Exception:
            pass
    # 恢复默认信号处理并重新发送信号
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _register_signal_handlers():
    """注册进程信号处理器（仅执行一次）"""
    global _signal_registered
    if _signal_registered:
        return
    _signal_registered = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            pass  # 非主线程中无法注册信号，忽略


class JSONLWriter:
    """
    JSON Lines 流式写入器。

    功能:
        - 内存缓冲，达到阈值或超时后批量落盘
        - 注册信号处理器，确保 SIGINT/SIGTERM 时数据不丢失
        - 线程安全（通过文件追加的原子性保证）

    用法:
        writer = JSONLWriter("data/products.jsonl", flush_size=50, flush_interval=10)
        writer.append({"sku": "C123", "title": "..."})
        writer.append({"sku": "C456", "title": "..."})
        # ... 自动按条件 Flush
        writer.flush()   # 手动 Flush
        writer.close()   # 关闭并 Flush
    """

    def __init__(
        self,
        filepath: str,
        flush_size: int = 50,
        flush_interval: float = 10.0,
    ):
        """
        Args:
            filepath: .jsonl 文件路径
            flush_size: 缓冲区达到多少条时触发 Flush（默认 50）
            flush_interval: 距离上次 Flush 超过多少秒时触发 Flush（默认 10）
        """
        self.filepath = filepath
        self.flush_size = max(1, flush_size)
        self.flush_interval = flush_interval
        self._buffer: List[dict] = []
        self._last_flush_time = time.time()
        self._closed = False

        # 确保目录存在
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

        # 注册到全局列表 + 信号处理
        _writers.append(self)
        _register_signal_handlers()

        # atexit 兜底：进程正常退出时 Flush
        atexit.register(self._atexit_flush)

    def append(self, item: dict) -> None:
        """
        追加一条记录到缓冲区，达到阈值自动 Flush。

        Args:
            item: 单条记录（dict）
        """
        if self._closed:
            raise RuntimeError("JSONLWriter already closed")

        self._buffer.append(item)

        # 检查是否达到 Flush 条件
        if len(self._buffer) >= self.flush_size:
            self.flush()
        elif time.time() - self._last_flush_time >= self.flush_interval:
            self.flush()

    def extend(self, items: List[dict]) -> None:
        """
        批量追加多条记录。

        Args:
            items: 记录列表
        """
        for item in items:
            self.append(item)

    def flush(self) -> None:
        """强制将缓冲区数据写入文件"""
        if not self._buffer:
            return

        try:
            with open(self.filepath, "a", encoding="utf-8") as f:
                for item in self._buffer:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            raise
        finally:
            self._buffer.clear()
            self._last_flush_time = time.time()

    def close(self) -> None:
        """关闭 Writer，Flush 剩余数据并从全局注册表移除"""
        if self._closed:
            return
        self._closed = True
        self.flush()
        try:
            _writers.remove(self)
        except ValueError:
            pass

    def _atexit_flush(self):
        """atexit 回调：进程正常退出时 Flush"""
        if not self._closed:
            try:
                self.flush()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------- 兼容旧接口 ----------

def append_to_json_file(item: dict, filepath: str) -> None:
    """
    增量追加一条记录到 JSON 数组文件（旧接口，保留兼容）。

    新代码建议使用 JSONLWriter 或 save_jsonl。

    Args:
        item: 单条记录
        filepath: JSON 数组文件路径
    """
    data = load_json(filepath)
    if data is None:
        data = []
    data.append(item)
    save_json(data, filepath)


def append_many_to_json_file(items: list, filepath: str) -> None:
    """
    批量追加多条记录到 JSON 数组文件（旧接口，保留兼容）。

    Args:
        items: 记录列表
        filepath: JSON 数组文件路径
    """
    data = load_json(filepath)
    if data is None:
        data = []
    data.extend(items)
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
