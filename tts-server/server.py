"""IndexTTS2 TTS 服务端。

将 IndexTTS2 包装为 HTTP API，供 WebUI 后端调用。
部署在与 index-tts 源码同一台 GPU 服务器上。

启动方式：
  python server.py --indextts-home /root/index-tts \
                   --model-dir /mnt/storage/index-tts-data/checkpoints \
                   --voices-dir /mnt/storage/index-tts-data/voices \
                   --output-dir /mnt/storage/index-tts-data/outputs \
                   --device cuda:0 --fp16 --host 0.0.0.0 --port 8000

也可用环境变量配置（见 _env 函数）。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# ─── 参数解析 ───────────────────────────────────────────────

parser = argparse.ArgumentParser(description="IndexTTS2 TTS Server")
parser.add_argument("--indextts-home", default=os.environ.get("INDEXTTS_HOME", "/root/index-tts"),
                    help="index-tts 源码目录（用于 import indextts）")
parser.add_argument("--model-dir", default=os.environ.get("MODEL_DIR", "/mnt/storage/index-tts-data/checkpoints"),
                    help="模型权重目录")
parser.add_argument("--voices-dir", default=os.environ.get("VOICES_DIR", "/mnt/storage/index-tts-data/voices"),
                    help="参考音频存放目录")
parser.add_argument("--output-dir", default=os.environ.get("OUTPUT_DIR", "/mnt/storage/index-tts-data/outputs"),
                    help="生成音频输出目录")
parser.add_argument("--device", default=os.environ.get("DEVICE", "cuda:0"), help="推理设备")
parser.add_argument("--fp16", action="store_true", default=os.environ.get("USE_FP16", "1") == "1",
                    help="启用 FP16")
parser.add_argument("--deepspeed", action="store_true", default=os.environ.get("USE_DEEPSPEED", "0") == "1",
                    help="启用 DeepSpeed GPT 推理")
parser.add_argument("--cuda-kernel", action="store_true", default=os.environ.get("USE_CUDA_KERNEL", "0") == "1",
                    help="启用 BigVGAN CUDA kernel")
parser.add_argument("--accel", action="store_true", default=os.environ.get("USE_ACCEL", "0") == "1",
                    help="启用 GPT2 acceleration engine")
parser.add_argument("--torch-compile", action="store_true", default=os.environ.get("USE_TORCH_COMPILE", "0") == "1",
                    help="启用 s2mel torch.compile（首次推理可能较慢）")
parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="监听地址")
parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="监听端口")
args, _ = parser.parse_known_args()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [tts-server] %(message)s",
)
logger = logging.getLogger("tts-server")

# 将 index-tts 源码加入 sys.path 以便 import indextts
_indextts_src = Path(args.indextts_home)
if _indextts_src.exists():
    sys.path.insert(0, str(_indextts_src))
    sys.path.insert(0, str(_indextts_src / "indextts"))

VOICES_DIR = Path(args.voices_dir)
OUTPUT_DIR = Path(args.output_dir)
VOICES_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── 模型加载 ───────────────────────────────────────────────

tts = None
_model_lock = threading.Lock()  # 推理锁，GPU 一次只跑一个

print(f">> IndexTTS2 TTS Server")
print(f"   indextts-home: {args.indextts_home}")
print(f"   model-dir:     {args.model_dir}")
print(f"   voices-dir:    {VOICES_DIR}")
print(f"   output-dir:    {OUTPUT_DIR}")
print(
    f"   device:        {args.device}  fp16: {args.fp16} "
    f"deepspeed: {args.deepspeed} cuda_kernel: {args.cuda_kernel} "
    f"accel: {args.accel} torch_compile: {args.torch_compile}"
)
print(f">> loading model (this may take a while)...")

# 加载失败时的可操作提示。失败被下面的 except 捕获后进程**照常监听**
# （/api/health 仍返回 200，只是 status=no_model），所以这句话往往是唯一的线索 ——
# 值得翻译成人能照着做的动作，而不是原样抛一句第三方库的英文。
# 四个签名覆盖了实际见过的成因，首例就是 2026-09-30 spacehpc 上缺 ninja。
_LOAD_FAILURE_HINTS: tuple[tuple[str, str, str], ...] = (
    ("ninja is required",
     "缺少 ninja（JIT 编译 CUDA 扩展用）",
     "装：{bin}/pip install ninja。⚠ 必须让 ninja 出现在 PATH 里 —— 它是被当**子进程**"
     "调用的（PATH 查找），而启动脚本直接调用 {py}、并没有 activate venv，"
     "所以只装不软链仍会报同一个错：ln -sf {bin}/ninja /usr/local/bin/ninja。"
     "--deepspeed 没有兜底（不像 --cuda-kernel），它一失败就是整个模型加载失败"),
    ("no cuda runtime is found",
     "找不到 CUDA 运行时（nvcc / CUDA_HOME）",
     "JIT 编译需要与本机 torch 匹配的 CUDA toolkit。装 cudatoolkit 或设 "
     "CUDA_HOME=/usr/local/cuda；本机不具备就把 --deepspeed / --cuda-kernel 关掉"),
    ("detected cuda version",
     "nvcc 版本与 torch 的 CUDA 版本不一致",
     "本机 torch 编译用的 CUDA = {torch_cuda}，而找得到的 nvcc 是另一个版本 —— 扩展编译必失败。"
     "让 CUDA_HOME 指向与 torch 匹配的那套 toolkit，或关掉加速项"),
    ("out of memory",
     "显存不足（OOM）",
     "DeepSpeed 默认按 90% 显存做预算，与别的进程挤在一起就会 OOM。"
     "先 nvidia-smi 看占用；也可以关掉 --deepspeed 用普通推理"),
)


def _explain_load_failure(exc: BaseException) -> None:
    """把模型加载失败翻译成「下一步做什么」。只打印，不改行为。"""
    py = sys.executable
    bin_dir = str(Path(py).parent)
    try:
        import torch  # noqa: PLC0415

        torch_cuda = torch.version.cuda or "无"
    except Exception:
        torch_cuda = "未知"
    text = repr(exc).lower()
    hit = next((h for h in _LOAD_FAILURE_HINTS if h[0] in text), None)
    if hit:
        print(f"!! ─ 定位：{hit[1]}")
        print("!!   " + hit[2].format(bin=bin_dir, py=py, torch_cuda=torch_cuda))
    else:
        print("!! ─ 定位：没匹配到已知签名（ninja / nvcc / 版本不匹配 / OOM），看下面体检结果")
    print(f"!! ─ 本次开关：deepspeed={args.deepspeed} cuda_kernel={args.cuda_kernel} "
          f"fp16={args.fp16} accel={args.accel} torch_compile={args.torch_compile}")
    print(f"!! ─ 逐项体检：{py} {Path(__file__).resolve().parent / 'doctor.py'} --deep")
    print("!! ─ 后果：backend 探活会看到 model_loaded=false → 判该资源不可用 → "
          "合成全部溢出到云端（在花钱）")


try:
    from indextts.infer_v2 import IndexTTS2
    tts = IndexTTS2(
        cfg_path=str(Path(args.model_dir) / "config.yaml"),
        model_dir=str(args.model_dir),
        use_fp16=args.fp16,
        device=args.device,
        use_cuda_kernel=args.cuda_kernel,
        use_deepspeed=args.deepspeed,
        use_accel=args.accel,
        use_torch_compile=args.torch_compile,
    )
    print(">> model loaded successfully")
except Exception as e:
    print(f"!! WARNING: model load failed: {e}")
    _explain_load_failure(e)
    print("!! server will start, but inference will not work until model is available")
    tts = None

# ─── 任务管理 ───────────────────────────────────────────────

class TaskInfo:
    def __init__(self, task_id: str, kind: str):
        self.task_id = task_id
        self.kind = kind  # "podcast" | "synthesize"
        self.status = "pending"  # pending | running | completed | failed
        self.progress = 0  # 0-100
        self.current_line = 0
        self.total_lines = 0
        self.message = ""
        self.output_path: Optional[str] = None
        self.duration_sec: float = 0
        self.error: Optional[str] = None
        self.created_at = datetime.now().isoformat()
        self.completed_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "current_line": self.current_line,
            "total_lines": self.total_lines,
            "message": self.message,
            "output_filename": Path(self.output_path).name if self.output_path else None,
            # 保留完整路径供 WebUI 后端记录；音频实际访问仍通过 task audio 代理。
            "output_path": self.output_path,
            "duration_sec": round(self.duration_sec, 2),
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


_tasks: dict[str, TaskInfo] = {}
_tasks_lock = threading.Lock()


def _make_progress_callback(task: TaskInfo):
    def cb(current, total, line_text, message):
        task.current_line = current
        task.total_lines = total
        task.message = message
        if total > 0:
            task.progress = int(current / total * 100)
        task.status = "running"
    return cb


# ─── 请求模型 ───────────────────────────────────────────────

class EmotionModel(BaseModel):
    mode: int = 0
    audio_path: Optional[str] = None
    vector: list = Field(default_factory=lambda: [0.0] * 8)
    weight: float = 0.65
    text: Optional[str] = None
    random: bool = False


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


class PodcastLineModel(BaseModel):
    speaker: str
    text: str
    emotion: EmotionModel = Field(default_factory=EmotionModel)
    silence_after_ms: Optional[int] = None


class PodcastRequestModel(BaseModel):
    lines: list[PodcastLineModel]
    voices: dict  # {"A": "/path/voice_a.wav", "B": "/path/voice_b.wav"}
    silence: SilenceModel = Field(default_factory=SilenceModel)
    params: GenerationParamsModel = Field(default_factory=GenerationParamsModel)


class RenameVoiceRequestModel(BaseModel):
    old_name: str
    new_name: str


class SynthesizeRequestModel(BaseModel):
    voice: str  # 参考音频路径
    text: str
    output_format: str = "wav"
    emotion: EmotionModel = Field(default_factory=EmotionModel)
    interval_silence: int = 200
    max_text_tokens_per_segment: int = 120
    params: GenerationParamsModel = Field(default_factory=GenerationParamsModel)


# ─── FastAPI 应用 ───────────────────────────────────────────

app = FastAPI(title="IndexTTS2 TTS Server", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    # 与 _apply_speed 共用同一个常量，避免两处各写一份解析规则后走偏
    from podcast_engine import NORM_ENABLED
    return {
        "status": "ok" if tts is not None else "no_model",
        "model_loaded": tts is not None,
        # 能力自述：本壳**默认不做响度归一**（2026-10-09 退役）—— 归一已统一由
        # backend 的池门面执行（webui-backend/app/audio_norm.py）。本字段如实反映
        # 本壳当前能力：默认 False；仅当启动环境显式设 PODCAST_NORM=gain 应急回退
        # 时才是 True。backend 已不再据此跳过归一（池层无条件执行一次），保留本
        # 字段供状态页与排障观测。
        # 历史（2026-09-30）：这里曾硬编码 True，让 backend 跳过自己那一次以免
        # 重复归一；那条规则已随归一的统一而废除。
        "normalizes_loudness": NORM_ENABLED,
        "device": args.device,
        "fp16": args.fp16,
        "model_dir": args.model_dir,
        "voices_dir": str(VOICES_DIR),
        "output_dir": str(OUTPUT_DIR),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/voices")
def list_voices():
    """列出可用的参考音频文件。"""
    voices = []
    for ext in ("*.wav", "*.mp3", "*.flac", "*.ogg", "*.webm"):
        for f in sorted(VOICES_DIR.glob(ext)):
            voices.append({
                "name": f.name,
                "path": str(f),
                "size_kb": round(f.stat().st_size / 1024, 1),
                "source": "custom",
                "renameable": True,
                "deletable": True,
            })
    return {"voices": voices, "count": len(voices)}


@app.post("/api/voices/upload")
async def upload_voice(file: UploadFile = File(...), name: str = Form(None)):
    """上传参考音频到 voices 目录。"""
    original_name = file.filename or ""
    if not original_name:
        logger.warning("[voice-upload] rejected empty filename custom_name=%r", name)
        raise HTTPException(400, "文件名为空")

    original_path = Path(original_name)
    safe_original_name = original_path.name
    ext = original_path.suffix.lower()
    allowed = (".wav", ".mp3", ".flac", ".ogg", ".webm")
    if ext not in allowed:
        logger.warning(
            "[voice-upload] rejected unsupported extension original=%r ext=%r custom_name=%r allowed=%s",
            original_name, ext, name, allowed,
        )
        raise HTTPException(400, f"仅支持 {allowed} 格式，收到: {ext or '无扩展名'}")

    custom_name = (name or "").strip()
    if custom_name and (
        Path(custom_name).name != custom_name
        or re.search(r"[\\\\/:*?\"<>|\x00-\x1f]", custom_name)
    ):
        logger.warning("[voice-upload] rejected illegal custom name=%r original=%r", custom_name, original_name)
        raise HTTPException(400, "音色名称包含非法文件名字符")
    custom_stem = Path(custom_name).stem if custom_name else ""
    if custom_name and not custom_stem:
        raise HTTPException(400, "音色名称不能为空")

    safe_name = custom_stem + ext if custom_stem else safe_original_name
    dest = VOICES_DIR / safe_name
    content = await file.read()
    logger.info(
        "[voice-upload] received original=%r custom_name=%r safe_name=%r ext=%s mime=%s size=%d dest=%s",
        original_name, custom_name or None, safe_name, ext,
        file.content_type or "application/octet-stream", len(content), dest,
    )
    if dest.exists():
        logger.warning("[voice-upload] rejected duplicate destination=%s", dest)
        raise HTTPException(409, f"音色名称已存在: {safe_name}")
    dest.write_bytes(content)
    logger.info("[voice-upload] saved path=%s size=%d", dest, len(content))
    return {"name": dest.name, "path": str(dest), "size_kb": round(len(content) / 1024, 1)}


@app.post("/api/voices/rename")
def rename_voice(req: RenameVoiceRequestModel):
    """重命名 voices 目录中的自定义参考音频。"""
    if not req.old_name or not req.new_name:
        raise HTTPException(400, "缺少参数")
    old_path = VOICES_DIR / Path(req.old_name).name
    safe_new = Path(req.new_name).name
    if Path(safe_new).suffix == "":
        safe_new += old_path.suffix
    new_path = VOICES_DIR / safe_new
    if not old_path.exists():
        raise HTTPException(404, "原音频不存在")
    if new_path.exists() and new_path != old_path:
        raise HTTPException(409, "目标名称已存在")
    old_path.rename(new_path)
    return {"name": new_path.name, "path": str(new_path)}


@app.delete("/api/voices/{filename}")
def delete_voice(filename: str):
    """删除 voices 目录中的自定义参考音频。"""
    safe_name = Path(filename).name
    target = VOICES_DIR / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "自定义音频不存在")
    target.unlink()
    return {"deleted": safe_name}


@app.post("/api/synthesize")
def synthesize(req: SynthesizeRequestModel):
    """同步单段合成（用于快速试听单行）。

    直接阻塞返回结果，适合短文本。
    """
    if tts is None:
        raise HTTPException(503, "模型未加载")
    if not os.path.exists(req.voice):
        raise HTTPException(400, f"参考音频不存在: {req.voice}")

    output_path = str(OUTPUT_DIR / f"synth_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav")

    # 转换为引擎数据结构
    from podcast_engine import (
        EmotionConfig, GenerationParams, _sanitize_text, log_text_tokens,
    )
    emo = EmotionConfig(
        mode=req.emotion.mode, audio_path=req.emotion.audio_path,
        vector=req.emotion.vector, weight=req.emotion.weight,
        text=req.emotion.text, random=req.emotion.random,
    )
    gen = GenerationParams(
        speed=req.params.speed,
        max_text_tokens_per_segment=req.params.max_text_tokens_per_segment,
        do_sample=req.params.do_sample, top_p=req.params.top_p,
        top_k=req.params.top_k, temperature=req.params.temperature,
        length_penalty=req.params.length_penalty, num_beams=req.params.num_beams,
        repetition_penalty=req.params.repetition_penalty,
        max_mel_tokens=req.params.max_mel_tokens,
    )

    infer_kwargs = {
        "spk_audio_prompt": req.voice,
        # 单段试听与播客路径使用同一套年份/时间/人名文本预处理。
        "text": _sanitize_text(req.text),
        "output_path": output_path,
        "interval_silence": int(req.interval_silence),
        "verbose": False,
    }
    infer_kwargs.update(emo.to_infer_kwargs(tts))
    infer_kwargs.update(gen.to_infer_kwargs())

    # 文本进模型前的最终形态（受 TTS_LOG_TOKENS 控制）
    log_text_tokens(tts, infer_kwargs["text"], "synthesize")

    with _model_lock:
        try:
            from podcast_engine import synthesize_line_with_pauses
            synthesize_line_with_pauses(
                tts, req.text, output_path, infer_kwargs, req.params.speed,
                token_log_tag="synthesize",
            )
        except Exception as e:
            raise HTTPException(500, f"合成失败: {e}")

    from podcast_engine import get_wav_duration
    duration = get_wav_duration(output_path)
    return {
        "output_filename": Path(output_path).name,
        "output_path": output_path,
        "duration_sec": round(duration, 2),
    }


# ── 已无活调用方的端点：只打一次性弃用告警，**不改变行为**（2026-09-30）──
#
# 背景：backend 自 2026-09-20 起改走引擎适配层合成（`/api/synthesize`），
# 「播客异步任务」这条体系再没有调用方。但**不能直接删**：
#   - 它曾是 tts-server 与 backend 之间的主链路，历史任务恢复与旧版 backend 可能还在用；
#   - 删掉会让「把 backend 回滚到旧版本」这条路彻底断掉。
# 所以先留着 + 打一次性告警，观察一段确认线上确实没人调了再删。
#
# ⚠️ 别把同一套逻辑套到 `split_pauses` / `synthesize_line_with_pauses` /
# `_concatenate_wav_segments` 上 —— 它们**仍在 `/api/synthesize` 的链路上**
# （行内停顿标记 `[pause:N]` 就靠它），是活代码。2026-09-30 的功能归属审查报告
# 曾把它们误列为死代码，已在该报告里更正。
_DEPRECATION_WARNED: set = set()


def _warn_deprecated(what: str, why: str) -> None:
    """对已无活调用方的入口打一次性弃用告警（不影响返回值与副作用）。"""
    if what in _DEPRECATION_WARNED:
        return
    _DEPRECATION_WARNED.add(what)
    logger.warning(
        "[deprecated] %s 已无活调用方（backend 改走 /api/synthesize 的引擎适配层），"
        "保留仅为兼容、将来可能移除。%s", what, why,
    )


@app.post("/api/podcast")
def create_podcast(req: PodcastRequestModel):
    """提交双人播客合成任务（异步），返回 task_id。"""
    _warn_deprecated("POST /api/podcast", "调用方应改用 backend 的 /api/queue/submit")
    logger.info(
        "[podcast] submit lines=%d voices=%s device=%s",
        len(req.lines), list(req.voices.keys()), args.device,
    )
    if tts is None:
        raise HTTPException(503, "模型未加载")
    if not req.lines:
        raise HTTPException(400, "对话脚本为空")
    blank_lines = [index for index, line in enumerate(req.lines, start=1) if not line.text.strip()]
    if blank_lines:
        preview = ", ".join(str(index) for index in blank_lines[:10])
        suffix = " 等" if len(blank_lines) > 10 else ""
        raise HTTPException(400, f"第 {preview}{suffix} 行台词为空，请补充内容后再提交")
    # 校验参考音频
    for speaker, path in req.voices.items():
        if not os.path.exists(path):
            raise HTTPException(400, f"说话人 {speaker} 的参考音频不存在: {path}")

    task_id = uuid.uuid4().hex[:12]
    task = TaskInfo(task_id, "podcast")
    task.total_lines = len(req.lines)
    with _tasks_lock:
        _tasks[task_id] = task

    # 后台线程执行
    thread = threading.Thread(target=_run_podcast_task, args=(task_id, req), daemon=True)
    thread.start()
    logger.info("[podcast] accepted task_id=%s total_lines=%d", task_id, len(req.lines))

    return {"task_id": task_id, "status": "pending", "total_lines": len(req.lines)}


def _run_podcast_task(task_id: str, req: PodcastRequestModel):
    from podcast_engine import (
        PodcastRequest, PodcastLine, EmotionConfig,
        SilenceConfig, GenerationParams, synthesize_podcast,
    )

    task = _tasks[task_id]
    task.status = "pending"
    task.message = "排队等待推理资源..."

    # 转换请求
    lines = []
    for lm in req.lines:
        lines.append(PodcastLine(
            speaker=lm.speaker, text=lm.text,
            silence_after_ms=lm.silence_after_ms,
            emotion=EmotionConfig(
                mode=lm.emotion.mode, audio_path=lm.emotion.audio_path,
                vector=lm.emotion.vector, weight=lm.emotion.weight,
                text=lm.emotion.text, random=lm.emotion.random,
            ),
        ))
    podcast_req = PodcastRequest(
        lines=lines,
        voices=req.voices,
        silence=SilenceConfig(
            within_segment=req.silence.within_segment,
            between_lines=req.silence.between_lines,
            speaker_switch=req.silence.speaker_switch,
        ),
        params=GenerationParams(
            speed=req.params.speed,
            speaker_speeds=req.params.speaker_speeds,
            max_text_tokens_per_segment=req.params.max_text_tokens_per_segment,
            do_sample=req.params.do_sample, top_p=req.params.top_p,
            top_k=req.params.top_k, temperature=req.params.temperature,
            length_penalty=req.params.length_penalty, num_beams=req.params.num_beams,
            repetition_penalty=req.params.repetition_penalty,
            max_mel_tokens=req.params.max_mel_tokens,
            infer_concurrency=req.params.infer_concurrency,
        ),
    )

    with _model_lock:
        try:
            task.status = "running"
            result = synthesize_podcast(
                tts, podcast_req, str(OUTPUT_DIR),
                progress_callback=_make_progress_callback(task),
            )
            task.output_path = result.output_path
            task.duration_sec = result.duration_sec
            task.progress = 100
            task.status = "completed"
            logger.info(
                "[podcast] completed task_id=%s output=%s duration=%.2f",
                task_id, result.output_path, result.duration_sec,
            )
            task.message = f"合成完成，时长 {result.duration_sec:.1f} 秒"
            task.completed_at = datetime.now().isoformat()
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            task.message = f"合成失败: {e}"
            logger.exception("[podcast] failed task_id=%s error=%s", task_id, e)
            task.completed_at = datetime.now().isoformat()


@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    """查询任务状态。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task.to_dict()


@app.get("/api/task/{task_id}/audio")
def get_task_audio(task_id: str):
    """下载任务生成的音频。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status != "completed" or not task.output_path:
        raise HTTPException(400, f"任务未完成或无音频 (status={task.status})")
    if not os.path.exists(task.output_path):
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(
        task.output_path,
        media_type="audio/wav",
        filename=Path(task.output_path).name,
    )


@app.get("/api/audio/{filename}")
def get_audio(filename: str):
    """按文件名获取音频：先查输出目录，再查参考音频目录。"""
    safe_name = Path(filename).name
    # 先在输出目录查找（生成的音频）
    path = OUTPUT_DIR / safe_name
    if path.exists():
        return FileResponse(path, media_type="audio/wav", filename=safe_name)
    # 再在参考音频目录查找（上传的音色）
    path = VOICES_DIR / safe_name
    if path.exists():
        return FileResponse(path, media_type="audio/wav", filename=safe_name)
    raise HTTPException(404, f"音频文件不存在: {safe_name}")


@app.delete("/api/task/{task_id}")
def delete_task(task_id: str):
    """删除任务（及其音频文件）。"""
    _warn_deprecated("DELETE /api/task/{task_id}", "无任何调用方（backend 侧已无对应路由）")
    with _tasks_lock:
        task = _tasks.pop(task_id, None)
    if task is None:
        raise HTTPException(404, "任务不存在")
    # 清理音频文件
    if task.output_path and os.path.exists(task.output_path):
        try:
            os.remove(task.output_path)
        except OSError:
            pass
    return {"deleted": task_id}


@app.get("/api/tasks")
def list_tasks(limit: int = 20):
    """列出最近的任务。"""
    with _tasks_lock:
        items = sorted(_tasks.values(), key=lambda t: t.created_at, reverse=True)[:limit]
    return {"tasks": [t.to_dict() for t in items], "count": len(items)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
