"""双人播客合成（直连/遗留）端点（原 server.py「双人播客合成」分区，行为不变）。"""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import TTS_URL, http_client
from ..models import PodcastRequestModel

router = APIRouter()


@router.post("/api/podcast/generate")
async def generate_podcast(req: PodcastRequestModel):
    """提交双人播客合成任务，返回 task_id。"""
    try:
        resp = await http_client.post(
            f"{TTS_URL}/api/podcast",
            json=req.model_dump(),
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, resp.json().get("detail", "提交失败"))
        return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")


@router.get("/api/podcast/status/{task_id}")
async def podcast_status_sse(task_id: str):
    """SSE 推送合成进度。前端用 EventSource 连接。

    每 1.5 秒轮询 TTS 服务，有变化就推送事件；任务完成/失败后推送终态并关闭。
    """
    async def event_stream():
        last_payload = None
        while True:
            try:
                resp = await http_client.get(f"{TTS_URL}/api/task/{task_id}", timeout=10.0)
                if resp.status_code == 404:
                    yield f"event: error\ndata: {json.dumps({'error': '任务不存在'})}\n\n"
                    return
                if resp.status_code != 200:
                    yield f"event: error\ndata: {json.dumps({'error': 'TTS 服务异常'})}\n\n"
                    return
                payload = resp.json()
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': f'轮询失败: {e}'})}\n\n"
                return

            # 只在有变化时推送
            if payload != last_payload:
                last_payload = payload
                yield f"data: {json.dumps(payload)}\n\n"

            if payload.get("status") in ("completed", "failed"):
                yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                return

            await asyncio.sleep(1.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/podcast/task/{task_id}")
async def get_task(task_id: str):
    """查询任务状态（非 SSE，单次查询）。"""
    try:
        resp = await http_client.get(f"{TTS_URL}/api/task/{task_id}", timeout=10.0)
        if resp.status_code == 404:
            raise HTTPException(404, "任务不存在")
        return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")


@router.get("/api/podcast/audio/{task_id}")
async def podcast_audio(task_id: str):
    """代理下载指定任务的合成音频（流式）。"""
    try:
        resp = await http_client.get(f"{TTS_URL}/api/task/{task_id}/audio", timeout=120.0)
        if resp.status_code != 200:
            detail = "音频不可用"
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise HTTPException(resp.status_code, detail)
        filename = f"podcast_{task_id}.wav"
        return StreamingResponse(
            iter([resp.content]),
            media_type="audio/wav",
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Content-Length": str(len(resp.content)),
                "Accept-Ranges": "bytes",
            },
        )
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")


@router.get("/api/tasks")
async def list_tasks():
    """列出 TTS 服务上的最近任务。"""
    try:
        resp = await http_client.get(f"{TTS_URL}/api/tasks", timeout=10.0)
        return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")
