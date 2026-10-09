"""双人播客任务执行器：走引擎适配层逐行合成（与 mono_runner 同链路）。

背景（2026-09-20）：播客任务原先 POST 到 tts-server /api/podcast（GPU 本地），
要求 tts-server 在线。迁移后与 mono 一致：由 backend 内的引擎适配层合成
（资源池：自建优先、云端溢出，见 webui-backend/ENGINES.md），
云引擎（302.ai 等）下不再依赖本地 tts-server。

引擎差异（2026-09-29）：本模块**不判断引擎名**。切片阈值与并发度都取自
`engine.capabilities`（此前是 `engine_name == "indextts_art"` 与
`_concurrency(engine_name)` 两处硬编码）。详见 webui-backend/ENGINES.md。

与 mono_runner 的差异：
  - 多音色：voices = {A: 路径, B: 路径}，每说话人独立 VoiceRef 与语速；
  - 行间静音三类（对齐原 podcast_engine 拼接规则）：
      行级 silence_after_ms 显式给出（含 0）→ 原值；
      末行 → 0；
      说话人切换 → silence.speaker_switch；同行连续 → silence.between_lines；
    行级值并入该行最后一个子段的 gap_ms，拼接期插入；
  - 变速：**只在资源侧应用一次** —— 资源原生支持 speed 就传参生效，不支持的由
    资源池在规范化时用 atempo 补齐（见 engines.base.EngineCapabilities.speed_guaranteed）。
    本模块只在「既非原生、也没有下层保障」（裸引擎）时才补变速，避免 speed²
    （2026-09-29 修：此前对原生引擎又套了一层 atempo，加速听起来偏快）；
    ffmpeg 不可用时跳过处理（仅告警），行为与 mono 一致。
  - 响度归一：**不在这里做**（2026-10-09 上提到池门面，实现见 app/audio_norm.py）。
    所有引擎产出都经 `Engines.synthesize_segment` 回到 backend，归一在那里对
    「单人/播客 × 本地壳/云 API」四条路径统一执行一次。本模块此前那份实现
    （与 tts-server 的 6 个常量逐项相同）已删除 —— 它让归一执行者随所选资源
    漂移，且完全漏掉「单人 + 云 API」（mono_runner 后端不做后处理）。

顺序保证：段落可并发（并发度取 capabilities.max_concurrency，未声明则读
TTS_CONCURRENCY），asyncio.gather 保序返回，拼接严格按文本顺序——第 10 段先完成
也不会插到第 8 段前面。
分段文件在内存中处理，拼接后仅保留整篇 wav（outputs/podcast_{task_id}.wav）。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from . import audio_norm
from . import queue_state as qs
from .config import logger
from .engines import (
    EngineCapabilities,
    SegmentRequest,
    SynthesisCancelled,
    VoiceRef,
    atempo_filters,
    effective_concurrency,
    mark_engine_failed,
    select_engine,
)
from .mono_runner import (
    MAX_GAP_MS,
    OUTPUTS_DIR,
    _TaskCancelled,
    _concat_wavs,
    _emotion_label,
    _resolve_local_voice,
    split_by_pauses,
)
from .engines.chunker import split_for_art
from .engines.base import NonRetryableSynthesisError, find_ffmpeg, fix_wav_header

# ── 响度归一已上提到池门面（2026-10-09）──
# 常量、开关（PODCAST_NORM）与实现全部在 app/audio_norm.py —— 那是
# 「单人/播客 × 本地壳/云 API」四条合成路径的唯一执行点。本模块不再持有归一参数。

_FFMPEG_WARNED = False


def _ffmpeg_bin() -> str | None:
    """定位 ffmpeg（PATH 优先，兜底常见安装路径）。实现统一在 engines.base。

    保留本模块同名函数是刻意的：既有测试会 patch 这个名字来模拟「无 ffmpeg」。
    """
    return find_ffmpeg()


def _fix_wav_header(data: bytes) -> bytes:
    """修正 ffmpeg 管道输出的 wav 头（实现见 engines.base.fix_wav_header）。

    wave 模块按头部声明的 size 读取，坏头会导致 readframes 读错帧数、
    拼接产物错乱（与 302.ai 引擎 _repair_wav 同款问题）。按实际字节数回写。
    """
    return fix_wav_header(data)


def _apply_speed(data: bytes, speed: float = 1.0) -> bytes:
    """对单段音频变速（管道，无临时文件）。**不做响度归一**。

    响度归一已上提到池门面（app/audio_norm.py，2026-10-09），本函数只剩变速职责。
    之所以保留，是给「既非原生支持、也没有池保障」的裸引擎兜底 —— 生产路径恒为
    池门面（speed_guaranteed=True），不会走到这里。

    参数 speed 是「**本层要补的语速**」，不是用户的语速：引擎原生支持变速
    （或资源池已保证）时调用方传 1.0，此时本函数原样返回（零 ffmpeg 开销）。

    ffmpeg 不可用或处理失败时返回原始数据（不影响拼接，只记告警），
    避免音频后处理失败毁掉已合成的段落。
    """
    global _FFMPEG_WARNED
    filters = atempo_filters(speed)
    if not filters:
        # 无变速要做：一次 ffmpeg 都不跑。这里**不**统一采样率 ——
        # 资源池 normalize_pcm 已把每段统一成 24kHz/单声道/PCM16。
        return data

    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        if not _FFMPEG_WARNED:
            logger.warning("[podcast] 未找到 ffmpeg，跳过变速（语速可能不生效）")
            _FFMPEG_WARNED = True
        return data

    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-filter:a", ",".join(filters), "-ar", "24000", "-f", "wav", "pipe:1"],
            input=data, capture_output=True,
        )
    except OSError as e:
        logger.warning("[podcast] ffmpeg 调用失败，返回原始音频: %s", e)
        return data
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", "ignore")[-300:]
        logger.warning("[podcast] ffmpeg 变速失败，返回原始音频: %s", stderr)
        return data
    return _fix_wav_header(result.stdout)


def _flatten_podcast_segments(lines: list[dict], caps: EngineCapabilities, silence: dict) -> list[dict]:
    """把播客行扁平化为合成段（并发最小单元），行间静音并入行尾子段。

    每段：{text, emotion, gap_ms, line_idx, speaker}。顺序即拼接顺序。
    行间静音规则（对齐原 podcast_engine，见模块 docstring）：
      显式 silence_after_ms（含 0）> 末行 0 > 说话人切换 > 同行连续。

    切片依据是 caps.max_input_chars（引擎能力），与 mono 路径同源 ——
    此前用 `engine_name == "indextts_art"` 判断，只切了 art。
    """
    entries: list[dict] = []
    prev_speaker: str | None = None
    last_idx = len(lines) - 1
    limit = caps.max_input_chars
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
        pieces = split_for_art(text, limit) if limit else [text]
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


async def run_podcast_task(task: dict, lines: list | None = None) -> None:
    """执行双人播客任务：分段 → 并发合成 → 段级变速/归一 → 按序拼接 → 落盘。

    lines：送进合成的文本（queue_worker 传术语替换后的副本）。缺省读
    task["lines"]；无论哪种来源，本函数都不改写 task["lines"]——任务详情与
    存档保持用户原文。
    """
    task_id = task["id"]
    if lines is None:
        lines = task.get("lines") or []
    params = task.get("params") or {}
    silence = task.get("silence") or {}
    voices_cfg = task.get("voices") or {}
    speaker_speeds = params.get("speaker_speeds") or {}
    if not lines:
        raise ValueError("播客任务没有文本段")
    if not voices_cfg:
        raise ValueError("播客任务缺少音色配置")

    # 引擎/资源池：生产路径下一定是资源池门面（池内按段挑资源、跨任务共享并发）。
    # 不按引擎名分支，一切差异看 capabilities。
    engine = await select_engine()
    # 先探一次活再读能力：本地资源的「响度归一」等能力是**服务自述**的
    # （IndexttsLocalEngine 从 /api/health 读），不探就只能拿到保守值 False
    # ⇒ 上层照旧归一（不会错，但白跑一次 ffmpeg）。池内探活带 TTL 缓存
    # （默认 15s），任务连发时通常零成本；TTS 真离线时这里返回 False，
    # 后续 synthesize 会照常报错 —— 行为不变。
    await engine.health()
    caps = engine.capabilities
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
            owner_id=task.get("member_id"),
        )
        speeds[spk] = float(speaker_speeds.get(spk) or params.get("speed") or 1.0)

    # 语速只应用一次：资源原生支持（或池已保证）时，本层补 1.0（= 不动）。
    # 混池时由池按「实际服务该段的资源」决定变速，所以这里绝不能按整体能力猜。
    speed_done_above = caps.supports_speed or caps.speed_guaranteed
    post_speeds = {spk: (1.0 if speed_done_above else value) for spk, value in speeds.items()}
    logger.info("[podcast] task=%s speeds=%s 语速由%s应用", task_id, speeds,
                "资源侧（原生/池内 ffmpeg）" if speed_done_above else "本模块 ffmpeg")
    # 响度归一不在本模块做（2026-10-09 上提到池门面，实现见 app/audio_norm.py）：
    # 池在返回每段音频前已按 PODCAST_NORM 统一处理，「单人/播客 × 本地壳/云 API」
    # 四条路径都覆盖。这里只把开关状态打进日志，便于对照排查。
    logger.info("[podcast] task=%s 响度归一由资源池统一执行（PODCAST_NORM=%s）",
                task_id, audio_norm.NORM_MODE)

    entries = _flatten_podcast_segments(lines, caps, silence)
    if not entries:
        raise ValueError("所有文本段均为空")
    total = len(entries)
    conc = effective_concurrency(caps)
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
                    # 见 mono_runner 同名注释：让池在提交前就能感知取消
                    should_cancel=lambda: bool(task.get("cancel_requested")),
                )
            )
        # 段级后处理只剩变速（响度归一已由池门面完成，见 app/audio_norm.py）：
        # 只有资源侧不负责语速时（post_speeds≠1）才在本层补一次，否则原样返回。
        post_speed = post_speeds[entry["speaker"]]
        if abs(post_speed - 1.0) >= 1e-3:
            audio = await asyncio.to_thread(_apply_speed, audio, post_speed)
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
    retry_idx = [i for i, r in enumerate(results) if isinstance(r, Exception)
                 and not isinstance(r, (NonRetryableSynthesisError, SynthesisCancelled, _TaskCancelled))]
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
        # 判据与 mono 路径一致（见 mono_runner 同名注释）：全部失败，
        # 或 3 段以上且过半失败，才判为引擎级故障；单段失败不熔断，避免误伤。
        if len(errors) == total or (len(errors) >= 3 and len(errors) * 2 >= total):
            mark_engine_failed(engine.name)
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
