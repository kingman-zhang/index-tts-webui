"""队列执行器：提交任务到 TTS、轮询状态、断连容错（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import httpx
from fastapi import HTTPException

from .config import TTS_URL, http_client, logger
# import 顺序有语义：config 先加载 .env，而 name_punct / year_norm / num_value_norm /
# number_norm 的开关是**模块级**读 os.environ 的。顺序反了会落到缺省值。
from . import name_punct, num_value_norm, number_norm, year_norm
from .stores import apply_glossary, load_glossary_for_synthesis
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
                elif task.get("cancel_requested"):
                    task["status"] = qs.QueueTaskStatus.CANCELLED
                    task["message"] = "已取消"
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
        # 术语替换只作用于「送去合成的那一份文本」，不回写 task["lines"]（2026-09-28）：
        # 任务详情、重试扣费、磁盘存档一律保持用户原文，替换仅是合成细节。
        # 词表 = 全局库（超管）+ 该用户自定义库（同名优先）；未登录仅用全局库。
        # 用 _for_synthesis 版本：含中点分隔号的词条会展开成全部码位变体，
        # 避免「原文里的中点换了写法 → 词条静默失效」（2026-09-28）。
        synth_lines = task["lines"]
        if task.get("glossary_enabled", True):
            terms = load_glossary_for_synthesis(task.get("member_id"))
            if terms:
                synth_lines = apply_glossary(task["lines"], terms)
        # 人名分隔号（中点）归一化：把 `・`/`•`/`‧` 等变体收敛到同一形态。
        # 只认 `·` 的 front.py 会把这些变体原样放进词表 → 变 unk → 模型吐怪音
        # （2026-09-28 token 级定位）。默认启用，NAME_PUNCT_NORMALIZE=0 关闭。
        # 详见 app/name_punct.py。
        if name_punct.ENABLED:
            synth_lines = name_punct.apply_name_separator_rules(synth_lines)
        # 年份读法：四位年份改逐位读（2011 年 → 二零一一年）。默认启用。
        # 这条规则原本只在 tts-server（本地 GPU 引擎）里，云引擎链路绕过它 ——
        # 见 app/year_norm.py 顶部的完整说明。
        if year_norm.ENABLED:
            synth_lines = year_norm.apply_year_rules(synth_lines)
        # 数值读法：单位/量词或幅度词旁边、以及带 % 的阿拉伯数字换汉字。
        # 阿拉伯数字在 IndexTTS 词表里不存在，进模型就是一个 unk（读法随机），
        # 而云端 TN 不可依赖 —— 详见 app/num_value_norm.py 顶部。
        # 必须夹在 year_norm 之后（年份先换掉）、number_norm 之前（两层命中集合不相交）。
        if num_value_norm.ENABLED:
            synth_lines = num_value_norm.apply_value_rules(synth_lines)
        # 数字读法归一化：只补文本前端 TN 的缺口（长号码被按数值读等），
        # 默认关闭；NUM_NORMALIZE=1 启用。详见 app/number_norm.py 与 NUMBER_NORMALIZATION.md。
        if number_norm.ENABLED:
            synth_lines = number_norm.apply_number_rules(synth_lines)

        # 合成只有一条路径：引擎适配层（engines/），在 backend 进程内合成。
        # 2026-09-29 之前这里还有一条兜底：kind 不是 mono/podcast 时
        # POST {TTS_URL}/api/podcast，把任务交给本地 tts-server 的播客引擎。
        # 那是「云引擎 / 本地服务器」两套执行路径并存的根源 —— 云端引擎部署下
        # 它必然失败，且绕过了能力声明、探活与熔断，长期没有测试覆盖。
        # 已删除：未知 kind 直接报错，好过悄悄走一条没人维护的老路。
        kind = task.get("kind")
        if kind == "mono":
            from .mono_runner import run_mono_task
            await run_mono_task(task, lines=synth_lines)
            return
        if kind == "podcast":
            from .podcast_runner import run_podcast_task
            await run_podcast_task(task, lines=synth_lines)
            return
        raise ValueError(f"未知任务类型 kind={kind!r}（应为 mono 或 podcast）")

    except FileNotFoundError as e:
        # 参考音频等文件缺失是永久性配置错误，重试无意义，不能误报"连接中断"
        logger.error("[queue] missing file task=%s error=%s", task_id, e)
        task["status"] = qs.QueueTaskStatus.FAILED
        task["message"] = "参考音频文件不存在，请检查任务音色配置"
        task["error"] = f"参考音频文件不存在: {e}"
        qs.persist_task(task_id)
    except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError) as e:
        # 引擎侧网络中断（云端 API 不可达 / 自建 tts-server 掉线）。任务可能已合成
        # 到一半，直接判 FAILED 会误导，标 INTERRUPTED 让用户决定是否重提。
        logger.error("[queue] engine connection interrupted task=%s error=%s", task_id, e)
        task["status"] = qs.QueueTaskStatus.INTERRUPTED
        task["message"] = "合成过程中与引擎的连接中断，可重新提交"
        task["error"] = f"引擎连接中断: {e}"
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
