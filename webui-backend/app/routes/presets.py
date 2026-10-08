"""预设音色端点（原 server.py「预设音色」分区，行为不变）。"""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..config import PRESET_VOICES_DIR, TTS_URL, http_client, logger

router = APIRouter()


def _json_object_or_none(resp: httpx.Response) -> dict | None:
    """解析上游正文为 JSON 对象；**不是 JSON 就返回 None，绝不抛 500**。

    为什么要单独收口这一处：上游在「端口代理在应答、实例没起来」时返回的是一张
    HTML 报错页（实测 AutoDL 关机走 `HTTP 404`，但状态码换成 200 **只差代理一行配置**）。
    这种正文上 `resp.json()` 抛的是 `json.JSONDecodeError`，它属于 `ValueError`，
    **不在 `httpx.*` 异常族里**（见下面两处 `except` 的捕获列表）⇒ 会直接冒成 500，
    把「上游返回的是网页而不是数据」这个真实原因吞成「服务器内部错误」。

    收口原则与「非 200」一致：拿不到可信的正文，就等同「服务器不可用」。
    """
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _voice_list_from(resp: httpx.Response) -> list[dict] | None:
    """从音色表响应里解析出 `voices`；正文不是 JSON 对象、或 `voices` 不是列表 ⇒ None。

    形状校验放在这里而不是调用处：**上游的正文一律不可信**，少一层校验就多一个
    500 的入口。非字典项直接滤掉（调用处会再按 name/path 匹配）。
    """
    data = _json_object_or_none(resp)
    if data is None:
        return None
    voices = data.get("voices", [])
    if not isinstance(voices, list):
        return None
    return [v for v in voices if isinstance(v, dict)]


def _local_path_fallback(name: str, local_path, why: str) -> dict:
    """TTS 服务器不可用时的降级：返回 backend 本地路径，真上传交给合成时的 voice_sync。

    为什么必须降级而不是报错 —— 本端点只是「预热」（提前把预设音色推到 tts-server），
    真正的上传由 `engines/voice_sync.ensure_voice_on_server` 在合成时按**实际分到的那台**
    补做，而它自己就有「取不到音色表就降级返回原路径」的兜底。所以预热失败在语义上
    等于「TTS 不可用」，不该让用户连音色都选不上：前端 `selectPreset` 写的是
    `onChange({voice_path: r.path, ...})`，一旦这里抛错，`onChange` 根本不执行 ——
    表现为「加载预设音色失败」并且**该音色完全无法选中**。

    典型触发场景：AutoDL 实例关机后端口代理返回 `HTTP 404` + 一张 HTML 报错页
    （TCP 通、代理在应答，所以不是 ConnectError）。旧实现只对「异常」降级、对
    「非 200 响应」`raise`，于是把那页 HTML 原文当错误详情弹给了用户。
    """
    logger.warning("[preset-upload] tts 不可用（%s），降级返回本地路径 name=%s path=%s",
                   why, name, local_path)
    return {
        "name": name,
        "path": str(local_path),
        "size_kb": round(local_path.stat().st_size / 1024, 1),
        "local_only": True,
    }


@router.get("/api/preset-voices")
async def list_preset_voices():
    """列出预设音色（分类：女声/男声/情感参考）。"""
    manifest_path = PRESET_VOICES_DIR / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    # fallback：扫描目录
    categories = {"female": [], "male": [], "emotion": []}
    if PRESET_VOICES_DIR.exists():
        for f in sorted(PRESET_VOICES_DIR.iterdir()):
            if f.suffix.lower() not in (".mp3", ".wav"):
                continue
            item = {"name": f.name, "path": str(f), "size_kb": round(f.stat().st_size / 1024, 1)}
            if f.name.startswith("女-"):
                categories["female"].append(item)
            elif f.name.startswith("男-"):
                categories["male"].append(item)
    emo_dir = PRESET_VOICES_DIR / "emotions"
    if emo_dir.exists():
        for f in sorted(emo_dir.iterdir()):
            if f.suffix.lower() not in (".mp3", ".wav"):
                continue
            categories["emotion"].append({"name": f.name, "path": str(f), "size_kb": round(f.stat().st_size / 1024, 1)})
    return {"categories": categories, "count": sum(len(v) for v in categories.values())}


@router.get("/api/preset-voices/{category}")
async def list_preset_voices_by_category(category: str):
    """按分类列出预设音色（female/male/emotion）。"""
    data = await list_preset_voices()
    cats = data.get("categories", data)
    if category in cats:
        return {"voices": cats[category], "count": len(cats[category])}
    raise HTTPException(404, f"分类不存在: {category}")


@router.post("/api/preset-voices/upload-to-tts")
async def upload_preset_to_tts(request: Request):
    """把本地预设音色上传到 TTS 服务器（选择预设音色时自动调用）。

    如果 TTS 服务器上已存在同名文件，直接返回路径，不重复上传。
    """
    body = await request.json()
    name = body.get("name")
    if not name:
        raise HTTPException(400, "缺少 name 字段")
    local_path = PRESET_VOICES_DIR / name
    if not local_path.exists():
        # 也检查 emotions 子目录
        local_path = PRESET_VOICES_DIR / "emotions" / name
    if not local_path.exists():
        raise HTTPException(404, f"预设音色不存在: {name}")

    # 先查询 TTS 服务器已有的音色列表，避免重复上传。
    try:
        list_resp = await http_client.get(f"{TTS_URL}/api/voices", timeout=10.0)
        if list_resp.status_code == 200:
            existing = _voice_list_from(list_resp)
            if existing is None:
                # 200 但正文不是音色表（HTML 报错页 / 形状不对）：与「非 200」同待遇 —— 直接短路，
                # 不再白等一次 60s 超时上传。上游正文只进日志（截 120 字）。
                return _local_path_fallback(
                    name, local_path,
                    f"音色表正文不是合法音色表：{(list_resp.text or '')[:120]!r}")
            for v in existing:
                if v.get("name") == name and v.get("path"):
                    logger.info("[preset-upload] already on tts, skip upload name=%s path=%s", name, v["path"])
                    return {"name": name, "path": v["path"], "size_kb": v.get("size_kb", 0), "local_only": False}
        else:
            # 非 200 一律视同「服务器不可用」并**直接短路**：音色表都拿不到，上传必然也拿不到，
            # 没必要再白等一次 60s 超时（实例关机时这就是常态）。
            return _local_path_fallback(
                name, local_path, f"音色表 HTTP {list_resp.status_code} {(list_resp.text or '')[:120]!r}"
            )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, OSError) as exc:
        logger.warning("[preset-upload] cannot query tts voice list, will try upload name=%s error=%s", name, exc)

    # TTS 服务器上没有该文件，执行上传
    try:
        with open(local_path, "rb") as f:
            files = {"file": (name, f, "audio/mpeg")}
            resp = await http_client.post(f"{TTS_URL}/api/voices/upload", files=files, timeout=60.0)
        if resp.status_code == 200:
            data = _json_object_or_none(resp)
            if data is None:
                # 上传可能其实成功了、只是正文不可解析。降级返回本地路径是**安全**的：
                # 合成时 voice_sync.ensure_voice_on_server 会按实际分到的那台再核一次
                # 音色表，已存在就复用、缺了才补传 —— 不会因这里判断保守而失败。
                return _local_path_fallback(
                    name, local_path,
                    f"上传响应不是合法 JSON 对象：{(resp.text or '')[:200]!r}")
            return data
        if resp.status_code == 409:
            # 文件刚好在上传期间被其他请求创建，视为成功
            logger.info("[preset-upload] concurrent upload, already exists name=%s", name)
            try:
                existing = resp.json()
                return {"name": name, "path": existing.get("path", name), "size_kb": existing.get("size_kb", 0), "local_only": False}
            except Exception:
                return {"name": name, "path": name, "size_kb": round(local_path.stat().st_size / 1024, 1), "local_only": False}
        # 非 200/409 同样视同「服务器不可用」→ 降级（详见 _local_path_fallback）。
        # 上游的报错正文只进日志，不再当业务详情回给前端。
        body_preview = (resp.text or "")[:200]
        return _local_path_fallback(name, local_path, f"上传 HTTP {resp.status_code} {body_preview!r}")
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, OSError) as exc:
        # TTS 不可用，返回本地路径
        return _local_path_fallback(name, local_path, f"上传失败：{type(exc).__name__}")
