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

并发规则（2026-09-18）：
  - 所有行先扁平化为段（_flatten_segments），第三方 API 引擎按
    TTS_CONCURRENCY（默认 3，钳 1-8）并发合成，自建 GPU 引擎恒串行；
  - gather 保序 → 拼接顺序与文本顺序一致；失败段自动重试一次。
"""

from __future__ import annotations

import asyncio
import io
import os
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


class _TaskCancelled(Exception):
    """用户取消（在并发 worker 内抛出，由 run_mono_task 汇总处理）。"""


def _flatten_segments(lines: list[dict], engine_name: str) -> list[dict]:
    """把任务行扁平化为合成段列表（并发调度的最小单元）。

    每段：{text, emotion, gap_ms, line_idx}。行内停顿/backend 切分、
    art 满片切分、行尾 silence_after_ms 并入本行最后一段的静音，
    与旧串行版语义一致；顺序即拼接顺序（gather 保序）。
    """
    entries: list[dict] = []
    for idx, line in enumerate(lines):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        emotion_label = _emotion_label(line)
        # autodl.art 按次计费且单次 ≤2048 字符：切满片省费用；自建引擎整段交给模型侧分段
        pieces = split_for_art(text) if engine_name == "indextts_art" else [text]
        line_entries: list[dict] = []
        for piece in pieces:
            for sub_text, gap_ms in split_by_pauses(piece):
                line_entries.append({"text": sub_text, "emotion": emotion_label, "gap_ms": gap_ms})
        if not line_entries:
            continue
        gap_after = int(line.get("silence_after_ms") or 0)
        if gap_after > 0:
            last = line_entries[-1]
            last["gap_ms"] = min(last["gap_ms"] + gap_after, MAX_GAP_MS)
        for e in line_entries:
            e["line_idx"] = idx
        entries.extend(line_entries)
    return entries


def _concurrency(engine_name: str) -> int:
    """并发度：自建 GPU 引擎恒为 1；第三方 API 读 TTS_CONCURRENCY（默认 3，钳 1-8）。"""
    if engine_name == "indextts_local":
        return 1
    try:
        n = int(os.environ.get("TTS_CONCURRENCY", "3").strip())
    except ValueError:
        n = 3
    return max(1, min(n, 8))


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


def _resolve_local_voice(voice_path: str) -> str:
    """任务音色路径在 backend 侧不可读时，按文件名在本地音色目录找同名替身。

    背景：任务文件可能带着部署机路径（如 /root/autodl-tmp/...），换环境运行
    时读不到；art（base64 内联）/SiliconFlow（克隆上传）等引擎都要求本地可读。
    原路径可读时原样返回（保证 302ai 等按路径+大小+mtime 做缓存键的引擎稳定）。
    """
    import os

    p = Path(voice_path)
    if p.is_file():
        return voice_path
    search_dirs = [DATA_DIR / "preset-voices", DATA_DIR / "voices"]
    extra = os.environ.get("VOICE_FALLBACK_DIRS", "")
    search_dirs += [Path(d) for d in extra.split(os.pathsep) if d.strip()]
    for d in search_dirs:
        cand = d / p.name
        if cand.is_file():
            logger.warning("[mono] 音色路径本地不可读 %s，改用同名文件 %s", voice_path, cand)
            return str(cand)
    return voice_path  # 找不到就原样返回，让引擎给出明确报错


async def run_mono_task(task: dict) -> None:
    """执行配音任务：分段 → 并发合成（第三方 API）/串行（自建）→ 拼接 → 落盘。

    并发说明：TTS_CONCURRENCY（默认 3）只作用于第三方 API 引擎；自建 GPU
    引擎恒为串行。失败段自动重试一次（只重试失败段，不整任务重来）；
    重试仍失败时抛出首个原始异常（保留 httpx 错误类型供 queue_worker
    正确归类 INTERRUPTED/FAILED）。
    """
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
    # art 平台当前不支持情绪控制（滑杆被静默忽略，见 indextts_art.py docstring），
    # 任务带情绪标签时提前警告，避免"合成了但没情绪"的静默降级
    if engine.name == "indextts_art" and any(_emotion_label(l) for l in lines):
        logger.warning(
            "[mono] task=%s autodl.art 平台当前不支持情绪控制（滑杆被忽略），"
            "情绪标签将降级为跟随参考音频；需要情绪表达请切换 TTS_ENGINE_PREFERRED=indextts_302ai",
            task_id,
        )
    voice = VoiceRef(tts_path=voice_path, local_path=_resolve_local_voice(voice_path), display_name=Path(voice_path).name)

    entries = _flatten_segments(lines, engine.name)
    if not entries:
        raise ValueError("所有文本段均为空")
    total = len(entries)
    conc = _concurrency(engine.name)
    logger.info("[mono] task=%s engine=%s entries=%d concurrency=%d", task_id, engine.name, total, conc)

    # 行 → 段数映射（用于 current_line：行内全部段完成才计入）
    line_counts: dict[int, int] = {}
    for e in entries:
        line_counts[e["line_idx"]] = line_counts.get(e["line_idx"], 0) + 1

    sem = asyncio.Semaphore(conc)
    state = {"submitted": 0, "done": 0}

    def _update_progress() -> None:
        done = state["done"]
        task["progress"] = round(done / total, 4)
        # 已完成行数近似值：全局完成数 ≥ 某行及之前所有段数时，视为推进到该行
        current_line = 0
        cum = 0
        for line_idx in sorted(line_counts):
            cum += line_counts[line_idx]
            if done >= cum:
                current_line = line_idx + 1
            else:
                break
        task["current_line"] = current_line
        if done >= total:
            task["message"] = "拼接音频中"
        elif done > 0:
            task["message"] = f"已合成 {done}/{total} 段"
        elif state["submitted"] > 0:
            task["message"] = f"已提交 {state['submitted']}/{total} 段，等待平台合成"
        qs.persist_task(task_id)

    async def _worker(entry: dict) -> bytes:
        async with sem:
            if task.get("cancel_requested"):
                raise _TaskCancelled()
            state["submitted"] += 1
            _update_progress()
            audio = await engine.synthesize_segment(
                SegmentRequest(text=entry["text"], voice=voice, emotion_label=entry["emotion"], speed=speed)
            )
        state["done"] += 1
        _update_progress()
        return audio

    async def _run_batch(batch: list[dict]) -> list:
        return await asyncio.gather(*[_worker(e) for e in batch], return_exceptions=True)

    def _failed(results: list) -> list[int]:
        return [i for i, r in enumerate(results) if isinstance(r, BaseException)]

    results = await _run_batch(entries)

    if task.get("cancel_requested") and any(isinstance(r, _TaskCancelled) for r in results):
        task["status"] = qs.QueueTaskStatus.CANCELLED
        task["message"] = "已取消"
        qs.persist_task(task_id)
        return

    # 失败段重试一次（只重试失败段；失败段本就未计入 done，重试成功后自动补上）
    retry_idx = _failed(results)
    if retry_idx:
        for i in retry_idx:
            if isinstance(results[i], _TaskCancelled):
                continue
            logger.warning("[mono] task=%s 段 %d/%d 首次合成失败，重试: %s", task_id, i + 1, total, results[i])
        task["message"] = f"重试 {len(retry_idx)} 个失败段"
        qs.persist_task(task_id)
        retry_results = await _run_batch([entries[i] for i in retry_idx])
        for slot, i in enumerate(retry_idx):
            results[i] = retry_results[slot]

    if task.get("cancel_requested") and any(isinstance(r, _TaskCancelled) for r in results):
        task["status"] = qs.QueueTaskStatus.CANCELLED
        task["message"] = "已取消"
        qs.persist_task(task_id)
        return

    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        first = errors[0]
        logger.error("[mono] task=%s %d/%d 段重试后仍失败", task_id, len(errors), total)
        raise first  # 保留原始异常类型，queue_worker 据此归类 INTERRUPTED/FAILED

    chunks: list[bytes] = [r for r in results]
    gaps: list[int] = [e["gap_ms"] for e in entries]

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
    logger.info("[mono] completed task=%s engine=%s segments=%d concurrency=%d duration=%.1fs", task_id, engine.name, len(chunks), conc, duration)
