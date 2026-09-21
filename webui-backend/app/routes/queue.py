"""任务队列端点（原 server.py「任务队列」的 HTTP 层，行为不变）。

执行逻辑在 queue_worker.process_queue，状态与持久化在 queue_state。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from .. import queue_state as qs
from ..config import logger
from ..models import QueueTaskModel, QueueTaskNameModel
from ..queue_worker import process_queue, refund_task_points, validate_queue_lines
from ..membership import get_optional_user
from ..membership.service import MemberError
from ..membership import service as member_svc

router = APIRouter()


def _isolation_on() -> bool:
    """数据隔离开关：会员或登录体系开启时生效。"""
    return member_svc.ENFORCE or member_svc.REQUIRE_LOGIN


def _check_access(task: dict, user: Optional[dict]):
    """任务归属校验：隔离开启时，登录用户只能访问自己的任务。

    无归属（member_id 为空）的遗留任务视为共享数据，仅在隔离未开启时可见；
    可用 tools/migrate_owners.py 一次性划归指定用户。
    """
    if not _isolation_on():
        return
    if not user or task.get("member_id") != user["user_id"]:
        raise HTTPException(404, "任务不存在")


@router.post("/api/queue/submit")
async def submit_to_queue(
    task: QueueTaskModel,
    user: Optional[dict] = Depends(get_optional_user),
):
    """提交任务到队列。

    会员扣费：MEMBER_ENFORCE=1 时登录用户按字数预扣积分，余额不足返回 402；
    未开启时行为与旧版完全一致（不含任何会员逻辑）。
    """
    validate_queue_lines(task.lines)
    task_id = f"q_{uuid.uuid4().hex[:10]}"
    entry = {
        "id": task_id,
        "project_name": task.project_name,
        "kind": task.kind,
        "lines": task.lines,
        "voices": task.voices,
        "silence": task.silence,
        "params": task.params,
        "glossary_enabled": task.glossary_enabled,
        "status": qs.QueueTaskStatus.QUEUED,
        "progress": 0,
        "current_line": 0,
        "total_lines": len(task.lines),
        "message": "排队中",
        "created_at": datetime.now().isoformat(),
        "cancel_requested": False,
    }
    if member_svc.ENFORCE or member_svc.REQUIRE_LOGIN:
        if not user:
            raise HTTPException(401, "请先登录后再提交合成任务")
    if user:
        entry["member_id"] = user["user_id"]  # 登录态下总是记录归属（数据隔离依据）
    if member_svc.ENFORCE:
        try:
            charge = member_svc.charge_for_task(user, task.lines, task_id)
        except MemberError as e:
            raise HTTPException(e.code, e.message)
        if charge["log_id"]:
            entry["points_charged"] = member_svc.estimate_task_cost(task.lines)
            entry["points_charge_log"] = charge["log_id"]
            entry["message"] = f"排队中（已预扣 {entry['points_charged']} 积分）"
    qs.queue_tasks[task_id] = entry
    qs.queue_order.append(task_id)
    qs.persist_task(task_id)
    qs.persist_queue_order()
    # 触发队列处理
    import asyncio
    asyncio.create_task(process_queue())
    return {"task_id": task_id, "status": "queued", "queue_position": len(qs.queue_order)}


@router.get("/api/queue")
async def list_queue(user: Optional[dict] = Depends(get_optional_user)):
    """列出当前用户可见的队列任务。排序：运行中 → 排队中(按执行顺序) → 终态(按创建时间倒序)。"""
    if _isolation_on() and not user:
        raise HTTPException(401, "请先登录后查看任务队列")
    running_tasks = []
    queued_tasks = []
    terminal_tasks = []
    for t in qs.queue_tasks.values():
        if _isolation_on() and t.get("member_id") != user["user_id"]:
            continue  # 只看自己的任务
        st = t.get("status")
        # 给每个任务标注 queue_position
        tid = t.get("id", "")
        if tid in qs.queue_order:
            t["queue_position"] = qs.queue_order.index(tid) + 1
        else:
            t["queue_position"] = None
        if st in (qs.QueueTaskStatus.RUNNING, qs.QueueTaskStatus.SYNCING):
            running_tasks.append(t)
        elif st == qs.QueueTaskStatus.QUEUED:
            queued_tasks.append(t)
        elif st == qs.QueueTaskStatus.PAUSED:
            pass
        else:
            terminal_tasks.append(t)
    # 排队任务按 queue_order 顺序排列
    queued_tasks.sort(key=lambda t: qs.queue_order.index(t["id"]) if t["id"] in qs.queue_order else 999)
    paused_tasks = [t for t in qs.queue_tasks.values() if t.get("status") == qs.QueueTaskStatus.PAUSED]
    paused_tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    # 终态任务按创建时间倒序
    terminal_tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    tasks = running_tasks + queued_tasks + paused_tasks + terminal_tasks
    return {
        "tasks": tasks,
        "count": len(tasks),
        "current": qs.current_task_id,
        "queued": len(qs.queue_order),
        "queue_order": list(qs.queue_order),
    }


@router.put("/api/queue/{task_id}")
async def update_queue_task(
    task_id: str,
    payload: QueueTaskModel,
    user: Optional[dict] = Depends(get_optional_user),
):
    """编辑尚未执行的排队任务。运行中及终态任务不可修改。

    已预扣积分的任务改稿后按新字数多退少补（仅 MEMBER_ENFORCE=1）。
    """
    task = qs.queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    _check_access(task, user)
    if task.get("status") != qs.QueueTaskStatus.QUEUED:
        raise HTTPException(409, "只有尚未执行的排队任务可以编辑")
    validate_queue_lines(payload.lines)
    if member_svc.ENFORCE and task.get("member_id"):
        try:
            rb = member_svc.rebalance_task_charge(
                {"user_id": task["member_id"]}, payload.lines, task_id,
                int(task.get("points_charged") or 0), task.get("points_charge_log") or "",
            )
        except MemberError as e:
            raise HTTPException(e.code, e.message)
        task["points_charged"] = rb["charged"]
    task.update({
        "project_name": payload.project_name,
        "lines": payload.lines,
        "voices": payload.voices,
        "silence": payload.silence,
        "params": payload.params,
        "glossary_enabled": payload.glossary_enabled,
        "total_lines": len(payload.lines),
        "message": "已更新，等待执行",
        "updated_at": datetime.now().isoformat(),
    })
    qs.persist_task(task_id)
    return task


@router.patch("/api/queue/{task_id}/name")
async def update_queue_task_name(
    task_id: str,
    payload: QueueTaskNameModel,
    user: Optional[dict] = Depends(get_optional_user),
):
    """修改尚未执行任务的名称。"""
    task = qs.queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    _check_access(task, user)
    editable_statuses = {
        qs.QueueTaskStatus.QUEUED,
        qs.QueueTaskStatus.PAUSED,
        qs.QueueTaskStatus.FAILED,
        qs.QueueTaskStatus.INTERRUPTED,
        qs.QueueTaskStatus.CANCELLED,
    }
    if task.get("status") not in editable_statuses:
        raise HTTPException(409, "任务正在执行或已完成，当前不能修改名称")
    name = payload.project_name.strip()
    if not name:
        raise HTTPException(400, "任务名称不能为空")
    if len(name) > 100:
        raise HTTPException(400, "任务名称不能超过 100 个字符")
    task["project_name"] = name
    task["updated_at"] = datetime.now().isoformat()
    qs.persist_task(task_id)
    return {"task_id": task_id, "project_name": name, "status": task["status"]}


@router.post("/api/queue/{task_id}/retry")
async def retry_queue_task(task_id: str, user: Optional[dict] = Depends(get_optional_user)):
    """重新排队执行失败、中断、暂停或取消的任务，从第一行重新生成。"""
    task = qs.queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    _check_access(task, user)
    if task.get("status") not in (qs.QueueTaskStatus.FAILED, qs.QueueTaskStatus.INTERRUPTED, qs.QueueTaskStatus.PAUSED, qs.QueueTaskStatus.CANCELLED):
        raise HTTPException(409, "只有失败、中断、暂停或取消任务可以重新提交")
    if task_id in qs.running_ids:
        raise HTTPException(409, "任务当前仍在执行")
    task.update({
        "status": qs.QueueTaskStatus.QUEUED,
        "progress": 0,
        "current_line": 0,
        "total_lines": len(task.get("lines", [])),
        "message": "已重新排队",
        "error": None,
        "tts_task_id": None,
        "audio_url": None,
        "output_path": None,
        "duration_sec": None,
        "cancel_requested": False,
        "started_at": None,
        "finished_at": None,
        "retried_at": datetime.now().isoformat(),
    })
    if task_id not in qs.queue_order:
        qs.queue_order.append(task_id)
    # 会员扣费：重试重新预扣（此前失败/中断时已退款）
    if member_svc.ENFORCE and task.get("member_id"):
        cost = member_svc.estimate_task_cost(task.get("lines", []))
        try:
            charge = member_svc.charge_for_task({"user_id": task["member_id"]}, task.get("lines", []), task_id)
        except MemberError as e:
            raise HTTPException(e.code, e.message)
        task["points_charged"] = cost if charge["log_id"] else 0
        task["points_charge_log"] = charge["log_id"]
    qs.persist_task(task_id)
    qs.persist_queue_order()
    import asyncio
    asyncio.create_task(process_queue())
    return {"task_id": task_id, "status": task["status"], "queue_position": qs.queue_order.index(task_id) + 1}


@router.post("/api/queue/bulk-pause")
async def pause_queued_tasks(kind: Optional[str] = None, user: Optional[dict] = Depends(get_optional_user)):
    """暂停当前用户排队中的任务；正在合成的任务不受影响。

    kind 可选（podcast/mono）：只暂停该类型的任务（前端按 tab 操作）；
    缺省时暂停全部排队任务（兼容旧行为）。
    """
    paused = []
    keep = []
    for task_id in list(qs.queue_order):
        task = qs.queue_tasks.get(task_id)
        mine = not _isolation_on() or (user and task and task.get("member_id") == user["user_id"])
        kind_match = not kind or task.get("kind") == kind
        if task and mine and kind_match and task.get("status") == qs.QueueTaskStatus.QUEUED:
            task["status"] = qs.QueueTaskStatus.PAUSED
            task["message"] = "已暂停"
            task["paused_at"] = datetime.now().isoformat()
            qs.persist_task(task_id)
            paused.append(task_id)
        else:
            keep.append(task_id)
    qs.queue_order[:] = keep
    qs.persist_queue_order()
    return {"paused": paused, "count": len(paused)}


@router.post("/api/queue/bulk-resume")
async def resume_paused_tasks(kind: Optional[str] = None, user: Optional[dict] = Depends(get_optional_user)):
    """将暂停任务按原创建时间恢复到队列末尾。

    kind 可选（podcast/mono）：只恢复该类型的任务；缺省时恢复全部。
    """
    resumed = []
    paused = [
        task for task in qs.queue_tasks.values()
        if task.get("status") == qs.QueueTaskStatus.PAUSED
        and (not kind or task.get("kind") == kind)
        and (not _isolation_on() or (user and task.get("member_id") == user["user_id"]))
    ]
    paused.sort(key=lambda task: task.get("created_at", ""))
    for task in paused:
        task_id = task["id"]
        task["status"] = qs.QueueTaskStatus.QUEUED
        task["message"] = "已重新排队"
        task["resumed_at"] = datetime.now().isoformat()
        qs.queue_order.append(task_id)
        qs.persist_task(task_id)
        resumed.append(task_id)
    qs.persist_queue_order()
    if resumed:
        import asyncio
        asyncio.create_task(process_queue())
    return {"resumed": resumed, "count": len(resumed)}


@router.get("/api/queue/{task_id}")
async def get_queue_task(task_id: str, user: Optional[dict] = Depends(get_optional_user)):
    """获取队列中单个任务状态。"""
    if task_id not in qs.queue_tasks:
        raise HTTPException(404, "任务不存在")
    _check_access(qs.queue_tasks[task_id], user)
    return qs.queue_tasks[task_id]


@router.delete("/api/queue/{task_id}")
async def cancel_queue_task(task_id: str, user: Optional[dict] = Depends(get_optional_user)):
    """取消/删除队列任务。排队中直接删除，运行中标记取消。"""
    if task_id not in qs.queue_tasks:
        raise HTTPException(404, "任务不存在")
    task = qs.queue_tasks[task_id]
    _check_access(task, user)
    if task["status"] == qs.QueueTaskStatus.QUEUED:
        # 排队中：直接从队列移除
        if task_id in qs.queue_order:
            qs.queue_order.remove(task_id)
        task["status"] = qs.QueueTaskStatus.CANCELLED
        task["message"] = "已取消"
        refund_task_points(task)  # 退还预扣积分（无则空操作）
        qs.persist_task(task_id)
        qs.persist_queue_order()
        return {"cancelled": task_id}
    elif task["status"] == qs.QueueTaskStatus.RUNNING:
        # 运行中：标记取消。执行器（mono_runner/podcast_runner）在每个分段启动前
        # 检查该标记——排队中的分段立即放弃，进行中的分段（≤并发数）合成完后终止。
        task["cancel_requested"] = True
        task["message"] = "正在取消，等待进行中的合成结束"
        qs.persist_task(task_id)
        return {"cancelling": task_id}
    else:
        # 已完成/失败：从列表和磁盘删除
        del qs.queue_tasks[task_id]
        qs.delete_persisted_task(task_id)
        return {"deleted": task_id}


@router.delete("/api/queue")
async def clear_finished_tasks(user: Optional[dict] = Depends(get_optional_user)):
    """清空当前用户已完成/失败/取消的任务。"""
    to_remove = [tid for tid, t in qs.queue_tasks.items()
                 if t["status"] in (qs.QueueTaskStatus.SUCCESS, qs.QueueTaskStatus.FAILED, qs.QueueTaskStatus.CANCELLED)
                 and (not _isolation_on() or (user and t.get("member_id") == user["user_id"]))]
    for tid in to_remove:
        del qs.queue_tasks[tid]
        qs.delete_persisted_task(tid)
    return {"cleared": len(to_remove), "remaining": len(qs.queue_tasks)}


@router.patch("/api/queue/reorder")
async def reorder_queue(payload: dict, user: Optional[dict] = Depends(get_optional_user)):
    """拖拽排序：接收新的排队任务 ID 顺序，重写 queue_order。

    kind 可选（podcast/mono）：task_ids 只含该类型的排队任务，重排后
    该类型任务按新顺序填回原类型槽位，其余类型任务的相对顺序保持不变；
    缺省时 task_ids 必须覆盖全部排队任务（兼容旧行为）。
    """
    new_order = payload.get("task_ids", [])
    kind = payload.get("kind")
    if not isinstance(new_order, list):
        raise HTTPException(400, "task_ids 必须是数组")
    async with qs.queue_lock:
        old_queued = [tid for tid in qs.queue_order
                      if (t := qs.queue_tasks.get(tid)) and t.get("status") == qs.QueueTaskStatus.QUEUED]
        if kind:
            # 部分重排：校验 task_ids 恰好是该类型的全部排队任务
            kind_queued = [tid for tid in old_queued if qs.queue_tasks[tid].get("kind") == kind]
            if set(new_order) != set(kind_queued) or len(new_order) != len(kind_queued):
                raise HTTPException(400, f"任务列表与 {kind} 类型的排队任务不匹配")
            if _isolation_on():
                for tid in new_order:
                    if qs.queue_tasks[tid].get("member_id") != user["user_id"]:
                        raise HTTPException(404, f"任务 {tid} 不存在")
            it = iter(new_order)
            final_order = [next(it) if tid in set(kind_queued) else tid for tid in old_queued]
        else:
            # 全量重排（旧行为）
            old_set, new_set = set(old_queued), set(new_order)
            if new_set != old_set:
                missing, extra = old_set - new_set, new_set - old_set
                detail = []
                if missing:
                    detail.append(f"缺少: {missing}")
                if extra:
                    detail.append(f"多余: {extra}")
                raise HTTPException(400, f"任务列表不匹配 {'; '.join(detail)}")
            for tid in new_order:
                t = qs.queue_tasks.get(tid)
                if _isolation_on() and t.get("member_id") != user["user_id"]:
                    raise HTTPException(404, f"任务 {tid} 不存在")
            final_order = new_order
        qs.queue_order.clear()
        qs.queue_order.extend(final_order)
        qs.persist_queue_order()
    logger.info("[queue] reordered: %s", new_order)
    return {"queue_order": list(qs.queue_order)}
