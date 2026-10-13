"""任务队列的存储**门面**（B1：队列任务可落 MongoDB；调度仍单实例）。

门面（按 `QUEUE_STORE` 分派）
────────────────────────────
import 期一次性选定后端（`queue_store_file` / `queue_store_mongo`），之后**只做转发**，
不含任何后端分支。取值域 `file`（默认）| `mongo`；非法值**启动即报错**、不静默回退
—— 两后端的存储形态不同，静默切换会让用户看到「任务全没了」。

接口刻意极小（只有「一个任务一份文档」这一种形态），保证 file / mongo 可逐项等价：

    save_task(doc)            整份覆盖（upsert by `_id = doc["id"]`）
    load_task(task_id)        单任务；不存在返回 None
    load_all_tasks()          全部任务（启动恢复用）
    delete_task(task_id)      删；返回是否真的删掉了
    load_legacy_order()       老 `_queue_order.json` 的排序提示（mongo 恒 []）

⚠️ 与账本 / 会话 / 内容三个门面一样：**不做**「连不上就退回文件」的兜底。
⚠️ 动因与边界见 `QUEUE_MIGRATION_PLAN.md`：**B1 只上库，不解决跨实例抢占**。
"""

from __future__ import annotations

from .config import QUEUE_STORE


# ─── 后端分派（import 期一次决定）────────────────────────────

if QUEUE_STORE == "file":
    from . import queue_store_file as _impl
elif QUEUE_STORE == "mongo":
    from . import queue_store_mongo as _impl
else:
    raise RuntimeError(
        f"未知的 QUEUE_STORE={QUEUE_STORE!r}；取值域 file | mongo。"
        "（两后端存储形态不同，不支持静默回退 —— 请检查 .env）"
    )

# 门面必须完整转发的接口；缺一个就在 import 期炸，而不是等到某个接口 500。
_REQUIRED_API = (
    "save_task", "load_task", "load_all_tasks", "delete_task", "load_legacy_order",
)
_missing = [n for n in _REQUIRED_API if not hasattr(_impl, n)]
if _missing:
    raise RuntimeError(
        f"队列存储后端 {QUEUE_STORE!r} 缺少接口：{', '.join(_missing)}"
    )

BACKEND = QUEUE_STORE   # 自检用（/api/version 报它）


def save_task(doc: dict) -> None:
    """整份覆盖写（队列任务是单文档，没有局部更新语义）。"""
    _impl.save_task(doc)


def load_task(task_id: str) -> dict | None:
    return _impl.load_task(task_id)


def load_all_tasks() -> list[dict]:
    """全部任务文档（启动恢复时一次性读完）。**不含** `_id`（后端负责剥掉）。"""
    return _impl.load_all_tasks()


def delete_task(task_id: str) -> bool:
    return _impl.delete_task(task_id)


def load_legacy_order() -> list[str]:
    """老 `_queue_order.json` 里的 id 顺序，仅用于给「缺 `seq` 的老任务」定序。

    这是**文件格式**的遗留概念，所以接口由后端实现：file 后端读那个文件，
    mongo 后端返回 `[]`（迁移工具 `tools/migrate_queue.py --backfill-seq` 负责补 `seq`）。
    """
    return _impl.load_legacy_order()
