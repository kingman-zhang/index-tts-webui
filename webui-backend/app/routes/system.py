"""配置与健康检查端点（原 server.py「配置与健康检查」分区）。"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from ..config import (
    PODCAST_DEFAULT_SILENCE,
    PODCAST_GEN_PARAMS,
    TTS_STATUS_POLL,
    TTS_URL,
    http_client,
)
from ..membership import service as member_svc

router = APIRouter()


@router.get("/api/config")
async def get_config():
    """返回后端配置与 TTS 服务状态。

    TTS_STATUS_POLL=0（默认）时不探测 tts-server，tts_online 返回 null——
    前端状态栏据此静默；=1 时探测一次，前端按 10s 轮询消费。
    """
    tts_ok: bool | None = None
    tts_info = None
    if TTS_STATUS_POLL:
        tts_ok = False
        try:
            resp = await http_client.get(f"{TTS_URL}/api/health", timeout=5.0)
            if resp.status_code == 200:
                tts_ok = True
                tts_info = resp.json()
        except Exception:
            pass
    return {
        "tts_url": TTS_URL,
        "tts_status_poll": TTS_STATUS_POLL,
        "tts_online": tts_ok,
        "tts_info": tts_info,
        # 积分定价（前端预估扣费用；均为静态配置，零探测成本）
        "member_enforce": member_svc.ENFORCE,
        "member_points_per_1000_chars": member_svc.POINTS_PER_1000_CHARS,
        # 双人播客默认静音/生成参数（.env: PODCAST_SILENCE_* / PODCAST_GEN_PARAMS）
        "podcast_defaults": {
            "silence": dict(PODCAST_DEFAULT_SILENCE),
            "params": dict(PODCAST_GEN_PARAMS),
        },
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
