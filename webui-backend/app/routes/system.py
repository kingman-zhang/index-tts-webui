"""配置与健康检查端点（原 server.py「配置与健康检查」分区）。"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from ..config import TTS_URL, http_client

router = APIRouter()


@router.get("/api/config")
async def get_config():
    """返回后端配置与 TTS 服务状态。"""
    tts_ok = False
    tts_info = None
    try:
        resp = await http_client.get(f"{TTS_URL}/api/health", timeout=5.0)
        if resp.status_code == 200:
            tts_ok = True
            tts_info = resp.json()
    except Exception:
        pass
    return {
        "tts_url": TTS_URL,
        "tts_online": tts_ok,
        "tts_info": tts_info,
    }


@router.get("/api/tts/health")
async def tts_health():
    """代理 TTS 健康检查。"""
    try:
        resp = await http_client.get(f"{TTS_URL}/api/health", timeout=5.0)
        return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")
    except Exception as e:
        raise HTTPException(502, f"TTS 服务异常: {e}")
