"""任务队列的 **MongoDB 后端**（B1，2026-10-13）。

上库动因 = **多实例部署**（原实现把队列状态放在进程内 + 本地文件，见 QUEUE_MIGRATION_PLAN.md §1）。

集合（库名 = `MONGO_DB`，默认 `podcast`）
──────────────────────────────────────────
| 集合 | `_id` | 文档 |
|---|---|---|
| `queue_tasks` | `task_id`（`q_*` / `recovered_*`） | 任务全文，字段与今天的任务 JSON **逐字段一致** |

设计要点
- **`_id` 就是业务主键**：`task_id` 天然唯一 ⇒ upsert 幂等、`load_task` 一次主键查、
  `delete_task` 一次主键删。文档里同时保留业务字段 `id`（上层到处读 `task["id"]`）。
- **顺序在任务自带的 `seq` 字段里**（不是单独一张顺序表）：插入取 `max(seq)+1`，
  拖拽排序＝把一组任务当前占用的 `seq` 槽位按新顺序回填。理由见方案文档 §3.2。
- 列表 / 调度用的索引见 `tools/init_indexes.py` 的 `QUEUE_INDEXES`。

⚠️ 与账本 / 内容后端一致：**不做**「连不上就退回文件」的兜底 —— 静默切换会让
   用户看到「任务全没了」。连不上就报错（`DBUnavailable`）。
"""

from __future__ import annotations

from typing import Optional

from .db import get_mongo_db


def _tasks():
    return get_mongo_db()["queue_tasks"]


def _clean(doc: Optional[dict]) -> Optional[dict]:
    """去掉 Mongo 自己维护的 `_id`（业务字段里已有 `id`，留着会污染上层对象）。"""
    if not doc:
        return None
    return {k: v for k, v in doc.items() if k != "_id"}


def save_task(doc: dict) -> None:
    """整份覆盖（upsert）。队列任务是单文档，没有局部更新语义。"""
    task_id = doc["id"]
    _tasks().replace_one({"_id": task_id}, {**doc, "_id": task_id}, upsert=True)


def load_task(task_id: str) -> Optional[dict]:
    return _clean(_tasks().find_one({"_id": task_id}))


def load_all_tasks() -> list[dict]:
    """全部任务（启动恢复用）。B1 阶段是全表扫 —— 启动一次、量级小（几十~几百条）。"""
    return [_clean(d) for d in _tasks().find({})]


def delete_task(task_id: str) -> bool:
    return _tasks().delete_one({"_id": task_id}).deleted_count > 0


def load_legacy_order() -> list[str]:
    """库里没有「老顺序文件」这个概念 —— 恒返回空。

    老 `_queue_order.json` 的顺序由迁移工具补进各任务的 `seq`
    （`tools/migrate_queue.py --backfill-seq`）。
    """
    return []
