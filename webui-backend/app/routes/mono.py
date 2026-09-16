"""配音模式（mono）结果音频服务。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import queue_state as qs

router = APIRouter()


@router.get("/api/mono/audio/{task_id}")
async def mono_audio(task_id: str):
    """提供配音任务的合成结果音频（backend 本地落盘，不经 tts-server）。"""
    task = qs.queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    output_path = task.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(output_path, media_type="audio/wav", filename=f"mono_{task_id}.wav")
