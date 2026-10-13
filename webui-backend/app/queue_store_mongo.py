"""任务队列的 **MongoDB 后端**（B1，2026-10-13）。

上库动因 = 把队列从「进程内 + 本地文件」挪到共享存储：任务不再依赖本机磁盘恢复，
且**可统计**（在跑多少、近一周任务量）。⚠️ 跨实例抢占（B2）已决定**挂起** ——
单实例够用，等用户量撑不住时再做；本后端只负责「持久化 + 可查询」。

集合（库名 = `MONGO_DB`，默认 `podcast`）
──────────────────────────────────────────
| 集合 | `_id` | 文档 |
|---|---|---|
| `queue_tasks` | `task_id`（`q_*` / `recovered_*`） | 任务全文，字段与任务 JSON **逐字段一致** |

⚠️ **`created_ts` / `deleted_at` 是后端自治字段**：不返回给上层、不参与上层写入
（见 `_MANAGED_FIELDS`）—— 所以内存里的任务对象与「文件后端」读出来的逐字段一致。

与「文件后端」有意的三处差异
────────────────────────────
1. **字段级更新**（`save_task`）：`lines` 一条就占任务体积 ~81%（实测单任务 8~38 KB），
   而进度更新（节流后 2s/次）只改 `status`/`progress`/`message` 几个小字段 ——
   整份覆盖等于让 80%+ 的写流量服务于 5% 的内容。这里把「大且低频」的字段挑出来
   （`_PAYLOAD_FIELDS`），**只在内容真的变了时才写**（进程内记指纹，见 `_payload_sig`），
   其余字段（都很小）照常每次都写。刻意**只挑大字段**做指纹，是为了把「某个字段
   静默没写进去」的风险压到最低。
2. **软删除**（`delete_task`）：只打 `deleted_at` 标记，**不从库里移除** —— 否则用户
   点一次「清空已完成」，统计历史就没了。读接口（`load_task` / `load_all_tasks`）
   统一过滤，所以**上层语义不变**（这个任务「不存在了」）。
3. **`created_ts`（BSON Date，UTC）**：`created_at` 是**无时区**的 ISO 字符串，
   范围查能用（字典序 = 时序）、但**按天/周聚合不能用**（`$dateToString` / `$dateTrunc`
   只认 Date 类型）。所以额外存一个真正的时间字段供统计，用 `$setOnInsert` 写入 ——
   只在首次插入时定，之后不再变。

⚠️ 因为有了软删除，`save_task` **绝不能退回 `replace_one`** —— 整份替换会把
   `deleted_at` 抹掉、让已删除的任务复活。用 `$set` 天然安全（没列出的字段不动）。
⚠️ 与账本 / 内容后端一致：**不做**「连不上就退回文件」的兜底 —— 静默切换会让
   用户看到「任务全没了」。连不上就报错（`DBUnavailable`）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from .db import get_mongo_db

# 后端自治字段：不在上层对象里出现（读时剥掉、写时不参与）。
_MANAGED_FIELDS = ("created_ts", "deleted_at")

# 「大且低频」的字段 —— 只在内容真变了才重写（见模块 docstring 第 1 条）。
_PAYLOAD_FIELDS = ("lines", "voices", "silence", "params")

# task_id -> 上次写入的负载指纹。进程内状态：重启后为空 ⇒ 每个任务会全量写一次，
# 之后回到「只写小字段」。条目在删除时清掉；单条 ~80 B，可忽略。
_payload_sig: dict[str, str] = {}


def _tasks():
    return get_mongo_db()["queue_tasks"]


def _now() -> datetime:
    """UTC 时间。pymongo 存为 BSON Date（聚合 / 索引都能用）。"""
    return datetime.now(timezone.utc)


def _alive(task_id: str) -> dict:
    """「未软删除」的查询条件。"""
    return {"_id": task_id, "deleted_at": {"$exists": False}}


def _clean(doc: Optional[dict]) -> Optional[dict]:
    """剥掉 Mongo 的 `_id` 与后端自治字段，让上层拿到的对象与文件后端一致。"""
    if not doc:
        return None
    return {k: v for k, v in doc.items() if k != "_id" and k not in _MANAGED_FIELDS}


def _payload_signature(doc: dict) -> str:
    """负载字段的指纹（内容没变 ⇒ 指纹不变 ⇒ 跳过重写）。"""
    payload = {k: doc.get(k) for k in _PAYLOAD_FIELDS if k in doc}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def save_task(doc: dict) -> None:
    """写入（upsert）：小字段每次都写，大字段只在内容变了时写。

    ⚠️ **不要**改回 `replace_one` —— 见模块 docstring 末尾（会抹掉 `deleted_at`）。
    """
    task_id = doc["id"]
    body = {k: v for k, v in doc.items() if k != "_id" and k not in _MANAGED_FIELDS}
    fields = {k: v for k, v in body.items() if k not in _PAYLOAD_FIELDS}
    sig = _payload_signature(doc)
    if _payload_sig.get(task_id) != sig:
        fields.update({k: body[k] for k in _PAYLOAD_FIELDS if k in body})
        _payload_sig[task_id] = sig
    # `created_ts` 只在**首次插入**时定；迁移工具会先把解析好的创建时间塞进 doc。
    on_insert = {"created_ts": doc.get("created_ts") or _now()}
    _tasks().update_one(
        {"_id": task_id},
        {"$set": fields, "$setOnInsert": on_insert},
        upsert=True,
    )


def load_task(task_id: str) -> Optional[dict]:
    return _clean(_tasks().find_one(_alive(task_id)))


def load_all_tasks() -> list[dict]:
    """全部**未删除**任务（启动恢复用）。

    B1 阶段是全表扫 —— 启动一次、量级小（几十~几百条）。软删除的记录会被过滤掉，
    所以已删除的任务不会在重启后「复活」。
    """
    return [_clean(d) for d in _tasks().find({"deleted_at": {"$exists": False}})]


def delete_task(task_id: str) -> bool:
    """软删除：打 `deleted_at` 标记。

    返回「是否在**本次**调用里标记成功」—— 任务不存在、或已经被标记过，都返回 False
    （与文件后端 `unlink` 找不到文件返回 False 的形状一致）。
    """
    res = _tasks().update_one(_alive(task_id), {"$set": {"deleted_at": _now()}})
    if not res.matched_count:
        return False
    _payload_sig.pop(task_id, None)
    return True


def load_legacy_order() -> list[str]:
    """库里没有「老顺序文件」这个概念 —— 恒返回空。

    老 `_queue_order.json` 的顺序由迁移工具补进各任务的 `seq`
    （`tools/migrate_queue.py --backfill-seq`）。
    """
    return []
