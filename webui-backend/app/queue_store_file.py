"""任务队列的 **JSON 文件后端**（B1，由原 `queue_state` 的持久化部分抽出）。

配合 `queue_store.py` 门面使用：门面按 `QUEUE_STORE` 在 file / mongo 之间分派，
本模块只负责「怎么落到磁盘」。

文件布局（`QUEUE_DIR` = `DATA_DIR/queue`）
  <task_id>.json          单个任务全文（与历史逐字节一致）
  _queue_order.json       **已退役**（顺序改为任务自带的 `seq` 字段）；
                          只在 `load_legacy_order()` 里读一次，用于给老任务定序。

⚠️ 两个顺带修正：
   1. 写盘改走 `app/json_store.write_json`（tmp + `os.replace`）—— 原实现是
      `path.write_text` 直写，**非原子**：崩在写一半就是 JSON 截断，启动解析失败
      只打 warning，该任务从内存消失、文件永久留盘、用户看不到也删不掉。
   2. `bak_001~006/` 之类**子目录**不再被 `glob("*.json")` 扫到（它们本来也不是任务
      文件，只是历史快照），因为只扫本层且跳过目录。

⚠️ 本模块**不带跨实例一致性**（本地磁盘）。多实例部署请用 `QUEUE_STORE=mongo`，
   见 QUEUE_MIGRATION_PLAN.md。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .config import QUEUE_DIR, QUEUE_ORDER_FILE
from .json_store import read_json as _read, write_json as _write


def task_path(task_id: str) -> Path:
    """单个任务的持久化路径（与历史命名一致，防路径穿越）。"""
    safe_id = str(task_id).replace("/", "_").replace("\\", "_")
    return QUEUE_DIR / f"{safe_id}.json"


def save_task(doc: dict) -> None:
    _write(task_path(doc["id"]), doc)


def load_task(task_id: str) -> Optional[dict]:
    data = _read(task_path(task_id), None)
    return data if isinstance(data, dict) else None


def load_all_tasks() -> list[dict]:
    """本层全部任务文件（跳过子目录与顺序索引文件）。

    顺序索引 `_queue_order.json` 的内容是**纯 id 数组**、不是任务对象，
    混进来会让上层按任务解析时报错 —— 所以显式跳过。
    """
    if not QUEUE_DIR.exists():
        return []
    out: list[dict] = []
    for f in sorted(QUEUE_DIR.glob("*.json")):
        if f == QUEUE_ORDER_FILE:
            continue
        data = _read(f, None)
        if isinstance(data, dict) and data.get("id"):
            out.append(data)
    return out


def delete_task(task_id: str) -> bool:
    try:
        task_path(task_id).unlink()
        return True
    except FileNotFoundError:
        return False


def load_legacy_order() -> list[str]:
    """读一次老 `_queue_order.json`（**只读不写**）。缺失或损坏返回 `[]`。"""
    if not QUEUE_ORDER_FILE.exists():
        return []
    try:
        raw = json.loads(QUEUE_ORDER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [str(t) for t in raw] if isinstance(raw, list) else []
