"""队列执行器：提交任务到 TTS、轮询状态、断连容错（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import httpx
from fastapi import HTTPException

from .config import TTS_URL, http_client, logger
from .stores import load_glossary
from . import queue_state as qs
from .membership import service as member_svc

# 每用户并发上限（同一用户同时运行的任务数）；全局硬上限防失控。
# 未登录/无会员环境的任务归属 "_anon"，退化为全局并发 = USER_CONCURRENCY。
USER_CONCURRENCY = max(1, min(int(os.environ.get("USER_CONCURRENCY", "3").strip() or 3), 10))
GLOBAL_CONCURRENCY = max(USER_CONCURRENCY, int(os.environ.get("USER_CONCURRENCY_GLOBAL", "12").strip() or 12))


def validate_queue_lines(lines: list) -> None:
    if not lines:
        raise HTTPException(400, "台词内容不能为空")
    blank_lines = []
    for index, line in enumerate(lines, start=1):
        text = line.get("text") if isinstance(line, dict) else None
        if not isinstance(text, str) or not text.strip():
            blank_lines.append(index)
    if blank_lines:
        preview = ", ".join(str(i) for i in blank_lines[:10])
        suffix = " 等" if len(blank_lines) > 10 else ""
        raise HTTPException(400, f"第 {preview}{suffix} 行台词为空，请补充内容后再提交")


def refund_task_points(task: dict) -> None:
    """会员预扣积分任务在非成功终态时退还积分（幂等，异常不外抛）。

    供 process_queue / resume_polling 的 finally 与取消入口复用。
    """
    if not (member_svc.ENFORCE and task.get("member_id") and task.get("points_charged")):
        return
    if task.get("status") == qs.QueueTaskStatus.SUCCESS:
        return
    try:
        member_svc.refund_task_charge(
            task["member_id"], task.get("id") or "",
            int(task.get("points_charged") or 0),
            task.get("points_charge_log") or "",
        )
        task["points_charged"] = 0
        task["points_charge_log"] = ""
    except Exception as e:  # 退款失败不阻断队列收尾
        logger.warning("[member] refund failed task=%s error=%s", task.get("id"), e)


async def resume_polling(webui_task_id: str):
    """恢复对 TTS 运行中任务的轮询。"""
    task = qs.queue_tasks.get(webui_task_id)
    if not task or not task.get("tts_task_id"):
        return
    tts_task_id = task["tts_task_id"]
    qs.current_task_id = webui_task_id
    qs.running_ids.add(webui_task_id)
    logger.info("[resume] polling webui=%s tts=%s", webui_task_id, tts_task_id)

    poll_errors = 0
    try:
        while True:
            await asyncio.sleep(1.5)
            try:
                r = await http_client.get(f"{TTS_URL}/api/task/{tts_task_id}", timeout=10.0)
                if r.status_code == 404:
                    # TTS 服务重启后任务可能丢失，标记为失败但保留数据
                    raise Exception("TTS 任务不存在（TTS 服务可能已重启）")
                if r.status_code != 200:
                    raise Exception(f"TTS 状态查询 HTTP {r.status_code}: {r.text[:500]}")
                payload = r.json()
                poll_errors = 0
                raw_progress = payload.get("progress", 0) or 0
                task["progress"] = max(0.0, min(1.0, float(raw_progress) / 100.0))
                task["current_line"] = payload.get("current_line", 0)
                task["total_lines"] = payload.get("total_lines", 0)
                task["message"] = payload.get("message", "")

                status = payload.get("status")
                if status in ("success", "completed"):
                    task["status"] = qs.QueueTaskStatus.SUCCESS
                    task["progress"] = 1.0
                    task["audio_url"] = f"/api/podcast/audio/{tts_task_id}"
                    task["duration_sec"] = payload.get("duration_sec")
                    task["output_path"] = payload.get("output_path")
                    task["message"] = payload.get("message") or "合成完成"
                    qs.persist_task(webui_task_id)
                    logger.info("[resume] completed webui=%s tts=%s", webui_task_id, tts_task_id)
                    break
                elif status == "failed":
                    task["status"] = qs.QueueTaskStatus.FAILED
                    task["error"] = payload.get("error", "未知错误")
                    qs.persist_task(webui_task_id)
                    break
            except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError) as e:
                poll_errors += 1
                logger.warning("[resume] poll error webui=%s retry=%d error=%s", webui_task_id, poll_errors, e)
                task["message"] = f"TTS 连接中断，正在重试（第 {poll_errors} 次）"
                if poll_errors >= 120:
                    raise Exception("连续无法连接 TTS 服务，已停止轮询；任务数据已保留")
                continue
            except Exception as e:
                task["status"] = qs.QueueTaskStatus.FAILED
                task["error"] = str(e)
                qs.persist_task(webui_task_id)
                break
    except Exception as e:
        logger.exception("[resume] failed webui=%s error=%s", webui_task_id, e)
        task["status"] = qs.QueueTaskStatus.FAILED
        task["error"] = str(e)
        qs.persist_task(webui_task_id)
    finally:
        refund_task_points(task)
        task["finished_at"] = datetime.now().isoformat()
        qs.persist_task(webui_task_id)
        qs.running_ids.discard(webui_task_id)
        qs.current_task_id = next(iter(qs.running_ids), None)
        asyncio.create_task(process_queue())


async def process_queue():
    """并发调度器：按排队顺序挑出可启动的任务（每用户并发数受 USER_CONCURRENCY 限制）。

    并发语义（2026-09-21）：USER_CONCURRENCY 是**每用户**的并发上限（默认 3），
    不是全平台上限——每个用户各自可同时运行 N 个任务；全局另有硬上限
    USER_CONCURRENCY_GLOBAL（默认 12）防失控。未登录/无会员环境统一视为
    单个 "_anon" 用户，退化为全局串行（上限 N）。
    """
    async with qs.queue_lock:
        while qs.queue_order:
            if len(qs.running_ids) >= GLOBAL_CONCURRENCY:
                return
            # 从队首找第一个"所属用户运行数未满"的任务（跳过超限的，保持 FIFO 公平）
            picked = None
            for tid in list(qs.queue_order):
                task = qs.queue_tasks.get(tid)
                if not task:
                    qs.queue_order.remove(tid)
                    continue
                owner = task.get("member_id") or "_anon"
                if _user_running_count(owner) >= USER_CONCURRENCY:
                    continue
                picked = tid
                break
            if not picked:
                return
            qs.queue_order.remove(picked)
            qs.running_ids.add(picked)
            task = qs.queue_tasks[picked]
            task["status"] = qs.QueueTaskStatus.RUNNING
            task["started_at"] = datetime.now().isoformat()
            qs.persist_task(picked)
            if qs.current_task_id is None:
                qs.current_task_id = picked
            asyncio.create_task(_execute_task(picked))


def _user_running_count(owner: str) -> int:
    count = 0
    for tid in qs.running_ids:
        t = qs.queue_tasks.get(tid)
        if t and (t.get("member_id") or "_anon") == owner:
            count += 1
    return count


async def _execute_task(task_id: str) -> None:
    """执行单个队列任务（播客/配音在 backend 适配层合成；历史 tts-server 任务轮询）。"""
    task = qs.queue_tasks[task_id]
    try:
        validate_queue_lines(task.get("lines", []))
        # 应用术语替换（播客与配音模式共用；req_data["lines"] 与 task["lines"] 是同一列表）
        if task.get("glossary_enabled", True):
            terms = load_glossary()
            if terms:
                for line in task["lines"]:
                    for t in terms:
                        line["text"] = line["text"].replace(t["original"], t["replacement"])

        # 配音/播客模式：不进 tts-server 播客引擎，走引擎适配层在 backend 进程内合成
        # （TTS_ENGINE_PREFERRED 指向云引擎时无需本地 tts-server 在线）
        if task.get("kind") == "mono":
            from .mono_runner import run_mono_task
            await run_mono_task(task)
            return
        if task.get("kind") == "podcast":
            from .podcast_runner import run_podcast_task
            await run_podcast_task(task)
            return

        req_data = {
            "lines": task["lines"],
            "voices": task["voices"],
            "silence": task["silence"],
            "params": task["params"],
        }

        resp = await http_client.post(f"{TTS_URL}/api/podcast", json=req_data, timeout=30.0)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", "提交 TTS 失败")
            except Exception:
                detail = resp.text[:1000] or f"HTTP {resp.status_code}"
            raise Exception(detail)
        tts_task_id = resp.json().get("task_id")
        if not tts_task_id:
            raise Exception("TTS 未返回 task_id")
        task["tts_task_id"] = tts_task_id
        qs.persist_task(task_id)
        logger.info("[queue] submitted task=%s tts_task=%s", task_id, tts_task_id)

        # 轮询 TTS 任务状态。短暂断连不能直接判失败，否则 GPU 任务仍会继续运行。
        poll_errors = 0
        while True:
            await asyncio.sleep(1.5)
            try:
                r = await http_client.get(f"{TTS_URL}/api/task/{tts_task_id}", timeout=10.0)
                if r.status_code == 404:
                    task["status"] = qs.QueueTaskStatus.INTERRUPTED
                    task["message"] = "TTS 任务不存在，可重新提交"
                    task["error"] = "TTS 任务不存在"
                    qs.persist_task(task_id)
                    break
                if r.status_code != 200:
                    raise Exception(f"TTS 状态查询 HTTP {r.status_code}: {r.text[:500]}")
                payload = r.json()
                poll_errors = 0
                raw_progress = payload.get("progress", 0) or 0
                task["progress"] = max(0.0, min(1.0, float(raw_progress) / 100.0))
                task["current_line"] = payload.get("current_line", 0)
                task["total_lines"] = payload.get("total_lines", 0)
                task["message"] = payload.get("message", "")

                status = payload.get("status")
                if status in ("success", "completed"):
                    task["status"] = qs.QueueTaskStatus.SUCCESS
                    task["progress"] = 1.0
                    task["output_path"] = payload.get("output_path")
                    task["audio_url"] = f"/api/podcast/audio/{tts_task_id}"
                    task["duration_sec"] = payload.get("duration_sec")
                    task["message"] = payload.get("message") or "合成完成"
                    qs.persist_task(task_id)
                    logger.info("[queue] completed task=%s tts_task=%s", task_id, tts_task_id)
                    break
                elif status == "failed":
                    task["status"] = qs.QueueTaskStatus.FAILED
                    task["error"] = payload.get("error", "未知错误")
                    qs.persist_task(task_id)
                    break
                elif task.get("cancel_requested"):
                    task["status"] = qs.QueueTaskStatus.CANCELLED
                    break
            except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError) as e:
                poll_errors += 1
                logger.warning(
                    "[queue] status poll disconnected task=%s tts_task=%s retry=%d error=%s",
                    task_id, tts_task_id, poll_errors, e,
                )
                task["status"] = qs.QueueTaskStatus.SYNCING
                task["message"] = f"TTS 连接中断，等待恢复（第 {poll_errors} 次）"
                qs.persist_task(task_id)
                # 任务已在 TTS 服务端创建，继续轮询，不能把它误判为失败。
                if poll_errors >= 120:
                    task["message"] = "连续无法连接 TTS 服务，可重新提交"
                    task["error"] = "连续无法连接 TTS 服务"
                    task["status"] = qs.QueueTaskStatus.INTERRUPTED
                    qs.persist_task(task_id)
                    break
                continue
            except Exception as e:
                task["status"] = qs.QueueTaskStatus.FAILED
                task["error"] = str(e)
                break

    except FileNotFoundError as e:
        # 参考音频等文件缺失是永久性配置错误，重试无意义，不能误报"连接中断"
        logger.error("[queue] missing file task=%s error=%s", task_id, e)
        task["status"] = qs.QueueTaskStatus.FAILED
        task["message"] = "参考音频文件不存在，请检查任务音色配置"
        task["error"] = f"参考音频文件不存在: {e}"
        qs.persist_task(task_id)
    except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError) as e:
        logger.error("[queue] request disconnected before task tracking task=%s error=%s", task_id, e)
        task["status"] = qs.QueueTaskStatus.INTERRUPTED
        task["message"] = "提交 TTS 时连接中断，可重新提交"
        task["error"] = f"提交 TTS 时连接中断: {e}"
        qs.persist_task(task_id)
    except Exception as e:
        logger.exception("[queue] failed task=%s error=%s", task_id, e)
        task["status"] = qs.QueueTaskStatus.FAILED
        task["message"] = "任务执行失败"
        task["error"] = str(e)
        qs.persist_task(task_id)
    finally:
        refund_task_points(task)
        task["finished_at"] = datetime.now().isoformat()
        qs.persist_task(task_id)
        qs.running_ids.discard(task_id)
        if task_id == qs.current_task_id:
            # 兼容展示：current 指向最早启动的运行中任务
            qs.current_task_id = next(iter(qs.running_ids), None)
        # 有空位则继续调度下一个
        asyncio.create_task(process_queue())
