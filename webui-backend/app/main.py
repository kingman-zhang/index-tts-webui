"""FastAPI 应用入口：CORS、启动恢复、路由挂载（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import queue_state as qs
# import 顺序有语义，别动：config 会加载 .env，而 name_punct / year_norm /
# number_norm 的开关是**模块级**读 os.environ 的（.env 未加载就会落到缺省值，
# 表现为「.env 里明明写了 NAME_PUNCT_NORMALIZE=0 却不生效」）。
# build_info 自己就把 config 放在首位，这里跟着同一顺序。
from .config import GLOSSARY_PATH, TTS_URL, args, http_client, logger
from . import build_info
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
    # 0. 运行实例自检（2026-09-28）
    # 背景：同一个下午踩了两次「看不见」的坑 ——
    #   ① 术语表缺失时 load_glossary() 静默返回空列表，表现为「词条配了却不生效」；
    #   ② 代码 pull 了但进程没重启，跑的是内存里的旧模块 —— 磁盘上怎么查都对，
    #      离线复现链路也正确，就是线上没变化。
    # 故启动时把「这个进程是谁」一次性打全：git HEAD / 启动时刻 / 数据目录 /
    # 各道前处理开关。这些符号只有新代码才有，日志里出现即证明加载的是哪一版。
    # 同一份快照也挂在 GET /api/version（见 app/build_info.py 与 routes/system.py）。
    _boot = build_info.snapshot()
    _tp = _boot.get("text_pipeline") or {}
    logger.info(
        "[startup] 运行实例 git=%s 启动于 %s 数据目录=%s",
        _boot.get("git_head") or "?", _boot["process_started_at"], _boot.get("data_dir"),
    )
    logger.info(
        "[startup] 文本前处理：术语表 %s 条（合成展开 %s）"
        " / 人名分隔号 %s(%s) / 年份读法 %s / 时间读法 %s / 数值读法 %s / 数字读法 %s",
        _tp.get("glossary_terms"), _tp.get("glossary_terms_for_synthesis"),
        "开" if _tp.get("name_punct_enabled") else "关", _tp.get("name_punct_target"),
        "开" if _tp.get("year_norm_enabled") else "关",
        "开" if _tp.get("time_norm_enabled") else "关",
        "开" if _tp.get("num_value_normalize") else "关",
        "开" if _tp.get("number_norm_enabled") else "关",
    )
    # 引擎链路（2026-09-29）：注册顺序即优先级，能力由各引擎自己声明。
    # 「这次为什么走了某个引擎」此前完全不可见，只能靠猜；这一行 + /api/version
    # 的 engines 字段把它变成可核对的事实。
    _engines = (_boot.get("engines") or {}).get("registered") or []
    if _engines:
        logger.info(
            "[startup] 引擎优先级 %s",
            " → ".join(
                "%s(上限%s/并发%s)" % (
                    e.get("name"),
                    e.get("max_input_chars") or "不限",
                    e.get("max_concurrency") or "env",
                )
                for e in _engines
            ),
        )
    else:
        logger.warning("[startup] 没有任何 TTS 引擎被注册 —— 所有合成都将失败")
    if _boot.get("stale_sources"):
        logger.warning(
            "[startup] !! 以下源文件在进程启动之后被改动，进程内仍是旧版本：%s"
            " —— 需重启 backend", _boot["stale_sources"],
        )
    if not _tp.get("glossary_terms"):
        logger.warning(
            "[startup] 全局术语表缺失或为空：%s —— 所有术语替换都不会生效。"
            "该文件已随代码入库，服务器上执行 git pull 即可恢复；"
            "若数据目录指错了也会这样，核对上一行的「数据目录」。",
            GLOSSARY_PATH,
        )

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
    try:
        from .engines.factory import config_source, build_registry
        resources = build_registry().engines[0].resources
        pool = [cfg.id for cfg, _ in resources]
        source = config_source()
        # `TTS_URL` 缺省即派生自池里第一个 local ⇒ 通常与池内某台同址。
        # 必须把这层「同址」点明：否则「合成不读这个变量」会被读成
        # 「合成不用这台」，而事实恰好相反 —— 那台既管音色、也参与合成
        # （2026-09-30 用户看到旧文案后就是这样问的）。
        same = next(
            (cfg.id for cfg, eng in resources
             if (getattr(eng, "tts_url", None) or "").rstrip("/") == TTS_URL),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - 横幅不能把启动带崩
        pool, source, same = [], f"读取失败：{exc}", None
    print(f">> Podcast WebUI Backend")
    print(f"   合成资源池: {pool or '（空！配置有误）'}")
    print(f"   配置来源:   {source}")
    print(f"   TTS URL:    {TTS_URL}"
          f"（音色管理与探针；合成不读这个变量，而是按上面的资源池调度）")
    if same:
        print(f"               └ 与池内 {same} 同址：该台同样参与合成，只是不通过这个变量")
    print(f"   Data dir:   {args.data_dir}")
    print(f"   Listen:     {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
