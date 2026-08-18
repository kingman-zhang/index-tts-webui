"""双人播客合成引擎。

逐行调用 IndexTTS2.infer() 合成每段语音，再用标准库 wave 拼接成单个 WAV。
拼接逻辑参考 indextts/cli_v2.py 的 _concatenate_wav_segments。
GPU 模型必须串行推理，避免多线程并发导致 CUDA 错误。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("podcast-engine")

# 情感向量 8 维顺序，与 IndexTTS2 normalize_emo_vec 一致
EMO_VECTOR_LABELS = ["happy", "angry", "sad", "afraid",
                     "disgusted", "melancholic", "surprised", "calm"]


@dataclass
class EmotionConfig:
    """单行情感控制配置，对应 IndexTTS2 的 4 种情感控制方式。"""
    mode: int = 0  # 0=跟随音色参考音频, 1=情感参考音频, 2=情感向量, 3=情感描述文本
    audio_path: Optional[str] = None  # mode=1 时的情感参考音频路径
    vector: list = field(default_factory=lambda: [0.0] * 8)  # mode=2 时的 8 维情感向量
    weight: float = 0.65  # 情感权重 emo_alpha
    text: Optional[str] = None  # mode=3 时的情感描述文本
    random: bool = False  # 情感随机采样

    def to_infer_kwargs(self, tts) -> dict:
        """转换为 IndexTTS2.infer() 接受的情感相关 kwargs。"""
        kwargs = {
            "emo_alpha": float(self.weight),
            "use_random": bool(self.random),
        }
        if self.mode == 0:
            # 跟随音色参考音频：不传 emo_audio_prompt（infer 内部会用 spk_audio_prompt）
            kwargs["emo_audio_prompt"] = None
            kwargs["emo_vector"] = None
            kwargs["use_emo_text"] = False
            kwargs["emo_text"] = None
        elif self.mode == 1:
            kwargs["emo_audio_prompt"] = self.audio_path
            kwargs["emo_vector"] = None
            kwargs["use_emo_text"] = False
            kwargs["emo_text"] = None
        elif self.mode == 2:
            vec = [float(v) for v in self.vector[:8]]
            while len(vec) < 8:
                vec.append(0.0)
            kwargs["emo_vector"] = tts.normalize_emo_vec(vec, apply_bias=True)
            kwargs["emo_audio_prompt"] = None
            kwargs["use_emo_text"] = False
            kwargs["emo_text"] = None
        elif self.mode == 3:
            kwargs["use_emo_text"] = True
            kwargs["emo_text"] = self.text or None
            kwargs["emo_audio_prompt"] = None
            kwargs["emo_vector"] = None
        return kwargs


@dataclass
class SilenceConfig:
    """静音配置（毫秒）。"""
    within_segment: int = 200  # 段内分句间静音（IndexTTS2 interval_silence）
    between_lines: int = 300  # 同一说话人连续发言的行间静音
    speaker_switch: int = 500  # 说话人切换时的行间静音


@dataclass
class GenerationParams:
    """GPT2 采样、分句和输出速度参数。"""
    speed: float = 1.0
    # A/B 角色独立语速；为空时回退到全局 speed。
    speaker_speeds: dict = field(default_factory=dict)
    max_text_tokens_per_segment: int = 120
    do_sample: bool = True
    top_p: float = 0.75       # 从 0.8 降到 0.75，收紧采样范围，减少异常音素
    top_k: int = 20           # 从 30 降到 20，进一步限制候选 token
    temperature: float = 0.6  # 从 0.8 降到 0.6，降低随机性，减少异常 s 音
    length_penalty: float = 0.0
    num_beams: int = 2        # 从 3 降到 2：beam search 对 TTS 质量提升有限，但慢 50%
    repetition_penalty: float = 5.0  # 从 10.0 降到 5.0，避免过度回避常见音素
    max_mel_tokens: int = 1500
    # GPU 模型只允许串行推理，固定为 1。
    infer_concurrency: int = 1

    def to_infer_kwargs(self) -> dict:
        return {
            "max_text_tokens_per_segment": int(self.max_text_tokens_per_segment),
            "do_sample": bool(self.do_sample),
            "top_p": float(self.top_p),
            "top_k": int(self.top_k) if int(self.top_k) > 0 else None,
            "temperature": float(self.temperature),
            "length_penalty": float(self.length_penalty),
            "num_beams": int(self.num_beams),
            "repetition_penalty": float(self.repetition_penalty),
            "max_mel_tokens": int(self.max_mel_tokens),
        }


@dataclass
class PodcastLine:
    """对话脚本中的一行。"""
    speaker: str  # "A" 或 "B"（或 voices 字典中的任意 key）
    text: str
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    silence_after_ms: Optional[int] = None


@dataclass
class PodcastRequest:
    """双人播客合成请求。"""
    lines: list  # list[PodcastLine]
    voices: dict  # {"A": "/path/voice_a.wav", "B": "/path/voice_b.wav"}
    silence: SilenceConfig = field(default_factory=SilenceConfig)
    params: GenerationParams = field(default_factory=GenerationParams)


@dataclass
class PodcastResult:
    """合成结果。"""
    output_path: str
    segment_paths: list  # 每段的临时路径
    duration_sec: float
    line_count: int


ProgressCallback = Callable[[int, int, str, str], None]
# (current_line, total_lines, line_text_preview, message)


def _sanitize_text(text: str) -> str:
    """清洗 BPE 无法编码的字符，避免推理阶段触发 tokenizer 错误。"""
    replacements = {
        "・": "、",
        "･": "、",
    }
    return str(text).translate(str.maketrans(replacements))


def _read_wav_format(path: str):
    """读取 WAV 文件的格式信息。"""
    with wave.open(path, "rb") as wf:
        return wf.getframerate(), wf.getnchannels(), wf.getsampwidth()


def _concatenate_wav_segments(segments, output_path: str):
    """拼接多段 WAV 为一个文件，每段后插入指定静音。

    segments: list of {"audio_path": str, "silence_after_ms": int}
    参考自 indextts/cli_v2.py _write_concat_wav。

    所有段必须使用相同的 PCM WAV 格式；变速后的段由 ffmpeg 统一输出为
    24 kHz，遇到格式不一致时立即失败，避免生成播放器无法解析的成品。
    """

    if not segments:
        raise ValueError("no segments to concatenate")

    frame_rate, channels, sample_width = _read_wav_format(segments[0]["audio_path"])
    output_path = str(output_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    for index, seg in enumerate(segments[1:], start=2):
        current_format = _read_wav_format(seg["audio_path"])
        if current_format != (frame_rate, channels, sample_width):
            raise ValueError(
                "WAV 格式不一致：第 1 段为 "
                f"{frame_rate}Hz/{channels}ch/{sample_width * 8}bit，"
                f"第 {index} 段为 "
                f"{current_format[0]}Hz/{current_format[1]}ch/{current_format[2] * 8}bit"
            )

    # 先写临时文件再原子替换，避免拼接失败产生半成品
    tmp_path = output_path + ".tmp"
    with wave.open(tmp_path, "wb") as out_wav:
        out_wav.setnchannels(channels)
        out_wav.setsampwidth(sample_width)
        out_wav.setframerate(frame_rate)
        for seg in segments:
            with wave.open(seg["audio_path"], "rb") as in_wav:
                out_wav.writeframes(in_wav.readframes(in_wav.getnframes()))
            silence_frames = frame_rate * seg["silence_after_ms"] // 1000
            if silence_frames > 0:
                out_wav.writeframes(b"\0" * channels * sample_width * silence_frames)

    os.replace(tmp_path, output_path)


def get_wav_duration(path: str) -> float:
    """获取 WAV 文件时长（秒）。"""
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def _apply_speed(path: str, speed: float) -> None:
    """使用 ffmpeg 调整 WAV 速度并归一化音量；1.0 速度也做归一化防破音。"""
    speed = max(0.5, min(2.0, float(speed)))
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("音频处理需要 TTS 服务器安装 ffmpeg")

    tmp = f"{path}.proc.tmp.wav"
    # 组合 atempo + loudnorm：先变速，再归一化到 -16 LUFS、峰值不超过 -1.5dB
    # 即使 speed=1.0 也执行 loudnorm，防止 IndexTTS2 输出的峰值过高导致破音。
    filters = []
    if abs(speed - 1.0) >= 0.001:
        filters.append(f"atempo={speed:g}")
    # loudnorm: I=-16（目标响度）TP=-1.5（真峰值上限）LRA=11（动态范围）
    filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    filter_str = ",".join(filters)

    result = subprocess.run(
        [ffmpeg, "-y", "-i", path, "-filter:a", filter_str, "-ar", "24000", tmp],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise RuntimeError(f"音频处理失败: {result.stderr[-500:]}")
    os.replace(tmp, path)


def synthesize_podcast(
    tts,
    request: PodcastRequest,
    output_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> PodcastResult:
    """执行双人播客合成。

    逐行调用 tts.infer() 生成每段 WAV，再拼接成最终音频。
    tts: IndexTTS2 实例
    request: PodcastRequest 合成请求
    output_dir: 输出目录
    progress_callback: 进度回调
    """
    lines = request.lines
    total = len(lines)
    if total == 0:
        raise ValueError("对话脚本为空")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 创建临时目录存放每段音频
    temp_dir = Path(tempfile.mkdtemp(prefix="podcast_", dir=str(output_dir)))
    segments = []
    prev_speaker = None

    try:
        # 1. 准备所有段的推理参数（CPU 密集，准备阶段可与 GPU 重叠）
        job_specs = []  # (idx, line, segment_path, infer_kwargs, speed)
        prev_speaker_check = None
        for idx, line in enumerate(lines, start=1):
            voice_path = request.voices.get(line.speaker)
            if not voice_path or not os.path.exists(voice_path):
                raise FileNotFoundError(
                    f"说话人 {line.speaker} 的参考音频不存在: {voice_path}"
                )

            segment_path = str(temp_dir / f"{idx:04d}.wav")
            infer_kwargs = {
                "spk_audio_prompt": voice_path,
                "text": _sanitize_text(line.text),
                "output_path": segment_path,
                "interval_silence": int(request.silence.within_segment),
                "verbose": False,
            }
            infer_kwargs.update(line.emotion.to_infer_kwargs(tts))
            infer_kwargs.update(request.params.to_infer_kwargs())

            speaker_speeds = getattr(request.params, "speaker_speeds", {})
            speed = speaker_speeds.get(line.speaker, request.params.speed)

            job_specs.append({
                "idx": idx,
                "line": line,
                "segment_path": segment_path,
                "infer_kwargs": infer_kwargs,
                "speed": speed,
                "prev_speaker": prev_speaker_check,
            })
            prev_speaker_check = line.speaker

        # GPU 模型不支持多线程并发，始终串行执行推理。
        concurrency = 1
        if progress_callback:
            progress_callback(0, total, "", "准备串行推理...")

        # 2. 串行推理
        completed_segments = [None] * total

        def _run_one(job: dict) -> dict:
            """单个段的完整推理+后处理。"""
            idx = job["idx"]
            t_infer_start = time.time()
            tts.infer(**job["infer_kwargs"])
            t_infer_end = time.time()
            _apply_speed(job["segment_path"], job["speed"])
            t_end = time.time()
            logger.info(
                "[podcast] line %d/%d speaker=%s infer=%.2fs total=%.2fs",
                idx, total, job["line"].speaker,
                t_infer_end - t_infer_start,
                t_end - t_infer_start,
            )
            return {
                "idx": idx,
                "audio_path": job["segment_path"],
                "speed": job["speed"],
                "prev_speaker": job["prev_speaker"],
                "line": job["line"],
            }

        for job in job_specs:
            result = _run_one(job)
            completed_segments[result["idx"] - 1] = result
            if progress_callback:
                preview = (result["line"].text[:20] + "...") if len(result["line"].text) > 20 else result["line"].text
                progress_callback(
                    result["idx"], total, preview,
                    f"完成 {result['idx']}/{total}（{result['line'].speaker}）"
                )

        # 3. 组装段信息和静音（按 idx 顺序）
        prev_speaker = None
        for result in completed_segments:
            line = result["line"]
            if prev_speaker is not None and prev_speaker != line.speaker:
                silence_ms = request.silence.speaker_switch
            else:
                silence_ms = request.silence.between_lines
            if line.silence_after_ms is not None:
                silence_ms = max(0, int(line.silence_after_ms))
            elif result["idx"] == total:
                silence_ms = 0
            segments.append({
                "audio_path": result["audio_path"],
                "silence_after_ms": silence_ms,
            })
            prev_speaker = line.speaker

        if progress_callback:
            progress_callback(total, total, "", "正在拼接音频...")

        t_concat_start = time.time()
        final_path = str(output_dir / f"podcast_{int(time.time())}.wav")
        _concatenate_wav_segments(segments, final_path)
        t_concat_end = time.time()
        logger.info(
            "[podcast] concatenation done segments=%d time=%.2fs output=%s",
            len(segments), t_concat_end - t_concat_start, final_path,
        )

        duration = get_wav_duration(final_path)
        if progress_callback:
            progress_callback(total, total, "", f"合成完成，时长 {duration:.1f} 秒")
        logger.info(
            "[podcast] total lines=%d duration=%.1fs final=%s",
            total, duration, final_path,
        )

        return PodcastResult(
            output_path=final_path,
            segment_paths=[s["audio_path"] for s in segments],
            duration_sec=duration,
            line_count=total,
        )
    except Exception:
        # 合成失败时清理临时目录
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    else:
        # 成功后也清理临时段文件（保留最终成品）
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
