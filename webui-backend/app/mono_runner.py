"""单音色配音任务执行器（G1）：走引擎适配层逐段合成，backend 侧拼接落盘。

与播客路径的区别：不经过 tts-server /api/podcast 的播客引擎，
而是用 engines/ 适配层（自建优先，autodl.art 自动兜底），
为后续接 MiniMax/CosyVoice 等商用 API 预留同一条链路。

任务数据约定（复用 /api/queue/submit，kind="mono"）：
  lines:  [{speaker: "A", text, emotion: {label} | null, silence_after_ms?}]
  voices: {"A": 参考音频路径}
  params: {speed / speaker_speeds: {A: x}}
情绪标签为统一 8 标签（EMO_VECTOR_ORDER）+ neutral；缺省/None = 跟随音色。

分段规则（与前端配音画布所见即所得一致）：
  - 行内停顿 [pause:秒] / <#> 由 backend 的 split_by_pauses 切分并在拼接时
    插入精确静音，不依赖 tts-server 的行内停顿实现；
  - autodl.art 引擎先按 2048 字符切满片（chunker.split_for_art），
    再按停顿切子段（注意：停顿切分会增加 art 的按次提交数）。
"""

from __future__ import annotations

import io
import re
import wave
from pathlib import Path

from . import queue_state as qs
from .config import DATA_DIR, TTS_URL, http_client, logger
from .engines import (
    EMO_VECTOR_ORDER,
    EngineRegistry,
    Indextts302aiEngine,
    IndexttsArtEngine,
    IndexttsLocalEngine,
    IndexttsSiliconflowEngine,
    SegmentRequest,
    VoiceRef,
)
from .engines.chunker import split_for_art

OUTPUTS_DIR = DATA_DIR / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

MAX_GAP_MS = 10000  # 单段尾部静音上限，与 tts-server [pause] 钳制一致
MIN_GAP_MS = 50     # 单个停顿下限，与前端编辑器 0.05s 钳制一致

# 行内停顿标记：[pause:秒]（支持两位小数）与 <#>（固定 0.5 秒）
PAUSE_TOKEN_RE = re.compile(r"\[pause:\s*([\d.]+)\s*\]|<#>")


def split_by_pauses(text: str) -> list[tuple[str, int]]:
    """按行内停顿标记把合成段切成子段，返回 [(子段文本, 段后静音ms)]。

    与前端配音画布的所见即所得模型对齐：停顿不再交给引擎侧处理
    （自建引擎依赖 tts-server 的 split_pauses、autodl.art 只能降级成逗号），
    而是 backend 精确切分并在拼接时插入静音——两个引擎行为一致，
    且 tts-server 只需最基础的 /api/synthesize 即可。

    规则：
      - 停顿属于其左侧文字的尾随静音；相邻停顿叠加（连插两个芯片即相加），
        整体钳制 MAX_GAP_MS；
      - 行首停顿无法前置（backend 只能做段间静音），顺延为首段尾随静音；
      - 单个停顿钳制 MIN_GAP_MS–MAX_GAP_MS。
    """
    # 先切成 (文字, 其后紧邻停顿ms) 的 token 序列
    tokens: list[tuple[str, int]] = []
    last = 0
    for m in PAUSE_TOKEN_RE.finditer(text):
        raw = m.group(1)
        try:
            sec = float(raw) if raw else 0.5
        except ValueError:
            sec = 0.5
        gap = min(max(int(round(sec * 1000)), MIN_GAP_MS), MAX_GAP_MS)
        tokens.append((text[last:m.start()], gap))
        last = m.end()
    tokens.append((text[last:], 0))

    pieces: list[list] = []  # [子段文本, 段后静音ms]
    pending = 0  # 距上一个非空子段以来累积的停顿
    for seg, gap in tokens:
        if seg.strip():
            if pieces:
                # 之前累积的停顿位于上一个子段与本段之间 → 归入上一个子段尾部
                pieces[-1][1] = min(pieces[-1][1] + pending, MAX_GAP_MS)
                pending = gap
            else:
                # 首个子段：行首停顿无法前置，顺延为其尾随静音（保留总时长）
                pending = min(pending + gap, MAX_GAP_MS)
            pieces.append([seg.strip(), 0])
        else:
            pending = min(pending + gap, MAX_GAP_MS)
    # 段尾剩余停顿并入最后子段
    if pieces and pending:
        pieces[-1][1] = min(pieces[-1][1] + pending, MAX_GAP_MS)
    return [(seg_text, gap) for seg_text, gap in pieces]


def build_registry() -> EngineRegistry:
    """配音模式引擎注册表，默认顺序：自建 → 302.ai → SiliconFlow → autodl.art。

    有 Key/Token 才注册对应引擎。TTS_ENGINE_PREFERRED 可调整优先级：
    逗号分隔的引擎名（indextts_local / indextts_302ai / indextts_siliconflow /
    indextts_art），列出的引擎按给定顺序排到最前，未列出的保持原相对顺序排在
    其后；resolve() 依序探活——排最前的不可用时自动落到后续引擎。
    例：TTS_ENGINE_PREFERRED=indextts_art
    """
    import logging
    import os

    registry = EngineRegistry()
    registry.register(IndexttsLocalEngine(TTS_URL, http_client))
    if os.environ.get("INDEXTTS302_API_KEY"):
        registry.register(
            Indextts302aiEngine(cache_path=DATA_DIR / "ai302_voices.json")
        )
    if os.environ.get("SILICONFLOW_API_KEY"):
        # 默认国内站（CosyVoice2）；要接国际站 IndexTTS-2 时：
        #   SILICONFLOW_BASE_URL=https://api.siliconflow.com/v1
        #   SILICONFLOW_MODEL=IndexTeam/IndexTTS-2
        #   （换国际站 Key，国内/国际 Key 不互通）
        registry.register(
            IndexttsSiliconflowEngine(
                base_url=os.environ.get("SILICONFLOW_BASE_URL")
                or "https://api.siliconflow.cn/v1",
                cache_path=DATA_DIR / "siliconflow_voices.json",
            )
        )
    registry.register(IndexttsArtEngine())  # token 从 AUTODL_API_TOKEN 读取

    preferred = os.environ.get("TTS_ENGINE_PREFERRED", "").strip()
    if preferred:
        log = logging.getLogger(__name__)
        by_name = {e.name: e for e in registry.engines}
        head: list = []
        for n in (s.strip() for s in preferred.split(",")):
            if not n:
                continue
            if n in by_name:
                head.append(by_name.pop(n))
            else:
                log.warning("TTS_ENGINE_PREFERRED 含未注册引擎 %r（缺 Key 或名字写错），已忽略", n)
        registry.engines = head + list(by_name.values())
    return registry


def _emotion_label(line: dict) -> str | None:
    """行情感 → 统一标签。显式 label 优先；兼容向量模式（取最强维）。"""
    emo = line.get("emotion") if isinstance(line, dict) else None
    if not isinstance(emo, dict):
        return None
    label = emo.get("label")
    if isinstance(label, str) and (label in EMO_VECTOR_ORDER or label == "neutral"):
        return label
    vector = emo.get("vector")
    if isinstance(vector, list) and len(vector) == 8:
        peak = max(vector)
        if isinstance(peak, (int, float)) and peak > 0:
            return EMO_VECTOR_ORDER[vector.index(peak)]
    return None


def _concat_wavs(chunks: list[bytes], gaps_ms: list[int]) -> tuple[bytes, float]:
    """拼接 wav 字节；gaps_ms[i] 为第 i 段之后的静音毫秒。返回 (wav_bytes, 总时长秒)。

    用 wave 模块直拼（无 ffmpeg 依赖）；各段音频参数必须一致——
    任务开始时固定单一引擎，正常情况下不会混流。
    """
    if not chunks:
        raise ValueError("没有可拼接的音频段")
    framerate = sampwidth = channels = 0
    frames: list[bytes] = []
    for i, data in enumerate(chunks):
        with wave.open(io.BytesIO(data), "rb") as r:
            if not frames:
                framerate, sampwidth, channels = r.getframerate(), r.getsampwidth(), r.getnchannels()
            elif (r.getframerate(), r.getsampwidth(), r.getnchannels()) != (framerate, sampwidth, channels):
                raise ValueError(
                    f"第 {i + 1} 段音频参数与前段不一致（引擎混流或格式变化），请重试"
                )
            frames.append(r.readframes(r.getnframes()))

    frame_bytes = sampwidth * channels
    out = io.BytesIO()
    total_frames = 0
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        for i, fr in enumerate(frames):
            w.writeframes(fr)
            total_frames += len(fr) // frame_bytes
            gap_ms = gaps_ms[i] if i < len(gaps_ms) else 0
            if gap_ms > 0:
                gap_frames = int(framerate * gap_ms / 1000)
                w.writeframes(b"\x00" * (gap_frames * frame_bytes))
                total_frames += gap_frames
    return out.getvalue(), total_frames / framerate


async def run_mono_task(task: dict) -> None:
    """执行配音任务：逐段合成 → 拼接 → 落盘。异常向上抛由 process_queue 收尾。"""
    task_id = task["id"]
    lines = task.get("lines") or []
    params = task.get("params") or {}
    speaker_speeds = params.get("speaker_speeds") or {}
    speed = float(speaker_speeds.get("A") or params.get("speed") or 1.0)
    voice_path = (task.get("voices") or {}).get("A")
    if not voice_path:
        raise ValueError("配音任务缺少音色参考音频")
    if not lines:
        raise ValueError("配音任务没有文本段")

    # 任务内固定单一引擎，避免中途故障切换导致音频参数不一致
    engine = await build_registry().resolve()
    logger.info("[mono] task=%s engine=%s lines=%d speed=%.2f", task_id, engine.name, len(lines), speed)
    voice = VoiceRef(tts_path=voice_path, local_path=voice_path, display_name=Path(voice_path).name)

    total = len(lines)
    chunks: list[bytes] = []
    gaps: list[int] = []
    for idx, line in enumerate(lines):
        if task.get("cancel_requested"):
            task["status"] = qs.QueueTaskStatus.CANCELLED
            task["message"] = "已取消"
            qs.persist_task(task_id)
            return
        text = (line.get("text") or "").strip()
        if not text:
            continue
        emotion_label = _emotion_label(line)
        # autodl.art 按次计费且单次 ≤2048 字符：切满片省费用；自建引擎整段交给模型侧分段
        pieces = split_for_art(text) if engine.name == "indextts_art" else [text]
        for piece in pieces:
            # 行内停顿由 backend 统一切分并插静音（与前端画布所见即所得一致，
            # 不依赖 tts-server 的行内停顿实现）
            for sub_text, gap_ms in split_by_pauses(piece):
                audio = await engine.synthesize_segment(
                    SegmentRequest(text=sub_text, voice=voice, emotion_label=emotion_label, speed=speed)
                )
                chunks.append(audio)
                gaps.append(gap_ms)
        gap_ms = int(line.get("silence_after_ms") or 0)
        if gap_ms > 0:
            gaps[-1] = min(gaps[-1] + gap_ms, MAX_GAP_MS) if gaps else min(gap_ms, MAX_GAP_MS)
        task["progress"] = round((idx + 1) / total, 4)
        task["current_line"] = idx + 1
        task["message"] = f"已合成 {idx + 1}/{total} 段"
        qs.persist_task(task_id)

    if not chunks:
        raise ValueError("所有文本段均为空")

    task["message"] = "拼接音频中"
    qs.persist_task(task_id)
    wav_bytes, duration = _concat_wavs(chunks, gaps)
    out_path = OUTPUTS_DIR / f"mono_{task_id}.wav"
    out_path.write_bytes(wav_bytes)

    task["status"] = qs.QueueTaskStatus.SUCCESS
    task["progress"] = 1.0
    task["audio_url"] = f"/api/mono/audio/{task_id}"
    task["output_path"] = str(out_path)
    task["duration_sec"] = round(duration, 2)
    task["message"] = "合成完成"
    task["engine"] = engine.name
    logger.info("[mono] completed task=%s engine=%s segments=%d duration=%.1fs", task_id, engine.name, len(chunks), duration)
