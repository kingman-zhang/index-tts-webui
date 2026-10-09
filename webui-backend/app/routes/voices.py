"""参考音频管理端点（原 server.py「参考音频管理」+「单段合成代理」分区，行为不变）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

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
from ..membership import get_optional_user
from ..membership import service as member_svc
from ..engines import voice_fanout
from ..engines.voice_sync import USER as VOICE_USER
from ..engines.voice_sync import target_server_name
from ..voice_store import (
    UserVoiceStore,
    VoiceNameTaken,
    VoiceStoreError,
    is_voice_id,
    list_owner_ids,
)

router = APIRouter()

VOICES_META_PATH = DATA_DIR / "voices_meta.json"  # 本地上传音色的归属记录 {文件名: {owner_id, uploaded_at}}

# 参考音频上传大小上限：45 分钟 48kHz 单声道 wav 也才 ~250MB，而音色参考音频
# 通常 <10MB。给一个宽松上限只为挡住「误传整张专辑」把后端读进内存打爆。
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _isolation_on() -> bool:
    return member_svc.ENFORCE or member_svc.REQUIRE_LOGIN


def _load_voices_meta() -> dict:
    if not VOICES_META_PATH.exists():
        return {}
    try:
        data = json.loads(VOICES_META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_voices_meta(meta: dict) -> None:
    VOICES_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _voice_owner_ok(filename: str, user: Optional[dict]) -> bool:
    """本地自定义音色归属校验；无归属记录的旧文件仅在隔离未开启时可见。"""
    if not _isolation_on():
        return True
    if not user:
        return False
    meta = _load_voices_meta().get(filename)
    return bool(meta) and meta.get("owner_id") == user["user_id"]


def _favorites_path_for(user: Optional[dict]) -> str:
    """收藏音色按用户分文件；未登录/隔离未开启用共享文件（旧行为）。"""
    if user and _isolation_on():
        return str(DATA_DIR / f"favorites_{user['user_id']}.json")
    return str(FAVORITES_PATH)


def _load_favorite_paths(path: Optional[str] = None) -> list[str]:
    path = path or str(FAVORITES_PATH)
    if not Path(path).exists():
        return []
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return list(dict.fromkeys(p for p in value if isinstance(p, str))) if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        logger.warning("无法读取收藏音色文件: %s", path)
        return []


def _save_favorite_paths(paths: list[str], path: Optional[str] = None) -> list[str]:
    normalized = list(dict.fromkeys(p for p in paths if isinstance(p, str) and p.strip()))
    Path(path or str(FAVORITES_PATH)).write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def _user_voice_records(owner_id: str) -> list[dict]:
    """新结构用户音色（`data/voices/<user_id>/index.json`）→ 前端 VoiceFile 形态。"""
    try:
        records = UserVoiceStore(owner_id).list()
    except VoiceStoreError as exc:
        logger.warning("[voices] 读取用户音色库失败 owner=%s: %s", owner_id, exc)
        return []
    return [
        {
            "name": r["name"],
            "path": r["path"],
            "size_kb": r["size_kb"],
            "source": "custom",
            "renameable": True,
            "deletable": True,
            "scope": "user",          # 「我的音色」只认这个标记
            "voice_id": r["voice_id"],
            # 试听走 `/api/audio/{文件名}`；显示名与文件名解耦后必须显式给
            "preview_name": r["preview_name"],
        }
        for r in records
    ]


@router.get("/api/voices")
async def list_voices(user: Optional[dict] = Depends(get_optional_user)):
    """列出参考音频：三段拼接，并用 `scope` 标明**它属于谁**。

    2026-10-02 修订（用户需求第 4 条）：**「我的音色」必须只来自 backend**。
    以前这里把 tts-server 的列表原样并进来，于是「我的音色」里会混着共享池里的
    预置音色、甚至别的用户早期上传的音色 —— 用户看到一个不属于他的列表。

    所以现在每项都带 `scope`：

    - `"library"` —— tts-server 上的（预置音色、BreezeBlue、历史转发上传的音色）。
      仍是共享池，任何用户都能选用，但**不再出现在「我的音色」里**。
    - `"user"` —— backend 本地属于当前用户的音色（新结构 `data/voices/<uid>/`，
      或老结构 `data/voices/*.wav` + `voices_meta.json` 归属命中）。
    """
    voices = []
    # ① tts-server 的共享池
    try:
        resp = await http_client.get(f"{TTS_URL}/api/voices", timeout=10.0)
        if resp.status_code == 200:
            for item in resp.json().get("voices", []) or []:
                if isinstance(item, dict):
                    voices.append({**item, "scope": "library"})
    except Exception:
        pass

    # ② 新结构用户音色（index.json）
    if _isolation_on():
        owners = [user["user_id"]] if user else []
    else:
        # 隔离未开启（本地开发）：所有用户的音色都可见，与老结构行为一致
        owners = list_owner_ids()
    for owner in owners:
        voices.extend(_user_voice_records(owner))

    # ③ 老结构平铺音色（data/voices/*.wav + voices_meta.json 归属过滤）
    #    刻意**保留**：结构不迁移（用户 2026-10-02 决定），存量音色继续可读可删。
    existing_names = {v.get("name") for v in voices}
    if LOCAL_VOICES_DIR.exists():
        for ext in ("*.wav", "*.mp3", "*.flac", "*.ogg", "*.webm"):
            for f in sorted(LOCAL_VOICES_DIR.glob(ext)):
                if f.name in existing_names:
                    continue
                if not _voice_owner_ok(f.name, user):
                    continue
                voices.append({
                    "name": f.name,
                    "path": str(f),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "source": "custom",
                    "renameable": True,
                    "deletable": True,
                    "scope": "user",
                    "preview_name": f.name,
                })
    return {"voices": voices, "count": len(voices)}


@router.get("/api/voice-favorites")
async def list_voice_favorites(user: Optional[dict] = Depends(get_optional_user)):
    """读取当前用户持久化的收藏音色路径。"""
    return {"paths": _load_favorite_paths(_favorites_path_for(user))}


@router.put("/api/voice-favorites")
async def save_voice_favorites(payload: FavoriteVoicesModel, user: Optional[dict] = Depends(get_optional_user)):
    """覆盖保存当前用户的收藏音色路径。"""
    return {"paths": _save_favorite_paths(payload.paths, _favorites_path_for(user))}


@router.post("/api/voices/upload")
async def upload_voice(
    file: UploadFile = File(...),
    name: str = Form(None),
    user: Optional[dict] = Depends(get_optional_user),
):
    """上传参考音频到**当前用户的音色库**，并广播到池内所有 tts-server。

    2026-10-02 起的行为（用户需求第 1/2 条）：

    1. **落 backend**：`data/voices/<user_id>/<voice_id><ext>` + 该用户的 `index.json`
       （显示名只在索引里 ⇒ 以后改名不用动文件）；
    2. **广播**：以 `{user_id}_{voice_id}{ext}` 为名推给池内**所有** local tts-server，
       失败只记日志、不影响上传结果（漏掉的那台会在首次合成时由 voice_sync 补传）；
    3. **不再"只转发给 `TTS_URL` 那一台"** —— 那是本项目此前最大的音色单点。

    ⚠️ **必须登录**（用户 2026-10-02 确认）：音色按用户归档，没有"匿名音色"这一档。
    """
    if not user:
        raise HTTPException(401, "请先登录后再上传音色")

    original_name = file.filename or "voice.wav"
    original_path = Path(original_name)
    ext = original_path.suffix.lower() or ".wav"
    allowed = (".wav", ".mp3", ".flac", ".ogg", ".webm")
    if ext not in allowed:
        raise HTTPException(400, f"仅支持 {allowed} 格式，收到: {ext or '无扩展名'}")

    custom_name = (name or "").strip()
    display_name = custom_name or original_path.stem or "未命名音色"

    content = await file.read()
    if not content:
        raise HTTPException(400, "上传内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"音频过大（{len(content) / 1024 / 1024:.1f}MB），上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB"
        )

    store = UserVoiceStore(user["user_id"])
    try:
        record = store.add(content, ext, display_name)
    except VoiceNameTaken as exc:
        raise HTTPException(409, str(exc))
    except VoiceStoreError as exc:
        raise HTTPException(400, str(exc))

    logger.info(
        "[voice-upload] 已入库 owner=%s id=%s name=%r file=%s size=%d",
        user["user_id"], record["voice_id"], record["name"], record["file"], len(content),
    )

    # 广播（尽力而为）：失败不改变上传结果，只回一份明细供排查
    results = await voice_fanout.broadcast_upload(
        http_client, Path(record["path"]), record["server_name"]
    )
    failed = {url: why for url, why in results.items() if why != "ok" and why != "ok(已存在)"}
    if results and not failed:
        logger.info("[voice-upload] 广播完成，%d 台 tts-server 均已就绪", len(results))
    elif failed:
        logger.warning(
            "[voice-upload] 广播有 %d 台未成功（不影响上传；合成时会按需补传）：%s",
            len(failed), failed,
        )

    return {
        "voice_id": record["voice_id"],
        "name": record["name"],
        "path": record["path"],
        "size_kb": record["size_kb"],
        "server_name": record["server_name"],
        "broadcast_total": len(results),
        "broadcast_failed": failed,
    }


def _old_style_server_name(file_name: str, owner_id: Optional[str]) -> str:
    """老结构音色在 tts-server 上的名字（`{名}__{owner}{ext}`）。

    走 `voice_sync.target_server_name` 同一个函数，避免两处规则各写一遍后漂移。
    """
    return target_server_name(Path(file_name).name, VOICE_USER, owner_id)


@router.post("/api/voices/rename")
async def rename_voice(request: Request, user: Optional[dict] = Depends(get_optional_user)):
    """重命名「我的音色」。

    2026-10-02 修订（用户需求第 7 条）：**不再让 tts-server 改名**。

    - **新结构**音色：服务器上的文件名是 `{user_id}_{voice_id}{ext}`，与显示名无关
      ⇒ 改名只改 `index.json` 里的 `name`，一个文件都不用动。
    - **老结构**音色：改名只动 backend 本地文件名，并**同步 `voices_meta.json` 的键**
      （不同步的话归属记录会跟丢，隔离开启时该音色会从列表里消失 —— 这是修掉的一个
      既有 bug）。服务器上的旧文件**刻意保留**：老任务里存的是服务器绝对路径，
      改/删它会让那些任务失效；新名字会在下次合成时按需上传。
    """
    body = await request.json()
    key = str(body.get("voice_id") or body.get("old_name") or "").strip()
    new_name = str(body.get("new_name") or "").strip()
    if not key or not new_name:
        raise HTTPException(400, "缺少参数")
    if not user:
        raise HTTPException(401, "请先登录后重命名音色")

    if is_voice_id(key):
        try:
            record = UserVoiceStore(user["user_id"]).rename(key, new_name)
        except VoiceNameTaken as exc:
            raise HTTPException(409, str(exc))
        except VoiceStoreError as exc:
            raise HTTPException(404, str(exc))
        return {
            "voice_id": record["voice_id"],
            "name": record["name"],
            "path": record["path"],
        }

    # ── 老结构（平铺文件）：只改 backend 本地 ──
    safe_old = Path(key).name
    if not safe_old or not _voice_owner_ok(safe_old, user):
        raise HTTPException(404, "音频文件不存在")
    ext = Path(safe_old).suffix
    stem = Path(new_name).name
    if ext and stem.lower().endswith(ext.lower()):
        stem = stem[: -len(ext)]
    if not stem:
        raise HTTPException(400, "音色名称不能为空")
    safe_new = stem + ext
    if safe_new == safe_old:
        return {"name": safe_new, "path": str(LOCAL_VOICES_DIR / safe_new)}
    old_path = LOCAL_VOICES_DIR / safe_old
    new_path = LOCAL_VOICES_DIR / safe_new
    if old_path.exists():
        if new_path.exists():
            raise HTTPException(409, "目标名称已存在")
        old_path.rename(new_path)
        meta = _load_voices_meta()
        if safe_old in meta:
            meta[safe_new] = meta.pop(safe_old)
            _save_voices_meta(meta)
        logger.info("[voices] 老结构音色改名 %s → %s（服务器上的旧文件保留，不影响老任务）",
                    safe_old, safe_new)
        return {"name": safe_new, "path": str(new_path)}
    # 在 preset-voices 目录查找（不允许重命名预设）
    for d in [PRESET_VOICES_DIR, PRESET_VOICES_DIR / "emotions"]:
        p = d / safe_old
        if p.exists():
            raise HTTPException(400, "预设音色不支持改名，请先上传副本")
    raise HTTPException(404, f"音频文件不存在: {safe_old}")


@router.delete("/api/voices/{key}")
async def delete_voice(key: str, user: Optional[dict] = Depends(get_optional_user)):
    """删除「我的音色」（用户需求第 5 条）。

    key 可以是音色 id（`voc_xxxxxxxxxxxx`，新结构）或文件名（老结构）：

    1. **广播删除** tts-server 上的对应文件（池内所有 local 都试一遍，失败只记日志）；
    2. 删 backend 本地文件；
    3. 新结构在 `index.json` 里标 `deleted_at`（留痕，列表默认隐藏）；
       老结构从 `voices_meta.json` 移除归属记录。

    预设音色（`data/preset-voices/`）一律不可删。
    """
    safe = Path(key).name
    for d in [PRESET_VOICES_DIR, PRESET_VOICES_DIR / "emotions"]:
        if (d / safe).exists():
            raise HTTPException(400, "预设音色不支持删除")
    if not user:
        raise HTTPException(401, "请先登录后删除音色")

    if is_voice_id(safe):
        try:
            record = UserVoiceStore(user["user_id"]).delete(safe)
        except VoiceStoreError as exc:
            raise HTTPException(404, str(exc))
        broadcast = await voice_fanout.broadcast_delete(http_client, record["server_name"])
        return {
            "deleted": safe,
            "name": record["name"],
            "server_name": record["server_name"],
            "broadcast": broadcast,
        }

    # ── 老结构 ──
    if not _voice_owner_ok(safe, user):
        raise HTTPException(404, "自定义音频不存在")
    target = LOCAL_VOICES_DIR / safe
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "自定义音频不存在")
    meta = _load_voices_meta()
    # 服务器上的名字用的是**上传者**的 id（隔离未开启时可能不是当前用户）
    owner = (meta.get(safe) or {}).get("owner_id") or user["user_id"]
    server_name = _old_style_server_name(safe, owner)
    broadcast = await voice_fanout.broadcast_delete(http_client, server_name)
    target.unlink()
    meta.pop(safe, None)
    _save_voices_meta(meta)
    logger.info("[voices] 老结构音色已删除 name=%s 广播=%s", safe, broadcast)
    return {"deleted": safe, "server_name": server_name, "broadcast": broadcast}


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
async def get_local_audio(filename: str):
    """按文件名返回 **backend 本地**的音频（音色试听）。

    2026-10-09（用户决定）：**移除 tts-server 兜底**，不再代理壳上的文件。
    backend 是音色的权威源 —— 上传先落 backend 磁盘、再广播到壳，前端试听用的
    `preview_name` / `filename` 本来就是 **backend 本地的文件名**（预设音色、BreezeBlue、
    用户音色三条列表都从 backend 产出），也就是合成时真正取用的那一份。

    旧实现「先打壳、失败才查本地」有三笔代价，换不来任何收益：
    - 壳不可达（网络黑洞而非拒绝连接）时要**白等 60s 超时**才回退；
    - 壳上存在同名旧文件时会**播错**（听到的不是将要用于合成的那份）；
    - 壳在线时白多一次往返。

    壳上独有、backend 没有的音色 ⇒ 直接 **404**，这是正确语义：音色列表本身
    就来自 backend，不该出现的条目本来也点不到。
    """
    safe_name = Path(filename).name
    # 根据后缀确定 content-type
    ext = Path(safe_name).suffix.lower()
    mime_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".webm": "audio/webm"}
    mime = mime_map.get(ext, "audio/mpeg")
    # 本地查找：voices 目录（含新结构的 `<user_id>/` 子目录）/ breezeblue 音色库 /
    # preset-voices 目录（含 emotions 子目录） / outputs 目录
    search_dirs = [
        LOCAL_VOICES_DIR,
        DATA_DIR / "breezeblue" / "audio",
        PRESET_VOICES_DIR,
        PRESET_VOICES_DIR / "emotions",
        DATA_DIR / "outputs",
    ]
    for d in search_dirs:
        local_path = d / safe_name
        if local_path.exists():
            return FileResponse(local_path, media_type=mime, filename=safe_name)
        # 新结构用户音色落在 `data/voices/<user_id>/` 下（试听请求带的是文件名本身）
        for sub in sorted(d.glob(f"*/{safe_name}")):
            if sub.is_file():
                return FileResponse(sub, media_type=mime, filename=safe_name)
    raise HTTPException(404, "音频文件不存在")
