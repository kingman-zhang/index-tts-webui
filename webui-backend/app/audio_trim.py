"""段级首尾静音裁剪 —— 让「行间静音」配置值真的等于实际听到的间隔。

为什么需要（2026-10-27）
========================
播客把每行单独合成后顺序拼接，`PODCAST_SILENCE_*` 只决定**段与段之间额外插入**
多少静音，但每段音频自己头尾还带着一大截空白。实测一组 7 段对话：

    段            时长     头静音    尾静音
    「嗯」        0.56s    120ms     227ms   ← 真正的人声只有 ~0.21s
    「没错」      1.43s    470ms     488ms   ← 真正的人声只有 ~0.47s
    「对」        0.43s     80ms     150ms
    长句（B）     3.9s      0~10ms     0~10ms

短文本的空白尤其离谱：「没错」这一行 1.43 秒里有 0.96 秒是空气。所以

  · 配 `between_lines=250` 实际听到的是 250 + 前段尾巴 ≈ 550~750ms，**配置值不准**；
  · 行内 `[pause:N]` 同理，段尾空白会叠加在它上面，标称值越大偏差比例越小，
    小值（0、50ms）时偏差甚至超过标称值本身。

裁掉头尾空白之后，`gap=0` 才真的是 0，静音配置值才等于听感。

实现约束
========
**只用标准库**（`wave` + `array`）。生产镜像 `requirements.txt` 里没有 numpy，
引入它等于又要 rebuild 基础镜像（见 STORAGE_MIGRATION_PLAN 那类教训）。
Python 3.13 已移除 `audioop`，所以按帧扫描用 `array('h')` + 内建 `max/min`
（C 速度），再从尾部/头部逐采样找第一个过阈值的点 —— 静音段几十个采样内就退出。

阈值取**相对峰值**而非绝对值：不同引擎、不同音色的段响度差很多，写死 -40dBFS
会让小音量段整段被判成静音。默认 -45dB（相对峰值），保留头 30ms / 尾 60ms
余量，避免削掉韵尾和气口。
"""

from __future__ import annotations

import io
import logging
import wave
from array import array

from .config import (
    PODCAST_TRIM,
    PODCAST_TRIM_KEEP_HEAD_MS,
    PODCAST_TRIM_KEEP_TAIL_MS,
    PODCAST_TRIM_THR_DB,
    logger,
)

# 裁剪后至少保留这么长，防止「整段几乎无声」被裁成空文件
_MIN_KEEP_MS = 50
# 单侧扫描上限：超过这个长度还没找到有声点，就当作整段静音（不必扫到底）
_MAX_SCAN_MS = 3000


def _voiced_index_head(samples: array, thr: int, limit: int) -> int:
    """从头找第一个 |x| > thr 的采样下标；找不到返回 len(samples)。"""
    n = min(len(samples), limit)
    for i in range(n):
        v = samples[i]
        if v > thr or -v > thr:
            return i
    return len(samples)


def _voiced_index_tail(samples: array, thr: int, limit: int) -> int:
    """从尾找最后一个 |x| > thr 的采样下标；找不到返回 -1。"""
    n = len(samples)
    stop = max(0, n - limit)
    for i in range(n - 1, stop - 1, -1):
        v = samples[i]
        if v > thr or -v > thr:
            return i
    return -1


def _peak_of(samples: array) -> int:
    """相对峰值：max/min 都是 C 实现，避免 Python 循环扫全长。"""
    if not samples:
        return 0
    hi = max(samples)
    lo = min(samples)
    return max(hi, -lo)


def edge_silence_ms(
    data: bytes,
    thr_db: float = PODCAST_TRIM_THR_DB,
) -> tuple[float, float]:
    """返回 (头部静音 ms, 尾部静音 ms)。解析失败返回 (0, 0)。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as r:
            sr = r.getframerate()
            ch = r.getnchannels()
            sw = r.getsampwidth()
            if sw != 2:
                return 0.0, 0.0
            samples = array("h")
            samples.frombytes(r.readframes(r.getnframes()))
    except (wave.Error, EOFError, OSError):
        return 0.0, 0.0
    if not samples or sr <= 0:
        return 0.0, 0.0

    peak = _peak_of(samples)
    if peak <= 0:
        ms = len(samples) / ch / sr * 1000
        return ms, ms
    thr = int(peak * (10 ** (thr_db / 20.0)))
    limit = int(sr * _MAX_SCAN_MS / 1000) * ch

    head = _voiced_index_head(samples, thr, limit)
    tail = _voiced_index_tail(samples, thr, limit)
    total = len(samples)
    head_ms = head / ch / sr * 1000
    tail_ms = 0.0 if tail < 0 else (total - 1 - tail) / ch / sr * 1000
    return head_ms, tail_ms


def trim_wav(
    data: bytes,
    *,
    thr_db: float = PODCAST_TRIM_THR_DB,
    keep_head_ms: int = PODCAST_TRIM_KEEP_HEAD_MS,
    keep_tail_ms: int = PODCAST_TRIM_KEEP_TAIL_MS,
) -> bytes:
    """裁掉首尾静音，各保留 keep_*_ms 余量。任何异常都原样返回，绝不让后处理毁掉已合成段。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as r:
            sr = r.getframerate()
            ch = r.getnchannels()
            sw = r.getsampwidth()
            raw = r.readframes(r.getnframes())
    except (wave.Error, EOFError, OSError) as e:
        logger.warning("[trim] 读取失败，原样返回: %s", e)
        return data
    if sw != 2 or ch < 1:
        return data  # 只处理 16bit PCM（池已统一成 24kHz/单声道/PCM16）

    samples = array("h")
    samples.frombytes(raw)
    if not samples:
        return data
    peak = _peak_of(samples)
    if peak <= 0:
        return data

    thr = int(peak * (10 ** (thr_db / 20.0)))
    limit = int(sr * _MAX_SCAN_MS / 1000) * ch
    head = _voiced_index_head(samples, thr, limit)
    tail = _voiced_index_tail(samples, thr, limit)
    if tail < 0:
        return data  # 全静音，不裁

    min_keep = int(sr * _MIN_KEEP_MS / 1000) * ch
    start = max(0, head - int(sr * keep_head_ms / 1000) * ch)
    end = min(len(samples), tail + 1 + int(sr * keep_tail_ms / 1000) * ch)
    if end - start < min_keep:
        # 语音本身太短（如单字「嗯」）：以语音段为中心对称补齐，别把字切掉
        mid = (head + tail + 1) // 2
        start = max(0, mid - min_keep // 2)
        end = min(len(samples), start + min_keep)
        start = max(0, end - min_keep)
    if start <= 0 and end >= len(samples):
        return data  # 无可裁

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(sr)
        w.writeframes(samples[start:end].tobytes())
    return out.getvalue()


def trim_if_enabled(data: bytes) -> bytes:
    """按 PODCAST_TRIM 开关决定是否裁剪；异常时返回原数据。"""
    if not PODCAST_TRIM:
        return data
    try:
        return trim_wav(data)
    except Exception as e:  # noqa: BLE001 —— 后处理失败不能毁掉已合成段
        logger.warning("[trim] 裁剪异常，原样返回: %s", e)
        return data
