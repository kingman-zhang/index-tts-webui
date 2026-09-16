"""任务队列的内存状态与磁盘持久化（自 server.py 拆出，行为不变）。

注意：queue_tasks / queue_order / current_task_id 是进程内共享状态，
其他模块必须通过 `from . import queue_state as qs` 后以 `qs.queue_tasks`
等方式访问（模块属性读写），不能用 `from ... import` 拷贝引用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .config import QUEUE_DIR, QUEUE_ORDER_FILE, logger


class QueueTaskStatus:
    QUEUED = "queued"        # 排队中
    PAUSED = "paused"        # 已暂停，暂不参与调度
    RUNNING = "running"      # 正在 TTS 上合成
    SUCCESS = "success"      # 完成
    FAILED = "failed"        # 失败
    SYNCING = "syncing"      # 等待 TTS 连接恢复
    INTERRUPTED = "interrupted"  # 任务中断，可重新提交
    CANCELLED = "cancelled"  # 已取消


queue_tasks: dict = {}  # task_id -> task_info
queue_order: list = []  # 排队顺序
current_task_id: Optional[str] = None  # 当前正在处理的任务

import asyncio  # noqa: E402  (与原实现保持一致：模块级单例锁)
queue_lock = asyncio.Lock()


def queue_file(task_id: str) -> Path:
    """单个队列任务的持久化路径。"""
    safe_id = task_id.replace("/", "_")
    return QUEUE_DIR / f"{safe_id}.json"


def persist_queue_order() -> None:
    """把 queue_order 持久化到磁盘，重启后恢复执行顺序。"""
    try:
        QUEUE_ORDER_FILE.write_text(
            json.dumps(queue_order, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("[queue] persist queue_order failed error=%s", e)


def load_queue_order() -> None:
    """从磁盘恢复 queue_order，并补入外部放入的 queued 任务。

    任务文件可以由 WebUI 以外的方式生成，因此不能只相信
    _queue_order.json；否则该文件为空或漏记任务时，任务会显示为 queued
    但永远不会被 _process_queue() 取出。
    """
    saved = []
    if QUEUE_ORDER_FILE.exists():
        try:
            raw = json.loads(QUEUE_ORDER_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                saved = raw
        except Exception as e:
            logger.warning("[queue] load queue_order failed error=%s", e)

    # 先保留持久化顺序，再把未被登记的 queued 任务按创建时间补到队尾。
    valid = [
        tid for tid in saved
        if tid in queue_tasks
        and queue_tasks[tid].get("status") == QueueTaskStatus.QUEUED
    ]
    missing = [
        (task.get("created_at", ""), tid)
        for tid, task in queue_tasks.items()
        if task.get("status") == QueueTaskStatus.QUEUED and tid not in valid
    ]
    missing.sort(key=lambda item: item[0])
    queue_order[:] = valid + [tid for _, tid in missing]
    if missing:
        persist_queue_order()
        logger.warning(
            "[queue] discovered %d queued task(s) missing from _queue_order.json: %s",
            len(missing), [tid for _, tid in missing],
        )
    logger.info("[queue] restored queue_order: %d tasks", len(queue_order))


def persist_task(task_id: str) -> None:
    """把单个队列任务写入磁盘。"""
    task = queue_tasks.get(task_id)
    if not task:
        return
    try:
        path = queue_file(task_id)
        path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("[queue] persist failed task=%s error=%s", task_id, e)


def delete_persisted_task(task_id: str) -> None:
    """从磁盘删除队列任务文件。"""
    try:
        path = queue_file(task_id)
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.warning("[queue] delete persisted failed task=%s error=%s", task_id, e)


def load_persisted_tasks() -> None:
    """启动时从磁盘恢复队列任务。"""
    if not QUEUE_DIR.exists():
        return
    for f in sorted(QUEUE_DIR.glob("*.json")):
        # 顺序索引不是任务文件，不能按任务对象解析。
        if f == QUEUE_ORDER_FILE:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            task_id = data.get("id")
            if not task_id or task_id in queue_tasks:
                continue
            queue_tasks[task_id] = data
            # 恢复排队顺序
            if data.get("status") == QueueTaskStatus.QUEUED:
                queue_order.append(task_id)
            # WebUI 重启后，带 TTS task id 的运行任务由启动逻辑恢复轮询。
            elif data.get("status") in (QueueTaskStatus.RUNNING, QueueTaskStatus.SYNCING):
                data["status"] = QueueTaskStatus.RUNNING
                data["message"] = data.get("message") or "WebUI 重启后恢复"
        except Exception as e:
            logger.warning("[queue] load persisted task failed file=%s error=%s", f, e)
    logger.info("[queue] loaded %d persisted tasks", len(queue_tasks))
    # 用持久化的 queue_order 覆盖默认排序
    load_queue_order()
