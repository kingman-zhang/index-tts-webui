"""FastAPI 应用入口：CORS、启动恢复、路由挂载（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import queue_state as qs
from .config import TTS_URL, args, http_client, logger
from .queue_worker import process_queue, resume_polling
from .queue_state import load_persisted_tasks
from .routes import all_routers

app = FastAPI(title="Podcast WebUI Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in all_routers:
    app.include_router(_router)


@app.on_event("shutdown")
async def shutdown():
    await http_client.aclose()


@app.on_event("startup")
async def on_startup():
    """启动时恢复队列：先从磁盘加载持久化任务，再从 TTS 同步状态。"""
    # 1. 从磁盘加载持久化的队列任务
    load_persisted_tasks()

    # 2. 对持久化中"运行中"且有 tts_task_id 的任务，尝试恢复轮询
    persisted_running = [
        (tid, t) for tid, t in qs.queue_tasks.items()
        if t.get("status") == qs.QueueTaskStatus.RUNNING and t.get("tts_task_id")
    ]
    known_tts_ids = {t.get("tts_task_id") for _, t in persisted_running}

    if persisted_running:
        # 取第一个运行中任务恢复轮询
        first_id = persisted_running[0][0]
        logger.info("[startup] resuming polling for persisted task %s", first_id)
        asyncio.create_task(resume_polling(first_id))

    # 3. 从 TTS 服务同步：只恢复磁盘上没有的任务
    try:
        resp = await http_client.get(f"{TTS_URL}/api/tasks?limit=50", timeout=10.0)
        if resp.status_code == 200:
            tts_tasks = resp.json().get("tasks", [])
            extra_recovered = 0
            for tt in tts_tasks:
                tts_task_id = tt.get("task_id")
                if not tts_task_id or tts_task_id in known_tts_ids:
                    continue
                status = tt.get("status")
                if status not in ("pending", "running", "completed", "failed"):
                    continue

                if status in ("pending", "running"):
                    q_status = qs.QueueTaskStatus.RUNNING
                    q_message = tt.get("message") or "TTS 服务端运行中（WebUI 重启后恢复）"
                elif status == "completed":
                    q_status = qs.QueueTaskStatus.SUCCESS
                    q_message = tt.get("message") or "合成完成"
                else:
                    q_status = qs.QueueTaskStatus.FAILED
                    q_message = tt.get("error") or "合成失败"

                raw_progress = tt.get("progress", 0) or 0
                q_id = f"recovered_{tts_task_id}"
                qs.queue_tasks[q_id] = {
                    "id": q_id,
                    "project_name": f"恢复任务 ({tts_task_id})",
                    "lines": [],
                    "voices": {},
                    "silence": {},
                    "params": {},
                    "glossary_enabled": False,
                    "status": q_status,
                    "progress": max(0.0, min(1.0, float(raw_progress) / 100.0)),
                    "current_line": tt.get("current_line", 0),
                    "total_lines": tt.get("total_lines", 0),
                    "message": q_message,
                    "created_at": tt.get("created_at", datetime.now().isoformat()),
                    "tts_task_id": tts_task_id,
                    "audio_url": f"/api/podcast/audio/{tts_task_id}" if q_status == qs.QueueTaskStatus.SUCCESS else None,
                    "output_path": tt.get("output_path"),
                    "duration_sec": tt.get("duration_sec"),
                    "error": tt.get("error") if q_status == qs.QueueTaskStatus.FAILED else None,
                    "cancel_requested": False,
                }
                qs.persist_task(q_id)
                extra_recovered += 1

            # 如果 TTS 端有运行中任务且磁盘没有对应记录，也恢复轮询
            if not persisted_running:
                tts_running = [
                    tid for tid, t in qs.queue_tasks.items()
                    if t.get("status") == qs.QueueTaskStatus.RUNNING and tid.startswith("recovered_")
                ]
                if tts_running:
                    logger.info("[startup] resuming polling for recovered tts task %s", tts_running[0])
                    asyncio.create_task(resume_polling(tts_running[0]))

            logger.info("[startup] recovered %d extra tasks from TTS", extra_recovered)
        else:
            logger.warning("[startup] tts tasks query failed status=%s", resp.status_code)
    except Exception as e:
        logger.warning("[startup] failed to sync tts tasks: %s", e)

    # 4. 如果有排队中的任务，触发队列处理
    if qs.queue_order:
        logger.info("[startup] %d queued tasks pending, resuming queue", len(qs.queue_order))
        asyncio.create_task(process_queue())

    logger.info("[startup] queue ready: %d tasks total", len(qs.queue_tasks))


def run():
    import uvicorn
    print(f">> Podcast WebUI Backend")
    print(f"   TTS URL:  {TTS_URL}")
    print(f"   Data dir: {args.data_dir}")
    print(f"   Listen:   {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
