"""双人播客 WebUI 后端。

作为前端的 API 网关，负责：
1. 转发合成请求到 TTS 服务端
2. SSE 推送合成进度给前端
3. 代理音频文件下载（隐藏 TTS 服务器地址）
4. 保存/加载播客项目（JSON 文件存储）
5. 代理参考音频管理

部署在 WebUI 服务器上（无需 GPU）。

启动方式：
  python server.py --tts-url http://gpu-server:8000 --host 0.0.0.0 --port 3001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from pydantic import BaseModel, Field

# ─── 配置 ───────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Podcast WebUI Backend")
parser.add_argument("--tts-url", default=os.environ.get("TTS_URL", "http://localhost:8000"),
                    help="TTS 服务端地址")
parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="监听地址")
parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3001")), help="监听端口")
parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "./data"),
                    help="项目数据存储目录")
args, _ = parser.parse_known_args()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [webui-backend] %(message)s",
)
logger = logging.getLogger("webui-backend")

TTS_URL = args.tts_url.rstrip("/")
DATA_DIR = Path(args.data_dir)
PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# ─── HTTP 客户端 ────────────────────────────────────────────

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(300.0, connect=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=5, keepalive_expiry=15.0),
)

# ─── 项目存储 ───────────────────────────────────────────────

class ProjectModel(BaseModel):
    id: Optional[str] = None
    name: str = "未命名播客"
    voices: dict = Field(default_factory=lambda: {
        "A": {"name": "主持人A", "voice_path": None, "voice_name": None},
        "B": {"name": "主持人B", "voice_path": None, "voice_name": None},
    })
    lines: list = Field(default_factory=list)
    silence: dict = Field(default_factory=lambda: {
        "within_segment": 200, "between_lines": 300, "speaker_switch": 500,
    })
    params: dict = Field(default_factory=lambda: {
        "speed": 1.0, "speaker_speeds": {}, "max_text_tokens_per_segment": 120,
        "do_sample": True, "top_p": 0.75, "top_k": 20, "temperature": 0.6,
        "length_penalty": 0.0, "num_beams": 2, "repetition_penalty": 5.0,
        "max_mel_tokens": 1500, "infer_concurrency": 1,
    })
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _project_path(project_id: str) -> Path:
    return PROJECTS_DIR / f"{project_id}.json"


def _save_project(project: ProjectModel) -> str:
    if project.id is None:
        project.id = f"proj_{uuid.uuid4().hex[:10]}"
        project.created_at = datetime.now().isoformat()
    project.updated_at = datetime.now().isoformat()
    _project_path(project.id).write_text(
        project.model_dump_json(indent=2), encoding="utf-8"
    )
    return project.id


def _load_project(project_id: str) -> Optional[ProjectModel]:
    path = _project_path(project_id)
    if not path.exists():
        return None
    return ProjectModel(**json.loads(path.read_text(encoding="utf-8")))


# ─── 请求模型（透传给 TTS 服务） ────────────────────────────

class EmotionModel(BaseModel):
    mode: int = 0
    audio_path: Optional[str] = None
    vector: list = Field(default_factory=lambda: [0.0] * 8)
    weight: float = 0.65
    text: Optional[str] = None
    random: bool = False


class PodcastLineModel(BaseModel):
    speaker: str
    text: str
    emotion: EmotionModel = Field(default_factory=EmotionModel)
    silence_after_ms: Optional[int] = None


class SilenceModel(BaseModel):
    within_segment: int = 200
    between_lines: int = 300
    speaker_switch: int = 500


class GenerationParamsModel(BaseModel):
    speed: float = 1.0
    speaker_speeds: dict[str, float] = Field(default_factory=dict)
    max_text_tokens_per_segment: int = 120
    do_sample: bool = True
    top_p: float = 0.75
    top_k: int = 20
    temperature: float = 0.6
    length_penalty: float = 0.0
    num_beams: int = 2
    repetition_penalty: float = 5.0
    max_mel_tokens: int = 1500
    infer_concurrency: int = 1  # GPU 模型串行推理，固定为 1


class PodcastRequestModel(BaseModel):
    lines: list[PodcastLineModel]
    voices: dict
    silence: SilenceModel = Field(default_factory=SilenceModel)
    params: GenerationParamsModel = Field(default_factory=GenerationParamsModel)


class SynthesizeRequestModel(BaseModel):
    voice: str
    text: str
    emotion: EmotionModel = Field(default_factory=EmotionModel)
    interval_silence: int = 200
    max_text_tokens_per_segment: int = 120
    params: GenerationParamsModel = Field(default_factory=GenerationParamsModel)


# ─── FastAPI 应用 ───────────────────────────────────────────

app = FastAPI(title="Podcast WebUI Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown():
    await http_client.aclose()


@app.on_event("startup")
async def on_startup():
    """启动时恢复队列：先从磁盘加载持久化任务，再从 TTS 同步状态。"""
    global current_task_id

    # 1. 从磁盘加载持久化的队列任务
    _load_persisted_tasks()

    # 2. 对持久化中"运行中"且有 tts_task_id 的任务，尝试恢复轮询
    persisted_running = [
        (tid, t) for tid, t in queue_tasks.items()
        if t.get("status") == QueueTaskStatus.RUNNING and t.get("tts_task_id")
    ]
    known_tts_ids = {t.get("tts_task_id") for _, t in persisted_running}

    if persisted_running:
        # 取第一个运行中任务恢复轮询
        first_id = persisted_running[0][0]
        logger.info("[startup] resuming polling for persisted task %s", first_id)
        asyncio.create_task(_resume_polling(first_id))

    # 3. 从 TTS 服务同步：只恢复磁盘上没有的任务
    try:
        resp = await http_client.get(f"{TTS_URL}/api/tasks?limit=50", timeout=10.0)
        if resp.status_code == 200:
            tts_tasks = resp.json().get("tasks", [])
            extra_recovered = 0
            for tt in tts_tasks:
                tts_task_id = tt.get("task_id")
                if not tts_task_id or tts_task_id in known_tts_ids:
                    continue
                status = tt.get("status")
                if status not in ("pending", "running", "completed", "failed"):
                    continue

                if status in ("pending", "running"):
                    q_status = QueueTaskStatus.RUNNING
                    q_message = tt.get("message") or "TTS 服务端运行中（WebUI 重启后恢复）"
                elif status == "completed":
                    q_status = QueueTaskStatus.SUCCESS
                    q_message = tt.get("message") or "合成完成"
                else:
                    q_status = QueueTaskStatus.FAILED
                    q_message = tt.get("error") or "合成失败"

                raw_progress = tt.get("progress", 0) or 0
                q_id = f"recovered_{tts_task_id}"
                queue_tasks[q_id] = {
                    "id": q_id,
                    "project_name": f"恢复任务 ({tts_task_id})",
                    "lines": [],
                    "voices": {},
                    "silence": {},
                    "params": {},
                    "glossary_enabled": False,
                    "status": q_status,
                    "progress": max(0.0, min(1.0, float(raw_progress) / 100.0)),
                    "current_line": tt.get("current_line", 0),
                    "total_lines": tt.get("total_lines", 0),
                    "message": q_message,
                    "created_at": tt.get("created_at", datetime.now().isoformat()),
                    "tts_task_id": tts_task_id,
                    "audio_url": f"/api/podcast/audio/{tts_task_id}" if q_status == QueueTaskStatus.SUCCESS else None,
                    "output_path": tt.get("output_path"),
                    "duration_sec": tt.get("duration_sec"),
                    "error": tt.get("error") if q_status == QueueTaskStatus.FAILED else None,
                    "cancel_requested": False,
                }
                _persist_task(q_id)
                extra_recovered += 1

            # 如果 TTS 端有运行中任务且磁盘没有对应记录，也恢复轮询
            if not persisted_running:
                tts_running = [
                    tid for tid, t in queue_tasks.items()
                    if t.get("status") == QueueTaskStatus.RUNNING and tid.startswith("recovered_")
                ]
                if tts_running:
                    logger.info("[startup] resuming polling for recovered tts task %s", tts_running[0])
                    asyncio.create_task(_resume_polling(tts_running[0]))

            logger.info("[startup] recovered %d extra tasks from TTS", extra_recovered)
        else:
            logger.warning("[startup] tts tasks query failed status=%s", resp.status_code)
    except Exception as e:
        logger.warning("[startup] failed to sync tts tasks: %s", e)

    # 4. 如果有排队中的任务，触发队列处理
    if queue_order:
        logger.info("[startup] %d queued tasks pending, resuming queue", len(queue_order))
        asyncio.create_task(_process_queue())

    logger.info("[startup] queue ready: %d tasks total", len(queue_tasks))


async def _resume_polling(webui_task_id: str):
    """恢复对 TTS 运行中任务的轮询。"""
    global current_task_id
    task = queue_tasks.get(webui_task_id)
    if not task or not task.get("tts_task_id"):
        return
    tts_task_id = task["tts_task_id"]
    current_task_id = webui_task_id
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
                    task["status"] = QueueTaskStatus.SUCCESS
                    task["progress"] = 1.0
                    task["audio_url"] = f"/api/podcast/audio/{tts_task_id}"
                    task["duration_sec"] = payload.get("duration_sec")
                    task["output_path"] = payload.get("output_path")
                    task["message"] = payload.get("message") or "合成完成"
                    _persist_task(webui_task_id)
                    logger.info("[resume] completed webui=%s tts=%s", webui_task_id, tts_task_id)
                    break
                elif status == "failed":
                    task["status"] = QueueTaskStatus.FAILED
                    task["error"] = payload.get("error", "未知错误")
                    _persist_task(webui_task_id)
                    break
            except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError) as e:
                poll_errors += 1
                logger.warning("[resume] poll error webui=%s retry=%d error=%s", webui_task_id, poll_errors, e)
                task["message"] = f"TTS 连接中断，正在重试（第 {poll_errors} 次）"
                if poll_errors >= 120:
                    raise Exception("连续无法连接 TTS 服务，已停止轮询；任务数据已保留")
                continue
            except Exception as e:
                task["status"] = QueueTaskStatus.FAILED
                task["error"] = str(e)
                _persist_task(webui_task_id)
                break
    except Exception as e:
        logger.exception("[resume] failed webui=%s error=%s", webui_task_id, e)
        task["status"] = QueueTaskStatus.FAILED
        task["error"] = str(e)
        _persist_task(webui_task_id)
    finally:
        task["finished_at"] = datetime.now().isoformat()
        _persist_task(webui_task_id)
        current_task_id = None
        asyncio.create_task(_process_queue())
# ─── 配置与健康检查 ─────────────────────────────────────────

@app.get("/api/config")
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


@app.get("/api/tts/health")
async def tts_health():
    """代理 TTS 健康检查。"""
    try:
        resp = await http_client.get(f"{TTS_URL}/api/health", timeout=5.0)
        return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")
    except Exception as e:
        raise HTTPException(502, f"TTS 服务异常: {e}")


# ─── 参考音频管理（代理 + 本地 fallback） ───────────────────

LOCAL_VOICES_DIR = DATA_DIR / "voices"
LOCAL_VOICES_DIR.mkdir(parents=True, exist_ok=True)
FAVORITES_PATH = DATA_DIR / "favorite-voices.json"


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


class FavoriteVoicesModel(BaseModel):
    paths: list[str] = Field(default_factory=list)


# 预设音色目录（提前定义，供 proxy_audio 查找）
PRESET_VOICES_DIR = DATA_DIR / "preset-voices"


@app.get("/api/voices")
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


@app.get("/api/voice-favorites")
async def list_voice_favorites():
    """读取持久化的收藏音色路径。"""
    return {"paths": _load_favorite_paths()}


@app.put("/api/voice-favorites")
async def save_voice_favorites(payload: FavoriteVoicesModel):
    """覆盖保存收藏音色路径。"""
    return {"paths": _save_favorite_paths(payload.paths)}


@app.post("/api/voices/upload")
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


@app.post("/api/voices/rename")
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


# ─── 单段合成（代理，快速试听） ─────────────────────────────

@app.delete("/api/voices/{filename}")
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


@app.post("/api/synthesize")
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


@app.get("/api/audio/{filename}")
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


# ─── 双人播客合成（异步任务 + SSE 进度） ────────────────────

@app.post("/api/podcast/generate")
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


@app.get("/api/podcast/status/{task_id}")
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


@app.get("/api/podcast/task/{task_id}")
async def get_task(task_id: str):
    """查询任务状态（非 SSE，单次查询）。"""
    try:
        resp = await http_client.get(f"{TTS_URL}/api/task/{task_id}", timeout=10.0)
        if resp.status_code == 404:
            raise HTTPException(404, "任务不存在")
        return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")


@app.get("/api/podcast/audio/{task_id}")
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


@app.get("/api/tasks")
async def list_tasks():
    """列出 TTS 服务上的最近任务。"""
    try:
        resp = await http_client.get(f"{TTS_URL}/api/tasks", timeout=10.0)
        return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, f"无法连接 TTS 服务: {TTS_URL}")


# ─── 项目管理（本地 JSON 存储） ─────────────────────────────

@app.get("/api/projects")
async def list_projects():
    """列出所有保存的项目。"""
    projects = []
    for f in sorted(PROJECTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            projects.append({
                "id": data.get("id"),
                "name": data.get("name", "未命名"),
                "updated_at": data.get("updated_at"),
                "line_count": len(data.get("lines", [])),
            })
        except Exception:
            continue
    return {"projects": projects, "count": len(projects)}


@app.post("/api/projects")
async def save_project(project: ProjectModel):
    """保存（或新建）项目。"""
    project_id = _save_project(project)
    return {"id": project_id, "name": project.name, "updated_at": project.updated_at}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    """加载项目。"""
    project = _load_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, project: ProjectModel):
    """更新项目。"""
    if not _project_path(project_id).exists():
        raise HTTPException(404, "项目不存在")
    project.id = project_id
    _save_project(project)
    return {"id": project_id, "name": project.name, "updated_at": project.updated_at}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目。"""
    path = _project_path(project_id)
    if not path.exists():
        raise HTTPException(404, "项目不存在")
    path.unlink()
    return {"deleted": project_id}


@app.post("/api/projects/import")
async def import_project_from_text(request: Request):
    """从纯文本导入对话脚本。

    解析 "A: 文本" / "B: 文本" 格式，返回 lines 数组（不保存）。
    """
    body = await request.json()
    raw_text = body.get("text", "")
    speaker_a = body.get("speaker_a_name", "A")
    speaker_b = body.get("speaker_b_name", "B")

    lines = []
    for raw_line in raw_text.strip().split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        # 支持 "A: 文本" / "A：文本" / "A 文本" 等格式
        if ":" in raw_line:
            spk, text = raw_line.split(":", 1)
        elif "：" in raw_line:
            spk, text = raw_line.split("：", 1)
        else:
            # 无前缀，交替分配
            spk = "A" if len(lines) % 2 == 0 else "B"
            text = raw_line
        spk = spk.strip().upper()
        if spk not in ("A", "B"):
            spk = "A" if len(lines) % 2 == 0 else "B"
        lines.append({
            "speaker": spk,
            "text": text.strip(),
            "emotion": {"mode": 0, "vector": [0]*8, "weight": 0.65, "random": False},
        })
    return {"lines": lines, "count": len(lines)}


# ─── 预设音色 ───────────────────────────────────────────────

# PRESET_VOICES_DIR 已在上方定义（与 LOCAL_VOICES_DIR 同处）


@app.get("/api/preset-voices")
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


@app.get("/api/preset-voices/{category}")
async def list_preset_voices_by_category(category: str):
    """按分类列出预设音色（female/male/emotion）。"""
    data = await list_preset_voices()
    cats = data.get("categories", data)
    if category in cats:
        return {"voices": cats[category], "count": len(cats[category])}
    raise HTTPException(404, f"分类不存在: {category}")


@app.post("/api/preset-voices/upload-to-tts")
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


# ─── 术语词汇表 ─────────────────────────────────────────────

GLOSSARY_PATH = DATA_DIR / "glossary.json"


class GlossaryTerm(BaseModel):
    original: str
    replacement: str


def _load_glossary() -> list:
    if GLOSSARY_PATH.exists():
        return json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    return []


def _save_glossary(terms: list):
    GLOSSARY_PATH.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/glossary")
async def get_glossary():
    """获取术语词汇表。"""
    terms = _load_glossary()
    return {"terms": terms, "count": len(terms)}


@app.post("/api/glossary")
async def add_glossary_term(term: GlossaryTerm):
    """添加术语。"""
    if not term.original.strip():
        raise HTTPException(400, "原词不能为空")
    terms = _load_glossary()
    # 去重
    terms = [t for t in terms if t.get("original") != term.original]
    terms.append({"original": term.original, "replacement": term.replacement})
    _save_glossary(terms)
    return {"terms": terms, "count": len(terms)}


@app.delete("/api/glossary/{original}")
async def delete_glossary_term(original: str):
    """删除术语。"""
    terms = _load_glossary()
    terms = [t for t in terms if t.get("original") != original]
    _save_glossary(terms)
    return {"terms": terms, "count": len(terms)}


@app.put("/api/glossary")
async def update_glossary(request: Request):
    """批量更新术语表。"""
    body = await request.json()
    terms = body.get("terms", [])
    _save_glossary(terms)
    return {"terms": terms, "count": len(terms)}


@app.post("/api/glossary/apply")
async def apply_glossary(request: Request):
    """对文本应用术语替换，返回替换后的文本。"""
    body = await request.json()
    text = body.get("text", "")
    terms = _load_glossary()
    for t in terms:
        text = text.replace(t["original"], t["replacement"])
    return {"text": text}


# ─── 音色预设 ───────────────────────────────────────────────

VOICE_PRESETS_DIR = DATA_DIR / "voice-presets"
VOICE_PRESETS_DIR.mkdir(parents=True, exist_ok=True)


class VoicePresetModel(BaseModel):
    id: Optional[str] = None
    name: str
    voice_path: Optional[str] = None
    voice_name: Optional[str] = None
    speed: float = 1.0
    role_speed: Optional[float] = None
    emotion: dict = Field(default_factory=lambda: {
        "mode": 0, "audio_path": None, "vector": [0.0]*8,
        "weight": 0.65, "text": None, "random": False,
    })
    is_preset: bool = False  # 是否为内置预设（vs 用户自建）
    created_at: Optional[str] = None


@app.get("/api/voice-presets")
async def list_voice_presets():
    """列出所有音色预设。"""
    presets = []
    for f in sorted(VOICE_PRESETS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            presets.append({
                "id": data.get("id"),
                "name": data.get("name"),
                "voice_name": data.get("voice_name"),
                "speed": data.get("role_speed", data.get("speed", 1.0)),
                "is_preset": data.get("is_preset", False),
                "created_at": data.get("created_at"),
            })
        except Exception:
            continue
    return {"presets": presets, "count": len(presets)}


@app.post("/api/voice-presets")
async def save_voice_preset(preset: VoicePresetModel):
    """保存音色预设。"""
    if preset.id is None:
        preset.id = f"vp_{uuid.uuid4().hex[:10]}"
        preset.created_at = datetime.now().isoformat()
    path = VOICE_PRESETS_DIR / f"{preset.id}.json"
    path.write_text(preset.model_dump_json(indent=2), encoding="utf-8")
    return {"id": preset.id, "name": preset.name, "created_at": preset.created_at}


@app.put("/api/voice-presets/{preset_id}")
async def rename_voice_preset(preset_id: str, request: Request):
    """重命名用户保存的角色预设。"""
    path = VOICE_PRESETS_DIR / f"{Path(preset_id).name}.json"
    if not path.exists():
        raise HTTPException(404, "预设不存在")
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "预设名称不能为空")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["name"] = name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": preset_id, "name": name}


@app.get("/api/voice-presets/{preset_id}")
async def get_voice_preset(preset_id: str):
    """加载音色预设详情。"""
    path = VOICE_PRESETS_DIR / f"{preset_id}.json"
    if not path.exists():
        raise HTTPException(404, "预设不存在")
    return json.loads(path.read_text(encoding="utf-8"))


@app.delete("/api/voice-presets/{preset_id}")
async def delete_voice_preset(preset_id: str):
    """删除音色预设。"""
    path = VOICE_PRESETS_DIR / f"{preset_id}.json"
    if not path.exists():
        raise HTTPException(404, "预设不存在")
    path.unlink()
    return {"deleted": preset_id}


# ─── 任务队列 ───────────────────────────────────────────────

class QueueTaskStatus:
    QUEUED = "queued"        # 排队中
    PAUSED = "paused"        # 已暂停，暂不参与调度
    RUNNING = "running"      # 正在 TTS 上合成
    SUCCESS = "success"      # 完成
    FAILED = "failed"        # 失败
    SYNCING = "syncing"      # 等待 TTS 连接恢复
    INTERRUPTED = "interrupted"  # 任务中断，可重新提交
    CANCELLED = "cancelled"  # 已取消


queue_tasks: dict = {}  # task_id -> task_info
queue_order: list = []  # 排队顺序
current_task_id: Optional[str] = None  # 当前正在处理的任务
queue_lock = asyncio.Lock()

# 队列持久化目录
QUEUE_DIR = DATA_DIR / "queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

QUEUE_ORDER_FILE = QUEUE_DIR / "_queue_order.json"


def _queue_file(task_id: str) -> Path:
    """单个队列任务的持久化路径。"""
    safe_id = task_id.replace("/", "_")
    return QUEUE_DIR / f"{safe_id}.json"


def _persist_queue_order() -> None:
    """把 queue_order 持久化到磁盘，重启后恢复执行顺序。"""
    try:
        QUEUE_ORDER_FILE.write_text(
            json.dumps(queue_order, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("[queue] persist queue_order failed error=%s", e)


def _load_queue_order() -> None:
    """从磁盘恢复 queue_order，并补入外部放入的 queued 任务。

    任务文件可以由 WebUI 以外的方式生成，因此不能只相信
    _queue_order.json；否则该文件为空或漏记任务时，任务会显示为 queued
    但永远不会被 _process_queue() 取出。
    """
    global queue_order
    saved = []
    if QUEUE_ORDER_FILE.exists():
        try:
            raw = json.loads(QUEUE_ORDER_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                saved = raw
        except Exception as e:
            logger.warning("[queue] load queue_order failed error=%s", e)

    # 先保留持久化顺序，再把未被登记的 queued 任务按创建时间补到队尾。
    valid = [
        tid for tid in saved
        if tid in queue_tasks
        and queue_tasks[tid].get("status") == QueueTaskStatus.QUEUED
    ]
    missing = [
        (task.get("created_at", ""), tid)
        for tid, task in queue_tasks.items()
        if task.get("status") == QueueTaskStatus.QUEUED and tid not in valid
    ]
    missing.sort(key=lambda item: item[0])
    queue_order = valid + [tid for _, tid in missing]
    if missing:
        _persist_queue_order()
        logger.warning(
            "[queue] discovered %d queued task(s) missing from _queue_order.json: %s",
            len(missing), [tid for _, tid in missing],
        )
    logger.info("[queue] restored queue_order: %d tasks", len(queue_order))


def _persist_task(task_id: str) -> None:
    """把单个队列任务写入磁盘。"""
    task = queue_tasks.get(task_id)
    if not task:
        return
    try:
        path = _queue_file(task_id)
        path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("[queue] persist failed task=%s error=%s", task_id, e)


def _delete_persisted_task(task_id: str) -> None:
    """从磁盘删除队列任务文件。"""
    try:
        path = _queue_file(task_id)
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.warning("[queue] delete persisted failed task=%s error=%s", task_id, e)


def _load_persisted_tasks() -> None:
    """启动时从磁盘恢复队列任务。"""
    if not QUEUE_DIR.exists():
        return
    for f in sorted(QUEUE_DIR.glob("*.json")):
        # 顺序索引不是任务文件，不能按任务对象解析。
        if f == QUEUE_ORDER_FILE:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            task_id = data.get("id")
            if not task_id or task_id in queue_tasks:
                continue
            queue_tasks[task_id] = data
            # 恢复排队顺序
            if data.get("status") == QueueTaskStatus.QUEUED:
                queue_order.append(task_id)
            # WebUI 重启后，带 TTS task id 的运行任务由启动逻辑恢复轮询。
            elif data.get("status") in (QueueTaskStatus.RUNNING, QueueTaskStatus.SYNCING):
                data["status"] = QueueTaskStatus.RUNNING
                data["message"] = data.get("message") or "WebUI 重启后恢复"
        except Exception as e:
            logger.warning("[queue] load persisted task failed file=%s error=%s", f, e)
    logger.info("[queue] loaded %d persisted tasks", len(queue_tasks))
    # 用持久化的 queue_order 覆盖默认排序
    _load_queue_order()


class QueueTaskModel(BaseModel):
    project_name: str = "未命名"
    lines: list
    voices: dict
    silence: dict = Field(default_factory=lambda: {"within_segment": 200, "between_lines": 300, "speaker_switch": 500})
    params: dict = Field(default_factory=lambda: {
        "speed": 1.0, "speaker_speeds": {}, "max_text_tokens_per_segment": 120, "do_sample": True, "top_p": 0.75,
        "top_k": 20, "temperature": 0.6, "length_penalty": 0.0,
        "num_beams": 2, "repetition_penalty": 5.0, "max_mel_tokens": 1500,
        "infer_concurrency": 1,
    })
    glossary_enabled: bool = True


def _validate_queue_lines(lines: list) -> None:
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


async def _process_queue():
    """处理队列：取出队首任务提交到 TTS 服务。"""
    global current_task_id
    async with queue_lock:
        if current_task_id is not None:
            return  # 已有任务在处理
        if not queue_order:
            return
        current_task_id = queue_order.pop(0)
        task = queue_tasks[current_task_id]
        task["status"] = QueueTaskStatus.RUNNING
        task["started_at"] = datetime.now().isoformat()
        _persist_task(current_task_id)

    task = queue_tasks[current_task_id]
    try:
        _validate_queue_lines(task.get("lines", []))
        # 应用术语替换
        req_data = {
            "lines": task["lines"],
            "voices": task["voices"],
            "silence": task["silence"],
            "params": task["params"],
        }
        if task.get("glossary_enabled", True):
            terms = _load_glossary()
            if terms:
                for line in req_data["lines"]:
                    for t in terms:
                        line["text"] = line["text"].replace(t["original"], t["replacement"])

        # 提交到 TTS 服务
        resp = await http_client.post(f"{TTS_URL}/api/podcast", json=req_data, timeout=30.0)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", "提交 TTS 失败")
            except Exception:
                detail = resp.text[:1000] or f"HTTP {resp.status_code}"
            raise Exception(detail)
        tts_task_id = resp.json().get("task_id")
        if not tts_task_id:
            raise Exception("TTS 未返回 task_id")
        task["tts_task_id"] = tts_task_id
        _persist_task(current_task_id)
        logger.info("[queue] submitted task=%s tts_task=%s", current_task_id, tts_task_id)

        # 轮询 TTS 任务状态。短暂断连不能直接判失败，否则 GPU 任务仍会继续运行。
        poll_errors = 0
        while True:
            await asyncio.sleep(1.5)
            try:
                r = await http_client.get(f"{TTS_URL}/api/task/{tts_task_id}", timeout=10.0)
                if r.status_code == 404:
                    task["status"] = QueueTaskStatus.INTERRUPTED
                    task["message"] = "TTS 任务不存在，可重新提交"
                    task["error"] = "TTS 任务不存在"
                    _persist_task(current_task_id)
                    break
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
                    task["status"] = QueueTaskStatus.SUCCESS
                    task["progress"] = 1.0
                    task["output_path"] = payload.get("output_path")
                    task["audio_url"] = f"/api/podcast/audio/{tts_task_id}"
                    task["duration_sec"] = payload.get("duration_sec")
                    task["message"] = payload.get("message") or "合成完成"
                    _persist_task(current_task_id)
                    logger.info("[queue] completed task=%s tts_task=%s", current_task_id, tts_task_id)
                    break
                elif status == "failed":
                    task["status"] = QueueTaskStatus.FAILED
                    task["error"] = payload.get("error", "未知错误")
                    _persist_task(current_task_id)
                    break
                elif task.get("cancel_requested"):
                    task["status"] = QueueTaskStatus.CANCELLED
                    break
            except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError) as e:
                poll_errors += 1
                logger.warning(
                    "[queue] status poll disconnected task=%s tts_task=%s retry=%d error=%s",
                    current_task_id, tts_task_id, poll_errors, e,
                )
                task["status"] = QueueTaskStatus.SYNCING
                task["message"] = f"TTS 连接中断，等待恢复（第 {poll_errors} 次）"
                _persist_task(current_task_id)
                # 任务已在 TTS 服务端创建，继续轮询，不能把它误判为失败。
                if poll_errors >= 120:
                    task["message"] = "连续无法连接 TTS 服务，可重新提交"
                    task["error"] = "连续无法连接 TTS 服务"
                    task["status"] = QueueTaskStatus.INTERRUPTED
                    _persist_task(current_task_id)
                    break
                continue
            except Exception as e:
                task["status"] = QueueTaskStatus.FAILED
                task["error"] = str(e)
                break

    except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError) as e:
        logger.error("[queue] request disconnected before task tracking task=%s error=%s", current_task_id, e)
        task["status"] = QueueTaskStatus.INTERRUPTED
        task["message"] = "提交 TTS 时连接中断，可重新提交"
        task["error"] = f"提交 TTS 时连接中断: {e}"
        _persist_task(current_task_id)
    except Exception as e:
        logger.exception("[queue] failed task=%s error=%s", current_task_id, e)
        task["status"] = QueueTaskStatus.FAILED
        task["message"] = "任务执行失败"
        task["error"] = str(e)
        _persist_task(current_task_id)
    finally:
        task["finished_at"] = datetime.now().isoformat()
        _persist_task(current_task_id)
        current_task_id_ref = current_task_id
        current_task_id = None
        # 继续处理下一个
        asyncio.create_task(_process_queue())


@app.post("/api/queue/submit")
async def submit_to_queue(task: QueueTaskModel):
    """提交任务到队列。"""
    global current_task_id
    task_id = f"q_{uuid.uuid4().hex[:10]}"
    queue_tasks[task_id] = {
        "id": task_id,
        "project_name": task.project_name,
        "lines": task.lines,
        "voices": task.voices,
        "silence": task.silence,
        "params": task.params,
        "glossary_enabled": task.glossary_enabled,
        "status": QueueTaskStatus.QUEUED,
        "progress": 0,
        "current_line": 0,
        "total_lines": len(task.lines),
        "message": "排队中",
        "created_at": datetime.now().isoformat(),
        "cancel_requested": False,
    }
    _validate_queue_lines(task.lines)
    queue_order.append(task_id)
    _persist_task(task_id)
    _persist_queue_order()
    # 触发队列处理
    asyncio.create_task(_process_queue())
    return {"task_id": task_id, "status": "queued", "queue_position": len(queue_order)}


@app.get("/api/queue")
async def list_queue():
    """列出所有队列任务。排序：运行中 → 排队中(按执行顺序) → 终态(按创建时间倒序)。"""
    running_tasks = []
    queued_tasks = []
    terminal_tasks = []
    for t in queue_tasks.values():
        st = t.get("status")
        # 给每个任务标注 queue_position
        tid = t.get("id", "")
        if tid in queue_order:
            t["queue_position"] = queue_order.index(tid) + 1
        else:
            t["queue_position"] = None
        if st in (QueueTaskStatus.RUNNING, QueueTaskStatus.SYNCING):
            running_tasks.append(t)
        elif st == QueueTaskStatus.QUEUED:
            queued_tasks.append(t)
        elif st == QueueTaskStatus.PAUSED:
            pass
        else:
            terminal_tasks.append(t)
    # 排队任务按 queue_order 顺序排列
    queued_tasks.sort(key=lambda t: queue_order.index(t["id"]) if t["id"] in queue_order else 999)
    paused_tasks = [t for t in queue_tasks.values() if t.get("status") == QueueTaskStatus.PAUSED]
    paused_tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    # 终态任务按创建时间倒序
    terminal_tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    tasks = running_tasks + queued_tasks + paused_tasks + terminal_tasks
    return {
        "tasks": tasks,
        "count": len(tasks),
        "current": current_task_id,
        "queued": len(queue_order),
        "queue_order": list(queue_order),
    }


@app.put("/api/queue/{task_id}")
async def update_queue_task(task_id: str, payload: QueueTaskModel):
    """编辑尚未执行的排队任务。运行中及终态任务不可修改。"""
    task = queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.get("status") != QueueTaskStatus.QUEUED:
        raise HTTPException(409, "只有尚未执行的排队任务可以编辑")
    _validate_queue_lines(payload.lines)
    task.update({
        "project_name": payload.project_name,
        "lines": payload.lines,
        "voices": payload.voices,
        "silence": payload.silence,
        "params": payload.params,
        "glossary_enabled": payload.glossary_enabled,
        "total_lines": len(payload.lines),
        "message": "已更新，等待执行",
        "updated_at": datetime.now().isoformat(),
    })
    _persist_task(task_id)
    return task


class QueueTaskNameModel(BaseModel):
    project_name: str


@app.patch("/api/queue/{task_id}/name")
async def update_queue_task_name(task_id: str, payload: QueueTaskNameModel):
    """修改尚未执行任务的名称。"""
    task = queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    editable_statuses = {
        QueueTaskStatus.QUEUED,
        QueueTaskStatus.PAUSED,
        QueueTaskStatus.FAILED,
        QueueTaskStatus.INTERRUPTED,
        QueueTaskStatus.CANCELLED,
    }
    if task.get("status") not in editable_statuses:
        raise HTTPException(409, "任务正在执行或已完成，当前不能修改名称")
    name = payload.project_name.strip()
    if not name:
        raise HTTPException(400, "任务名称不能为空")
    if len(name) > 100:
        raise HTTPException(400, "任务名称不能超过 100 个字符")
    task["project_name"] = name
    task["updated_at"] = datetime.now().isoformat()
    _persist_task(task_id)
    return {"task_id": task_id, "project_name": name, "status": task["status"]}


@app.post("/api/queue/{task_id}/retry")
async def retry_queue_task(task_id: str):
    """重新排队执行失败、中断、暂停或取消的任务，从第一行重新生成。"""
    global current_task_id
    task = queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.get("status") not in (QueueTaskStatus.FAILED, QueueTaskStatus.INTERRUPTED, QueueTaskStatus.PAUSED, QueueTaskStatus.CANCELLED):
        raise HTTPException(409, "只有失败、中断、暂停或取消任务可以重新提交")
    if current_task_id == task_id:
        raise HTTPException(409, "任务当前仍在执行")
    task.update({
        "status": QueueTaskStatus.QUEUED,
        "progress": 0,
        "current_line": 0,
        "total_lines": len(task.get("lines", [])),
        "message": "已重新排队",
        "error": None,
        "tts_task_id": None,
        "audio_url": None,
        "output_path": None,
        "duration_sec": None,
        "cancel_requested": False,
        "started_at": None,
        "finished_at": None,
        "retried_at": datetime.now().isoformat(),
    })
    if task_id not in queue_order:
        queue_order.append(task_id)
    _persist_task(task_id)
    _persist_queue_order()
    asyncio.create_task(_process_queue())
    return {"task_id": task_id, "status": task["status"], "queue_position": queue_order.index(task_id) + 1}


@app.post("/api/queue/bulk-pause")
async def pause_queued_tasks():
    """暂停所有排队中的任务；正在合成的任务不受影响。"""
    paused = []
    for task_id in list(queue_order):
        task = queue_tasks.get(task_id)
        if task and task.get("status") == QueueTaskStatus.QUEUED:
            task["status"] = QueueTaskStatus.PAUSED
            task["message"] = "已暂停"
            task["paused_at"] = datetime.now().isoformat()
            _persist_task(task_id)
            paused.append(task_id)
    queue_order.clear()
    _persist_queue_order()
    return {"paused": paused, "count": len(paused)}


@app.post("/api/queue/bulk-resume")
async def resume_paused_tasks():
    """将所有暂停任务按原创建时间恢复到队列末尾。"""
    resumed = []
    paused = [
        task for task in queue_tasks.values()
        if task.get("status") == QueueTaskStatus.PAUSED
    ]
    paused.sort(key=lambda task: task.get("created_at", ""))
    for task in paused:
        task_id = task["id"]
        task["status"] = QueueTaskStatus.QUEUED
        task["message"] = "已重新排队"
        task["resumed_at"] = datetime.now().isoformat()
        queue_order.append(task_id)
        _persist_task(task_id)
        resumed.append(task_id)
    _persist_queue_order()
    if resumed:
        asyncio.create_task(_process_queue())
    return {"resumed": resumed, "count": len(resumed)}


@app.get("/api/queue/{task_id}")
async def get_queue_task(task_id: str):
    """获取队列中单个任务状态。"""
    if task_id not in queue_tasks:
        raise HTTPException(404, "任务不存在")
    return queue_tasks[task_id]


@app.delete("/api/queue/{task_id}")
async def cancel_queue_task(task_id: str):
    """取消/删除队列任务。排队中直接删除，运行中标记取消。"""
    global current_task_id
    if task_id not in queue_tasks:
        raise HTTPException(404, "任务不存在")
    task = queue_tasks[task_id]
    if task["status"] == QueueTaskStatus.QUEUED:
        # 排队中：直接从队列移除
        if task_id in queue_order:
            queue_order.remove(task_id)
        task["status"] = QueueTaskStatus.CANCELLED
        task["message"] = "已取消"
        _persist_task(task_id)
        _persist_queue_order()
        return {"cancelled": task_id}
    elif task["status"] == QueueTaskStatus.RUNNING:
        # 运行中：标记取消（TTS 无法真正停止，但后续不再更新）
        task["cancel_requested"] = True
        task["message"] = "取消请求已发送"
        _persist_task(task_id)
        return {"cancelling": task_id}
    else:
        # 已完成/失败：从列表和磁盘删除
        del queue_tasks[task_id]
        _delete_persisted_task(task_id)
        return {"deleted": task_id}


@app.delete("/api/queue")
async def clear_finished_tasks():
    """清空所有已完成/失败/取消的任务。"""
    global current_task_id
    to_remove = [tid for tid, t in queue_tasks.items()
                 if t["status"] in (QueueTaskStatus.SUCCESS, QueueTaskStatus.FAILED, QueueTaskStatus.CANCELLED)]
    for tid in to_remove:
        del queue_tasks[tid]
        _delete_persisted_task(tid)
    return {"cleared": len(to_remove), "remaining": len(queue_tasks)}


@app.patch("/api/queue/reorder")
async def reorder_queue(payload: dict):
    """拖拽排序：接收新的排队任务 ID 顺序，重写 queue_order。"""
    new_order = payload.get("task_ids", [])
    if not isinstance(new_order, list):
        raise HTTPException(400, "task_ids 必须是数组")
    async with queue_lock:
        old_set = set(queue_order)
        new_set = set(new_order)
        # 校验：新顺序必须包含且仅包含当前所有排队任务
        if new_set != old_set:
            missing = old_set - new_set
            extra = new_set - old_set
            detail = []
            if missing:
                detail.append(f"缺少: {missing}")
            if extra:
                detail.append(f"多余: {extra}")
            raise HTTPException(400, f"任务列表不匹配 {'; '.join(detail)}")
        # 校验所有任务确实是 queued 状态
        for tid in new_order:
            if queue_tasks[tid].get("status") != QueueTaskStatus.QUEUED:
                raise HTTPException(400, f"任务 {tid} 不是排队状态，无法排序")
        queue_order.clear()
        queue_order.extend(new_order)
        _persist_queue_order()
    logger.info("[queue] reordered: %s", new_order)
    return {"queue_order": list(queue_order)}


if __name__ == "__main__":
    import uvicorn
    print(f">> Podcast WebUI Backend")
    print(f"   TTS URL:  {TTS_URL}")
    print(f"   Data dir: {DATA_DIR}")
    print(f"   Listen:   {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
