"""预设音色端点（原 server.py「预设音色」分区，行为不变）。"""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..config import PRESET_VOICES_DIR, TTS_URL, http_client, logger

router = APIRouter()


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

    # 先查询 TTS 服务器已有的音色列表，避免重复上传
    try:
        list_resp = await http_client.get(f"{TTS_URL}/api/voices", timeout=10.0)
        if list_resp.status_code == 200:
            existing = list_resp.json().get("voices", [])
            for v in existing:
                if v.get("name") == name:
                    logger.info("[preset-upload] already on tts, skip upload name=%s path=%s", name, v.get("path"))
                    return {"name": name, "path": v["path"], "size_kb": v.get("size_kb", 0), "local_only": False}
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, OSError) as exc:
        logger.warning("[preset-upload] cannot query tts voice list, will try upload name=%s error=%s", name, exc)

    # TTS 服务器上没有该文件，执行上传
    try:
        with open(local_path, "rb") as f:
            files = {"file": (name, f, "audio/mpeg")}
            resp = await http_client.post(f"{TTS_URL}/api/voices/upload", files=files, timeout=60.0)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 409:
            # 文件刚好在上传期间被其他请求创建，视为成功
            logger.info("[preset-upload] concurrent upload, already exists name=%s", name)
            try:
                existing = resp.json()
                return {"name": name, "path": existing.get("path", name), "size_kb": existing.get("size_kb", 0), "local_only": False}
            except Exception:
                return {"name": name, "path": name, "size_kb": round(local_path.stat().st_size / 1024, 1), "local_only": False}
        body_preview = resp.text[:500] if resp.text else ""
        logger.warning("[preset-upload] tts rejected name=%s status=%s body=%s", name, resp.status_code, body_preview)
        raise HTTPException(resp.status_code, f"上传到 TTS 服务失败: {body_preview or resp.status_code}")
    except HTTPException:
        raise
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, OSError) as exc:
        # TTS 不可用，返回本地路径
        logger.warning("[preset-upload] tts unavailable, returning local path name=%s error=%s", name, exc)
        return {"name": name, "path": str(local_path), "size_kb": round(local_path.stat().st_size / 1024, 1), "local_only": True}
