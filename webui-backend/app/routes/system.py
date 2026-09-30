"""配置与健康检查端点（原 server.py「配置与健康检查」分区）。"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from ..config import (
    DATA_DIR,
    PODCAST_DEFAULT_SILENCE,
    PODCAST_GEN_PARAMS,
    TTS_STATUS_POLL,
    TTS_URL,
    http_client,
)
from .. import build_info
from ..engines.factory import refresh_pool_health
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
        # 单次合成最低收费：前端只有拿到它才能算出与后端一致的地板价
        "member_min_charge": member_svc.MIN_CHARGE,
        # 双人播客默认静音/生成参数（.env: PODCAST_SILENCE_* / PODCAST_GEN_PARAMS）
        "podcast_defaults": {
            "silence": dict(PODCAST_DEFAULT_SILENCE),
            "params": dict(PODCAST_GEN_PARAMS),
        },
    }


@router.get("/api/health")
async def backend_health():
    """后端自身存活探针（容器 healthcheck 用）。

    与 /api/tts/health 的区别：不探测本地 tts-server——云端引擎部署
    （TTS_ENGINE_PREFERRED=indextts_302ai 等）没有本地 GPU 服务，
    探 TTS_URL 会恒 503 导致容器永远 unhealthy。
    """
    return {
        "status": "ok",
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
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


@router.get("/api/version")
async def version():
    """运行实例自检：这个进程在跑哪份代码、文本链路的开关是什么。

    为什么需要：`git log` 看的是磁盘上的仓库，而这个进程是**启动时**把源码读进
    内存的 —— `git pull` 之后不重启，改动一行都不生效，且界面上完全看不出来。
    详见 app/build_info.py。

    判据：
      - `stale_sources` 非空        ⇒ 这里点名的文件在进程启动后才被改过，进程跑的是旧代码
      - `git_head` 与 `git rev-parse --short HEAD` 不一致 ⇒ 同上
      - `process_started_at`        ⇒ 进程启动时刻（与源文件 mtime 直接可比）

    部署后核对（在服务器上）：

        curl -s localhost:3001/api/version
        git rev-parse --short HEAD        # 两个 sha 必须一致

    纯自检端点，不参与业务，容器 healthcheck 不依赖它。

    会先触发一次**池探活**再读快照：`normalizes_loudness` 这类能力是资源在
    `/api/health` 里**自述**的，适配器初始为保守值、探过活才翻成真值；不探就报的
    是构造时的保守值，部署自检据此会得到与事实相反的结论（本地 2.0 壳本会把响度
    归一到 -16、自述 True，却报 False）。探活幂等、带 15s TTL，且内置引擎的探活
    都不产生合成费用。详见 engines/factory.py:refresh_pool_health。
    """
    try:
        await refresh_pool_health()
    except Exception:  # noqa: BLE001 - 自检端点绝不能因探活失败而 500
        pass
    return build_info.snapshot()
