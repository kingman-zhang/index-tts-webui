"""参考音频管理端点（原 server.py「参考音频管理」+「单段合成代理」分区，行为不变）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..config import (
    DATA_DIR,
    FAVORITES_PATH,
    LOCAL_VOICES_DIR,
    PRESET_VOICES_DIR,
    TTS_URL,
    http_client,
    logger,
)
from ..models import FavoriteVoicesModel, SynthesizeRequestModel

router = APIRouter()


def _load_favorite_paths() -> list[str]:
    if not FAVORITES_PATH.exists():
        return []
    try:
        value = json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
        return list(dict.fromkeys(path for path in value if isinstance(path, str))) if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        logger.warning("无法读取收藏音色文件: %s", FAVORITES_PATH)
        return []


def _save_favorite_paths(paths: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(path for path in paths if isinstance(path, str) and path.strip()))
    FAVORITES_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


@router.get("/api/voices")
async def list_voices():
    """列出参考音频：合并 TTS 服务和本地 data/voices/ 的列表。"""
    voices = []
    # 尝试从 TTS 服务获取
    try:
        resp = await http_client.get(f"{TTS_URL}/api/voices", timeout=10.0)
        if resp.status_code == 200:
            voices.extend(resp.json().get("voices", []))
    except Exception:
        pass
    # 合并本地保存的音频（去重）
    existing_names = {v.get("name") for v in voices}
    if LOCAL_VOICES_DIR.exists():
        for ext in ("*.wav", "*.mp3", "*.flac", "*.ogg", "*.webm"):
            for f in sorted(LOCAL_VOICES_DIR.glob(ext)):
                if f.name not in existing_names:
                    voices.append({
                        "name": f.name,
                        "path": str(f),
                        "size_kb": round(f.stat().st_size / 1024, 1),
                        "source": "custom",
                        "renameable": True,
                        "deletable": True,
                    })
    return {"voices": voices, "count": len(voices)}


@router.get("/api/voice-favorites")
async def list_voice_favorites():
    """读取持久化的收藏音色路径。"""
    return {"paths": _load_favorite_paths()}


@router.put("/api/voice-favorites")
async def save_voice_favorites(payload: FavoriteVoicesModel):
    """覆盖保存收藏音色路径。"""
    return {"paths": _save_favorite_paths(payload.paths)}


@router.post("/api/voices/upload")
async def upload_voice(file: UploadFile = File(...), name: str = Form(None)):
    """上传参考音频，优先转发 TTS；TTS 不可达时才保存到本地。"""
    original_name = file.filename or "voice.wav"
    original_path = Path(original_name)
    ext = original_path.suffix.lower() or ".wav"
    allowed = (".wav", ".mp3", ".flac", ".ogg", ".webm")
    if ext not in allowed:
        raise HTTPException(400, f"仅支持 {allowed} 格式，收到: {ext or '无扩展名'}")

    custom_name = (name or "").strip()
    if custom_name:
        # 只接受单一文件名，不允许路径分隔符；扩展名统一沿用原始音频扩展名。
        if Path(custom_name).name != custom_name or re.search(r"[\\\\/:*?\"<>|\x00-\x1f]", custom_name):
            raise HTTPException(400, "音色名称包含非法文件名字符")
        custom_stem = Path(custom_name).stem
        if not custom_stem:
            raise HTTPException(400, "音色名称不能为空")
        safe_name = custom_stem + ext
    else:
        safe_name = original_path.name

    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    logger.info(
        "[voice-upload] received original=%r custom_name=%r safe_name=%r ext=%s mime=%s size=%d",
        original_name, custom_name or None, safe_name, ext, content_type, len(content),
    )

    try:
        files = {"file": (safe_name, content, content_type)}
        form_data = {"name": custom_stem} if custom_name else None
        resp = await http_client.post(
            f"{TTS_URL}/api/voices/upload", files=files, data=form_data, timeout=60.0
        )
        body_preview = resp.text[:1000]
        logger.info(
            "[voice-upload] tts-response status=%s target=%s filename=%r body=%s",
            resp.status_code, TTS_URL, safe_name, body_preview,
        )
        if resp.status_code == 200:
            result = resp.json()
            result["name"] = safe_name
            logger.info("[voice-upload] completed via tts name=%r", safe_name)
            return result
        if resp.status_code not in (404, 405, 502, 503, 504):
            detail = body_preview or f"TTS 服务返回 HTTP {resp.status_code}"
            raise HTTPException(resp.status_code, f"TTS 上传失败: {detail}")
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        logger.warning("[voice-upload] tts-timeout target=%s error=%s; fallback=local", TTS_URL, exc)
    except httpx.HTTPError as exc:
        logger.warning("[voice-upload] tts-http-error target=%s error=%s; fallback=local", TTS_URL, exc)

    LOCAL_VOICES_DIR.mkdir(parents=True, exist_ok=True)
    dest = LOCAL_VOICES_DIR / safe_name
    if dest.exists():
        raise HTTPException(409, f"音色名称已存在: {safe_name}")
    dest.write_bytes(content)
    logger.info("[voice-upload] completed locally path=%s size=%d", dest, len(content))
    return {"name": dest.name, "path": str(dest), "size_kb": round(len(content) / 1024, 1)}


@router.post("/api/voices/rename")
async def rename_voice(request: Request):
    """重命名已上传的参考音频。"""
    body = await request.json()
    old_name = body.get("old_name", "")
    new_name = body.get("new_name", "")
    if not old_name or not new_name:
        raise HTTPException(400, "缺少参数")
    # 保留原扩展名
    ext = Path(old_name).suffix
    safe_new = Path(new_name).name + ext
    # 先尝试重命名 TTS 服务器上的自定义音频
    try:
        resp = await http_client.post(
            f"{TTS_URL}/api/voices/rename",
            json={"old_name": old_name, "new_name": safe_new},
            timeout=30.0,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    # TTS 不可用时，在本地 voices 目录查找
    old_path = LOCAL_VOICES_DIR / old_name
    new_path = LOCAL_VOICES_DIR / safe_new
    if old_path.exists():
        if new_path.exists():
            raise HTTPException(409, "目标名称已存在")
        old_path.rename(new_path)
        return {"name": safe_new, "path": str(new_path)}
    # 在 preset-voices 目录查找（不允许重命名预设）
    for d in [PRESET_VOICES_DIR, PRESET_VOICES_DIR / "emotions"]:
        p = d / old_name
        if p.exists():
            raise HTTPException(400, "预设音色不支持改名，请先上传副本")
    raise HTTPException(404, f"音频文件不存在: {old_name}")


@router.delete("/api/voices/{filename}")
async def delete_voice(filename: str):
    """删除自定义参考音频，禁止删除内置预设。"""
    safe_name = Path(filename).name
    for d in [PRESET_VOICES_DIR, PRESET_VOICES_DIR / "emotions"]:
        if (d / safe_name).exists():
            raise HTTPException(400, "预设音色不支持删除")
    try:
        resp = await http_client.delete(f"{TTS_URL}/api/voices/{safe_name}", timeout=30.0)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code not in (404, 405):
            raise HTTPException(resp.status_code, resp.json().get("detail", "删除失败"))
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
        pass
    target = LOCAL_VOICES_DIR / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "自定义音频不存在")
    target.unlink()
    return {"deleted": safe_name}


@router.post("/api/synthesize")
async def synthesize(req: SynthesizeRequestModel):
    """转发单段合成请求到 TTS 服务（同步返回）。"""
    try:
        resp = await http_client.post(
            f"{TTS_URL}/api/synthesize",
            json=req.model_dump(),
            timeout=300.0,
        )
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, resp.json().get("detail", "合成失败"))
        return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")


@router.get("/api/audio/{filename}")
async def proxy_audio(filename: str):
    """获取音频文件：先尝试 TTS 服务，失败则本地查找。"""
    safe_name = Path(filename).name
    # 根据后缀确定 content-type
    ext = Path(safe_name).suffix.lower()
    mime_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".webm": "audio/webm"}
    mime = mime_map.get(ext, "audio/mpeg")
    # 先尝试从 TTS 服务获取
    try:
        resp = await http_client.get(f"{TTS_URL}/api/audio/{safe_name}", timeout=60.0)
        if resp.status_code == 200:
            return StreamingResponse(
                iter([resp.content]),
                media_type=mime,
                headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
            )
    except Exception:
        pass
    # 本地查找：voices 目录 / preset-voices 目录（含 emotions 子目录） / outputs 目录
    search_dirs = [
        LOCAL_VOICES_DIR,
        PRESET_VOICES_DIR,
        PRESET_VOICES_DIR / "emotions",
        DATA_DIR / "outputs",
    ]
    for d in search_dirs:
        local_path = d / safe_name
        if local_path.exists():
            return FileResponse(local_path, media_type=mime, filename=safe_name)
    raise HTTPException(404, "音频文件不存在")
