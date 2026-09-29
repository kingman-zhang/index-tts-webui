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
  - 超过引擎单次上限的长文本先按上限切满片，再按停顿切子段
    （上限来自 engine.capabilities.max_input_chars，**不再按引擎名判断**：
    此前只有 art 被切，而 302.ai 上限 2000、SiliconFlow 上限 2048，
    两者超限直接抛错 ⇒ 同一段长文本 art 成功、302.ai 必失败）。

并发规则（2026-09-18，2026-09-29 改为读能力声明）：
  - 所有行先扁平化为段（_flatten_segments），并发度取
    engine.capabilities.max_concurrency（自建 GPU 实例声明 1，串行；
    第三方声明 None ⇒ 读 TTS_CONCURRENCY，默认 3，钳 1-8）；
  - gather 保序 → 拼接顺序与文本顺序一致；失败段自动重试一次，
    重试后仍失败则把该引擎置入冷却（避免每个新任务重复撞同一个故障引擎）。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import wave
from pathlib import Path

from . import queue_state as qs
from .config import DATA_DIR, logger
from .engines import (
    EMO_VECTOR_ORDER,
    EngineCapabilities,
    SegmentRequest,
    SynthesisCancelled,
    VoiceRef,
    effective_concurrency,
    mark_engine_failed,
    select_engine,
)
from .engines.chunker import split_for_art
from .engines.base import NonRetryableSynthesisError

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


# 引擎注册表已迁到 app/engines/factory.py（2026-09-29）：
# 它必须是**进程级单例** —— 熔断状态要跨任务存活，health TTL 与音色解析缓存
# 不能每任务重建就作废。本模块是任务级对象，不适合持有它。
# 选引擎请用 app/engines/selector.select_engine()。


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


def _flatten_segments(lines: list[dict], caps: EngineCapabilities) -> list[dict]:
    """把任务行扁平化为合成段列表（并发调度的最小单元）。

    每段：{text, emotion, gap_ms, line_idx}。行内停顿/backend 切分、
    超长行按引擎单次上限切满片、行尾 silence_after_ms 并入本行最后一段的静音，
    与旧串行版语义一致；顺序即拼接顺序（gather 保序）。

    切片依据是 `caps.max_input_chars`（引擎自己声明的能力），**不再是引擎名**。
    旧写法 `split_for_art(text) if engine_name == "indextts_art" else [text]`
    让 302.ai（上限 2000 字）与 SiliconFlow（上限 2048 字）的超长输入直接抛错
    「请走 chunker」，而上层从来不切 —— 同一段长文本 art 成功、302.ai 必失败。
    """
    entries: list[dict] = []
    limit = caps.max_input_chars
    for idx, line in enumerate(lines):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        emotion_label = _emotion_label(line)
        pieces = split_for_art(text, limit) if limit else [text]
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


# 并发度计算已上收到 engines.effective_concurrency(caps)（2026-09-29）：
# 原 _concurrency(engine_name) 靠 `engine_name == "indextts_local"` 判断自建引擎串行，
# 既是引擎名硬编码、又被 podcast_runner 反向 import。现在读能力声明 max_concurrency。


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


async def run_mono_task(task: dict, lines: list | None = None) -> None:
    """执行配音任务：分段 → 并发合成（第三方 API）/串行（自建）→ 拼接 → 落盘。

    并发说明：TTS_CONCURRENCY（默认 3）只作用于第三方 API 引擎；自建 GPU
    引擎恒为串行。失败段自动重试一次（只重试失败段，不整任务重来）；
    重试仍失败时抛出首个原始异常（保留 httpx 错误类型供 queue_worker
    正确归类 INTERRUPTED/FAILED）。

    lines：送进合成的文本（queue_worker 传术语替换后的副本）。缺省读
    task["lines"]；无论哪种来源，本函数都不改写 task["lines"]——任务详情与
    存档保持用户原文。
    """
    task_id = task["id"]
    if lines is None:
        lines = task.get("lines") or []
    params = task.get("params") or {}
    speaker_speeds = params.get("speaker_speeds") or {}
    speed = float(speaker_speeds.get("A") or params.get("speed") or 1.0)
    voice_path = (task.get("voices") or {}).get("A")
    if not voice_path:
        raise ValueError("配音任务缺少音色参考音频")
    if not lines:
        raise ValueError("配音任务没有文本段")

    # 引擎/资源池：生产路径下一定是资源池门面（池内按段挑资源、跨任务共享并发）
    engine = await select_engine()
    caps = engine.capabilities
    logger.info("[mono] task=%s engine=%s lines=%d speed=%.2f", task_id, engine.name, len(lines), speed)
    # 语速由资源侧应用一次（原生参数，或池对不支持原生变速的资源用 atempo 补齐）。
    # 这里只在「既非原生、也没有下层保障」时告警——mono 路径不做后处理，
    # 那种情况下语速会静默丢掉，必须让人看得见。
    if abs(speed - 1.0) >= 1e-3 and not (caps.supports_speed or caps.speed_guaranteed):
        logger.warning("[mono] task=%s 该引擎不支持语速且无底层保障，speed=%.2f 不会生效", task_id, speed)
    voice = VoiceRef(tts_path=voice_path, local_path=_resolve_local_voice(voice_path), display_name=Path(voice_path).name)

    entries = _flatten_segments(lines, caps)
    if not entries:
        raise ValueError("所有文本段均为空")
    total = len(entries)
    conc = effective_concurrency(caps)
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
            # should_cancel 让资源池在「排队等待中 / 已拿到租约但尚未提交」时也能
            # 发现取消，直接放弃该段而不是照常提交给平台（提交即产生费用）。
            audio = await engine.synthesize_segment(
                SegmentRequest(text=entry["text"], voice=voice, emotion_label=entry["emotion"],
                               speed=speed, should_cancel=lambda: bool(task.get("cancel_requested")))
            )
        state["done"] += 1
        _update_progress()
        return audio

    async def _run_batch(batch: list[dict]) -> list:
        return await asyncio.gather(*[_worker(e) for e in batch], return_exceptions=True)

    def _failed(results: list) -> list[int]:
        # 不可重试的失败（可能已计费）与主动取消都不重试：
        # NonRetryableSynthesisError 重试会重复扣平台费用，SynthesisCancelled 是用户主动放弃。
        return [i for i, r in enumerate(results) if isinstance(r, Exception)
                and not isinstance(r, (NonRetryableSynthesisError, SynthesisCancelled, _TaskCancelled))]

    results = await _run_batch(entries)

    # 无条件检查取消：即使所有分段都在标记设置前提交（短任务），停止也必须生效
    if task.get("cancel_requested"):
        task["status"] = qs.QueueTaskStatus.CANCELLED
        task["message"] = "已取消"
        qs.persist_task(task_id)
        return

    # 失败段重试一次（只重试失败段；失败段本就未计入 done，重试成功后自动补上）
    retry_idx = _failed(results)
    if retry_idx:
        for i in retry_idx:
            logger.warning("[mono] task=%s 段 %d/%d 首次合成失败，重试: %s", task_id, i + 1, total, results[i])
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
        first = errors[0]
        logger.error("[mono] task=%s %d/%d 段重试后仍失败", task_id, len(errors), total)
        # 判为引擎级故障的判据刻意收窄：全部失败，或 3 段以上且过半失败。
        # 单段失败更可能是该段文本自身的问题（引擎拒绝/转写异常），
        # 把它当引擎故障会让整个引擎白冷却 120 秒 —— 误伤比漏判更贵。
        if len(errors) == total or (len(errors) >= 3 and len(errors) * 2 >= total):
            mark_engine_failed(engine.name)
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
