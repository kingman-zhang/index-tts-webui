"""任务队列的内存状态与持久化（B1：持久化改走 `queue_store` 门面）。

注意：queue_tasks / queue_order / current_task_id / running_ids 是进程内共享状态，
其他模块必须通过 `from . import queue_state as qs` 后以 `qs.queue_tasks`
等方式访问（模块属性读写），不能用 `from ... import` 拷贝引用。

B1 改了什么（其余一律不变）
──────────────────────────
* 落盘 / 读盘转发给 `app/queue_store.py`（`QUEUE_STORE=file|mongo`），
  **函数名与全部调用点都不变**；调度逻辑一行没动。
* 排队顺序由「`_queue_order.json` 一个数组」改为「每个任务自带的 `seq` 字段」。
  原因：库里再搞一个全局可变数组，只是把同一个共享点从文件搬进库（B2 里既无法
  原子取队首、又是写热点）。见 QUEUE_MIGRATION_PLAN.md §3.2。
* 内存仍是**执行期的权威**（单实例）：`queue_order` 依旧是真源，
  `seq` 只是它的持久化投影。
"""

from __future__ import annotations

import re
from typing import Optional

from . import queue_store
from .config import QUEUE_WORKER_ENABLED, logger


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
current_task_id: Optional[str] = None  # 当前正在处理的任务（最早启动的一个，兼容旧展示）
running_ids: set = set()  # 所有运行中任务 id（并发调度用）

import asyncio  # noqa: E402  (与原实现保持一致：模块级单例锁)
queue_lock = asyncio.Lock()


def audio_download_name(task: Optional[dict], task_id: str, kind: str) -> str:
    """只调整下载名，不改磁盘路径；缺项目名的历史任务保留原命名。"""
    name = (task or {}).get("project_name") or f"{kind}_{task_id}"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f-\x9f]', "_", name).strip().rstrip(". ")
    name = re.sub(r"(?:\.wav)+$", "", name, flags=re.IGNORECASE).rstrip(". ")
    if not name:
        name = "audio"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.IGNORECASE):
        name = "_" + name
    # 给扩展名预留空间，避免中文多字节名称超过常见文件系统限制。
    name = name.encode("utf-8")[:240].decode("utf-8", errors="ignore").rstrip(". ")
    return f"{name}.wav"


# ─── 排队顺序：内存 queue_order ⇄ 每任务 seq 字段 ────────────────

def next_seq() -> int:
    """下一个排队位次（全局递增整数）。

    只要求**相对顺序**：任务被取走后不重编号（会留空洞，与改造前一致）。
    新任务恒取 `max(全部任务 seq)+1` ⇒ 追加在队尾，且不会与其他任务撞号。
    """
    return max((t.get("seq") or 0 for t in queue_tasks.values()), default=0) + 1


def _order_hint() -> dict:
    """老 `_queue_order.json` 的 id → 名次（仅 file 后端有；缺失即空表）。"""
    try:
        return {tid: i for i, tid in enumerate(queue_store.load_legacy_order())}
    except Exception as e:  # 兼容提示拿不到不影响启动
        logger.warning("[queue] 读取遗留顺序文件失败（忽略）: %s", e)
        return {}


def persist_queue_order() -> None:
    """把内存里的排队顺序物化到各任务的 `seq` 字段（幂等）。

    三种情形：
    1. `seq` 已严格递增（提交 / 重试 / 恢复后的常规路径）⇒ **零写入**，
       这是绝大多数调用的情形；
    2. `seq` 乱序（只有拖拽排序会造成）⇒ 把当前占用的那组 `seq` 槽位
       **按新顺序回填**（同一组槽位换主人，其他任务的相对位置一点不动）；
    3. 队列里有任务**没有 `seq`**（老数据首次启动）⇒ 从全局最大值起整队重编号。
    """
    ids = [tid for tid in queue_order if tid in queue_tasks]
    if not ids:
        return
    slots = sorted(
        queue_tasks[tid]["seq"] for tid in ids if queue_tasks[tid].get("seq") is not None
    )
    changed: list = []
    if len(slots) != len(ids):
        # 情形 3：有任务缺 seq，无法只做槽位置换 ⇒ 整队重编号（一次性）
        base = max((t.get("seq") or 0 for t in queue_tasks.values()), default=0)
        for offset, tid in enumerate(ids, start=1):
            task = queue_tasks[tid]
            if task.get("seq") != base + offset:
                task["seq"] = base + offset
                changed.append(tid)
    else:
        # 情形 1 / 2：槽位置换（顺序没变时每个都可能与自身相等 ⇒ 不写）
        for tid, slot in zip(ids, slots):
            task = queue_tasks[tid]
            if task.get("seq") != slot:
                task["seq"] = slot
                changed.append(tid)
    for tid in changed:
        persist_task(tid)
    if changed:
        logger.info("[queue] 排队顺序已物化到 seq：%d 个任务", len(changed))


def load_queue_order() -> None:
    """启动时重建 `queue_order`：先按 `seq`，缺 `seq` 的老任务按遗留顺序 → created_at 补队尾。

    与旧实现的差异只有一处：旧版把「`_queue_order.json` 里有、但任务不在」的条目丢掉，
    并把「任务是 queued 但文件里没登记」的按 created_at 补到队尾 —— 这里由
    「有 seq / 无 seq」自然区分，语义等价（见 QUEUE_MIGRATION_PLAN.md §3.2）。
    """
    queued = [
        tid for tid, task in queue_tasks.items()
        if task.get("status") == QueueTaskStatus.QUEUED
    ]
    with_seq = sorted(
        (tid for tid in queued if queue_tasks[tid].get("seq") is not None),
        key=lambda tid: queue_tasks[tid]["seq"],
    )
    missing = [tid for tid in queued if queue_tasks[tid].get("seq") is None]
    if missing:
        hint = _order_hint()
        missing.sort(key=lambda tid: (
            hint.get(tid, len(hint)), queue_tasks[tid].get("created_at", ""),
        ))
    queue_order[:] = with_seq + missing
    if missing:
        logger.warning(
            "[queue] 发现 %d 个排队任务缺 seq（老数据），已按遗留顺序/创建时间补到队尾并补写：%s",
            len(missing), missing,
        )
    persist_queue_order()
    logger.info("[queue] restored queue_order: %d tasks", len(queue_order))


# ─── 任务持久化（转发给存储门面）────────────────────────────

def persist_task(task_id: str) -> None:
    """把单个队列任务写入存储后端。

    失败**只告警不抛**（与旧实现一致）：这一步处在合成热路径上，写失败不该
    把合成打断。⚠️ 代价是「存储不可用 ⇒ 任务不可持久」，所以启动自检与
    `/api/version` 都要能看出当前后端（`queue_store_backend`）。
    """
    task = queue_tasks.get(task_id)
    if not task:
        return
    try:
        queue_store.save_task(task)
    except Exception as e:
        logger.warning("[queue] persist failed task=%s error=%s", task_id, e)


def delete_persisted_task(task_id: str) -> None:
    """从存储后端删除任务。"""
    try:
        queue_store.delete_task(task_id)
    except Exception as e:
        logger.warning("[queue] delete persisted failed task=%s error=%s", task_id, e)


def load_persisted_tasks() -> None:
    """启动时从存储后端恢复队列任务。

    ⚠️ `QUEUE_WORKER_ENABLED=0` 时**完全不加载** —— 见 QUEUE_MIGRATION_PLAN.md §5：
    一旦存储共享，第二个实例把另一台 `running` 的任务读进内存后会改判
    `INTERRUPTED`，用户再点删除就会把对方正在写的任务从库里删掉。
    B1 还没有租约，所以只能靠这个开关显式声明「本实例不接管队列」。
    """
    if not QUEUE_WORKER_ENABLED:
        logger.warning(
            "[queue] QUEUE_WORKER_ENABLED=0：本实例不接管任务队列"
            "（不加载、不恢复、不调度；队列接口返回空、提交返回 503）。"
            "多实例部署时请确保**另一台**已开启。"
        )
        return
    try:
        docs = queue_store.load_all_tasks()
    except Exception as e:
        logger.error("[queue] 读取队列存储失败（本实例队列为空）: %s", e)
        return
    for data in docs:
        task_id = data.get("id")
        if not task_id or task_id in queue_tasks:
            continue
        queue_tasks[task_id] = data
        # WebUI 重启后，带 TTS task id 的运行任务由启动逻辑恢复轮询。
        if data.get("status") in (QueueTaskStatus.RUNNING, QueueTaskStatus.SYNCING):
            if data.get("tts_task_id"):
                # 遗留的 tts-server 轮询任务：main 的启动逻辑会用 resume_polling 接管
                data["status"] = QueueTaskStatus.RUNNING
                data["message"] = data.get("message") or "WebUI 重启后恢复"
            else:
                # 在 backend 进程内合成的任务（mono / podcast）无法跨重启恢复。
                #
                # ⚠️ 判据必须是 tts_task_id，不能是 kind（2026-10-02 修）。
                # podcast 早就改成 backend 进程内合成（_execute_task → run_podcast_task），
                # 和 mono 一样没有任何跨重启接管路径；旧代码按 kind 分流，把 podcast
                # 任务永远留成 RUNNING —— 启动后没有任何代码再碰它，`cancel_requested`
                # 也永远无人检查 ⇒ 前端卡在「取消中」且删除不了（见 routes/queue.py 的
                # 孤儿分支）。恢复轮询的唯一真实判据是「有没有 tts_task_id」。
                data["status"] = QueueTaskStatus.INTERRUPTED
                data["message"] = "服务重启中断，可重新提交"
    logger.info("[queue] loaded %d persisted tasks", len(queue_tasks))
    # 用持久化的 seq 重建默认排序
    load_queue_order()
