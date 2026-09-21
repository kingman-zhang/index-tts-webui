"""双人播客任务执行器：走引擎适配层逐行合成（与 mono_runner 同链路）。

背景（2026-09-20）：播客任务原先 POST 到 tts-server /api/podcast（GPU 本地），
要求 tts-server 在线。迁移后与 mono 一致：由 backend 内的引擎适配层合成
（自建 → 302.ai → SiliconFlow → autodl.art，TTS_ENGINE_PREFERRED 调序），
云引擎（302.ai 等）下不再依赖本地 tts-server。

与 mono_runner 的差异：
  - 多音色：voices = {A: 路径, B: 路径}，每说话人独立 VoiceRef 与语速；
  - 行间静音三类（对齐原 podcast_engine 拼接规则）：
      行级 silence_after_ms 显式给出（含 0）→ 原值；
      末行 → 0；
      说话人切换 → silence.speaker_switch；同行连续 → silence.between_lines；
    行级值并入该行最后一个子段的 gap_ms，拼接期插入；
  - 变速与响度归一：原引擎每行/子段独立 _apply_speed（ebur128 → 固定增益 →
    alimiter，目标 -16 LUFS，峰值顶 -1.5dBFS，输出 24kHz）。302.ai 等云引擎
    不支持 speed 参数，由本模块用 ffmpeg 管道逐段补齐（atempo + 归一 +
    统一重采样 24kHz）；ffmpeg 不可用时跳过处理（仅告警），行为与 mono 一致。

顺序保证：段落可并发（TTS_CONCURRENCY），asyncio.gather 保序返回，
拼接严格按文本顺序——第 10 段先完成也不会插到第 8 段前面。
分段文件在内存中处理，拼接后仅保留整篇 wav（outputs/podcast_{task_id}.wav）。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import wave
import io
import logging
import os
from pathlib import Path

from . import queue_state as qs
from .config import DATA_DIR, logger
from .engines import SegmentRequest, VoiceRef
from .mono_runner import (
    MAX_GAP_MS,
    OUTPUTS_DIR,
    _TaskCancelled,
    _concurrency,
    _concat_wavs,
    _emotion_label,
    _resolve_local_voice,
    build_registry,
    split_by_pauses,
)
from .engines.chunker import split_for_art

# ─── 响度归一参数（对齐 tts-server podcast_engine.NORM_*） ──────────
NORM_TARGET_LUFS = -16.0
NORM_CEILING_DBFS = -1.5
NORM_LIMITER_MARGIN_DB = 0.5
NORM_MAX_GAIN_DB = 24.0
NORM_ABNORMAL_GAIN_DB = 12.0
NORM_SAMPLE_RATE = 24000  # 与原播客引擎输出一致（-ar 24000）

_FFMPEG_WARNED = False


def _ffmpeg_bin() -> str | None:
    """定位 ffmpeg：PATH 优先，兜底常见安装路径（macOS homebrew 等）。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(cand).is_file():
            return cand
    return None


def _measure_loudness(ffmpeg: str, data: bytes) -> float | None:
    """ebur128 量积分响度（LUFS），测不出返回 None。"""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", "pipe:0",
             "-af", "ebur128=peak=true", "-f", "null", "-"],
            input=data, capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    lufs = None
    for line in result.stderr.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if line.startswith("I:"):
            value = line.split()[1] if len(line.split()) > 1 else ""
            if value == "-inf":
                continue
            try:
                lufs = float(value)
            except ValueError:
                continue
            break
    return lufs


def _fix_wav_header(data: bytes) -> bytes:
    """修正 ffmpeg 管道输出的 wav 头：data/RIFF size 是流式占位（0xFFFFFFFF 或 0）。

    wave 模块按头部声明的 size 读取，坏头会导致 readframes 读错帧数、
    拼接产物错乱（与 302.ai 引擎 _repair_wav 同款问题）。按实际字节数回写。
    """
    if len(data) < 44 or data[:4] != b"RIFF":
        return data
    b = bytearray(data)
    pos = 12
    while pos + 8 <= len(b):
        cid = bytes(b[pos:pos + 4])
        size = int.from_bytes(b[pos + 4:pos + 8], "little")
        remaining = len(b) - pos - 8
        if size > remaining:  # 占位/坏 size：按实际剩余字节数修正
            size = remaining
            b[pos + 4:pos + 8] = size.to_bytes(4, "little")
        if cid == b"data":
            b[4:8] = (len(b) - 8).to_bytes(4, "little")
            return bytes(b)
        pos += 8 + size + (size & 1)  # chunk 按 2 字节对齐
    return data


def _normalize_segment(data: bytes, speed: float) -> bytes:
    """对单段音频做变速 + 响度归一（管道，无临时文件）。

    对齐原 podcast_engine._apply_speed：atempo 变速 → ebur128 测量 →
    固定增益 → alimiter 限幅兜底 → 统一 24kHz。
    ffmpeg 不可用或处理失败时返回原始数据（不影响拼接，只记告警），
    避免音频后处理失败毁掉已合成的段落。
    """
    global _FFMPEG_WARNED
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        if not _FFMPEG_WARNED:
            logger.warning("[podcast] 未找到 ffmpeg，跳过变速/响度归一（音色间响度可能不齐）")
            _FFMPEG_WARNED = True
        return data

    speed = max(0.5, min(2.0, float(speed)))
    filters: list[str] = []
    if abs(speed - 1.0) >= 0.001:
        filters.append(f"atempo={speed:g}")

    lufs = _measure_loudness(ffmpeg, data)
    if lufs is None:
        # 与原引擎一致：测不出响度时退回 loudnorm（可能过短或全静音）
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    else:
        gain = NORM_TARGET_LUFS - lufs
        if gain > NORM_ABNORMAL_GAIN_DB:
            logger.warning("[podcast] 段原始响度 %.1f LUFS 偏小（需提升 %.1f dB），建议核对听感", lufs, gain)
        if gain > NORM_MAX_GAIN_DB:
            gain = NORM_MAX_GAIN_DB
        limit = 10 ** ((NORM_CEILING_DBFS - NORM_LIMITER_MARGIN_DB) / 20)
        filters.append(f"volume={gain:.2f}dB")
        filters.append(f"alimiter=limit={limit:.4f}:level=disabled")

    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-filter:a", ",".join(filters), "-ar", str(NORM_SAMPLE_RATE),
             "-f", "wav", "pipe:1"],
            input=data, capture_output=True,
        )
    except OSError as e:
        logger.warning("[podcast] ffmpeg 调用失败，返回原始音频: %s", e)
        return data
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", "ignore")[-300:]
        logger.warning("[podcast] ffmpeg 归一失败，返回原始音频: %s", stderr)
        return data
    return _fix_wav_header(result.stdout)


def _flatten_podcast_segments(lines: list[dict], engine_name: str, silence: dict) -> list[dict]:
    """把播客行扁平化为合成段（并发最小单元），行间静音并入行尾子段。

    每段：{text, emotion, gap_ms, line_idx, speaker}。顺序即拼接顺序。
    行间静音规则（对齐原 podcast_engine，见模块 docstring）：
      显式 silence_after_ms（含 0）> 末行 0 > 说话人切换 > 同行连续。
    """
    entries: list[dict] = []
    prev_speaker: str | None = None
    last_idx = len(lines) - 1
    for idx, line in enumerate(lines):
        text = (line.get("text") or "").strip()
        speaker = line.get("speaker") or "A"
        if line.get("silence_after_ms") is not None:
            line_gap = max(0, int(line["silence_after_ms"]))
        elif idx == last_idx:
            line_gap = 0
        elif prev_speaker is not None and prev_speaker != speaker:
            line_gap = int(silence.get("speaker_switch") or 0)
        else:
            line_gap = int(silence.get("between_lines") or 0)
        if not text:
            continue  # 空行不合成（提交侧已校验拒绝）；不更新 prev_speaker
        prev_speaker = speaker
        emotion_label = _emotion_label(line)
        # autodl.art 按次计费且单次 ≤2048 字符：切满片省费用；其余引擎整行交引擎
        pieces = split_for_art(text) if engine_name == "indextts_art" else [text]
        line_entries: list[dict] = []
        for piece in pieces:
            for sub_text, gap_ms in split_by_pauses(piece):
                line_entries.append({"text": sub_text, "emotion": emotion_label, "gap_ms": gap_ms})
        if not line_entries:
            continue
        if line_gap > 0:
            last = line_entries[-1]
            last["gap_ms"] = min(last["gap_ms"] + line_gap, MAX_GAP_MS)
        for e in line_entries:
            e["line_idx"] = idx
            e["speaker"] = speaker
        entries.extend(line_entries)
    return entries


async def run_podcast_task(task: dict) -> None:
    """执行双人播客任务：分段 → 并发合成 → 段级变速/归一 → 按序拼接 → 落盘。"""
    task_id = task["id"]
    lines = task.get("lines") or []
    params = task.get("params") or {}
    silence = task.get("silence") or {}
    voices_cfg = task.get("voices") or {}
    speaker_speeds = params.get("speaker_speeds") or {}
    if not lines:
        raise ValueError("播客任务没有文本段")
    if not voices_cfg:
        raise ValueError("播客任务缺少音色配置")

    # 任务内固定单一引擎，避免中途故障切换导致音频参数不一致
    engine = await build_registry().resolve()
    logger.info("[podcast] task=%s engine=%s lines=%d", task_id, engine.name, len(lines))

    # 每说话人的音色引用与语速
    voice_refs: dict[str, VoiceRef] = {}
    speeds: dict[str, float] = {}
    for spk in {ln.get("speaker") or "A" for ln in lines}:
        voice_path = voices_cfg.get(spk)
        if not voice_path:
            raise FileNotFoundError(f"说话人 {spk} 的参考音频不存在")
        voice_refs[spk] = VoiceRef(
            tts_path=voice_path,
            local_path=_resolve_local_voice(voice_path),
            display_name=Path(voice_path).name,
        )
        speeds[spk] = float(speaker_speeds.get(spk) or params.get("speed") or 1.0)

    entries = _flatten_podcast_segments(lines, engine.name, silence)
    if not entries:
        raise ValueError("所有文本段均为空")
    total = len(entries)
    conc = _concurrency(engine.name)
    logger.info("[podcast] task=%s engine=%s entries=%d concurrency=%d", task_id, engine.name, total, conc)

    # 行 → 段数映射（用于 current_line：行内全部段完成才计入）
    line_counts: dict[int, int] = {}
    for e in entries:
        line_counts[e["line_idx"]] = line_counts.get(e["line_idx"], 0) + 1

    sem = asyncio.Semaphore(conc)
    state = {"submitted": 0, "done": 0}

    def _update_progress() -> None:
        done = state["done"]
        task["progress"] = round(done / total, 4)
        current_line = 0
        cum = 0
        for line_idx in sorted(line_counts):
            cum += line_counts[line_idx]
            if done >= cum:
                current_line = line_idx + 1
            else:
                break
        task["current_line"] = current_line
        if task.get("cancel_requested"):
            task["message"] = "正在取消，等待进行中的合成结束"  # 取消中不覆盖提示
        elif done >= total:
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
                SegmentRequest(
                    text=entry["text"],
                    voice=voice_refs[entry["speaker"]],
                    emotion_label=entry["emotion"],
                    speed=speeds[entry["speaker"]],
                )
            )
        # 段级变速 + 响度归一（ffmpeg 管道；不可用时原样返回）
        audio = await asyncio.to_thread(_normalize_segment, audio, speeds[entry["speaker"]])
        state["done"] += 1
        _update_progress()
        return audio

    async def _run_batch(batch: list[dict]) -> list:
        # gather 保序：结果顺序 == entries 顺序 == 文本顺序，与完成先后无关
        return await asyncio.gather(*[_worker(e) for e in batch], return_exceptions=True)

    results = await _run_batch(entries)

    # 无条件检查取消：即使所有分段都在标记设置前提交（短任务），停止也必须生效
    if task.get("cancel_requested"):
        task["status"] = qs.QueueTaskStatus.CANCELLED
        task["message"] = "已取消"
        qs.persist_task(task_id)
        return

    # 失败段重试一次（只重试失败段；成功段保留原结果不重复扣调用）
    retry_idx = [i for i, r in enumerate(results) if isinstance(r, BaseException) and not isinstance(r, _TaskCancelled)]
    if retry_idx:
        for i in retry_idx:
            logger.warning("[podcast] task=%s 段 %d/%d 首次合成失败，重试: %s", task_id, i + 1, total, results[i])
        task["message"] = f"重试 {len(retry_idx)} 个失败段"
        qs.persist_task(task_id)
        retry_results = await _run_batch([entries[i] for i in retry_idx])
        for slot, i in enumerate(retry_idx):
            results[i] = retry_results[slot]

    # 重试批次期间也可能被取消
    if task.get("cancel_requested"):
        task["status"] = qs.QueueTaskStatus.CANCELLED
        task["message"] = "已取消"
        qs.persist_task(task_id)
        return

    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        logger.error("[podcast] task=%s %d/%d 段重试后仍失败", task_id, len(errors), total)
        raise errors[0]  # 保留原始异常类型，queue_worker 据此归类 INTERRUPTED/FAILED

    chunks: list[bytes] = [r for r in results]
    gaps: list[int] = [e["gap_ms"] for e in entries]

    task["message"] = "拼接音频中"
    qs.persist_task(task_id)
    wav_bytes, duration = _concat_wavs(chunks, gaps)
    out_path = OUTPUTS_DIR / f"podcast_{task_id}.wav"
    out_path.write_bytes(wav_bytes)

    task["status"] = qs.QueueTaskStatus.SUCCESS
    task["progress"] = 1.0
    task["audio_url"] = f"/api/podcast/audio/{task_id}"
    task["output_path"] = str(out_path)
    task["duration_sec"] = round(duration, 2)
    task["message"] = "合成完成"
    task["engine"] = engine.name
    logger.info("[podcast] completed task=%s engine=%s segments=%d concurrency=%d duration=%.1fs",
                task_id, engine.name, len(chunks), conc, duration)
