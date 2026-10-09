"""响度归一 —— 全链路唯一实现（2026-10-09 从 podcast_runner 上提到池层）。

为什么必须在池层
================
所有引擎的产出（本地 tts-server 壳 / 302.ai / autodl.art / 硅基）都经
`engines.base.Engines.synthesize_segment` 回到 backend —— 那是**唯一**的共同出口。
归一放这里，一次覆盖四种组合：

              本地壳      云 API
    单人配音   池层        池层
    双人播客   池层        池层

历史上有两份重复实现（tts-server 的 `podcast_engine._apply_speed` 与 backend 的
`podcast_runner._normalize_segment`，6 个常量逐项相同），造成三个问题：
  ① **单人 + 云 API 实际没人归一** —— mono_runner 后端不做后处理、云引擎自述
     `normalizes_loudness=False`，而 podcast_runner 的归一只服务播客路径；
  ② 归一的执行者随所选资源走，同一篇音频的响度由"选了哪条资源"决定；
  ③ 两处实现要同步改，容易走偏。

本次统一：壳侧退役、podcast_runner 侧退役，只留这一份。

处理链
======
    ebur128 测积分响度 → volume=+(target−measured)dB → alimiter 限幅 → 24kHz

目标 **-16 LUFS**、峰值上限 **-1.5 dBFS**；alimiter 再留 0.5 dB 余量 ⇒ 实际
限到约 -2.0 dBFS。逐段独立归一（不是整片一次），多音色之间的响度一致靠的就是它。

开关
====
`PODCAST_NORM`（**模块导入时**读取 ⇒ 改 .env 后必须重启 backend，与 MEMBER_* 同规则）：

    gain（默认）          逐段测量 → 固定增益 → 限幅
    off/none/0/false/no   完全不做响度归一（只比对，不处理）

  空值（写成 `PODCAST_NORM=`）按**默认 gain** 处理，只有显式的 off 词才关闭 ——
  否则少写一个值就静默关掉归一，属于「配了却不生效」那一类难查的坑。

⚠️ 这是**全局唯一**的归一开关。关掉它，本地壳与云 API、单人与播客全都不归一。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 参数（对齐全链路交付口径：播客 -16 LUFS / 峰值 -1.5 dBFS）──
NORM_TARGET_LUFS = -16.0
NORM_CEILING_DBFS = -1.5
# alimiter 限制的是采样峰值，真峰值（过采样）会略微过冲，留出余量
NORM_LIMITER_MARGIN_DB = 0.5
# 单行提升量上限：超过说明该行原始输出明显偏低（模型偶发低电平），
# 记警告提示重生成该行；仍按上限提升，避免整片电平失衡。
NORM_MAX_GAIN_DB = 24.0
# 需要提升超过这个量，说明该行原始输出异常偏小，值得重生成
NORM_ABNORMAL_GAIN_DB = 12.0
# 测不出响度时（过短或全静音）的兜底滤镜
NORM_LEGACY_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"

NORM_MODE = os.environ.get("PODCAST_NORM", "").strip().lower() or "gain"
NORM_OFF_VALUES = ("off", "none", "0", "false", "no")
NORM_ENABLED = NORM_MODE not in NORM_OFF_VALUES

_FFMPEG_WARNED = False


def find_ffmpeg() -> Optional[str]:
    """定位 ffmpeg：PATH 优先，兜底常见安装路径（macOS homebrew / Linux 发行版）。

    为什么不能只信 shutil.which：本机与部分容器里 ffmpeg 装在 /opt/homebrew/bin
    或 /usr/local/bin 但不在 PATH 上，只查 PATH 会误判「没有 ffmpeg」而退化到
    低质量重采样。
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(cand).is_file():
            return cand
    return None


def fix_wav_header(data: bytes) -> bytes:
    """修正 ffmpeg 管道输出的 wav 头：RIFF/data size 是流式占位（0xFFFFFFFF 或 0）。

    wave 模块按头部声明的 size 读取，坏头会导致 readframes 读错帧数、
    拼接产物错乱。按实际字节数回写。
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


def measure_loudness(ffmpeg: str, data: bytes) -> Optional[float]:
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


def _apply_loudness(data: bytes) -> bytes:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        global _FFMPEG_WARNED
        if not _FFMPEG_WARNED:
            logger.warning("[norm] 未找到 ffmpeg，跳过响度归一（各段响度可能不齐）")
            _FFMPEG_WARNED = True
        return data

    lufs = measure_loudness(ffmpeg, data)
    if lufs is None:
        # 过短或全静音：测不出积分响度，退回单遍 loudnorm
        filters = [NORM_LEGACY_FILTER]
    else:
        gain = NORM_TARGET_LUFS - lufs
        if gain > NORM_ABNORMAL_GAIN_DB:
            logger.warning(
                "[norm] 段原始响度 %.1f LUFS 偏小（需提升 %.1f dB），建议核对听感",
                lufs, gain,
            )
        gain = min(gain, NORM_MAX_GAIN_DB)
        limit = 10 ** ((NORM_CEILING_DBFS - NORM_LIMITER_MARGIN_DB) / 20)
        filters = [f"volume={gain:.2f}dB", f"alimiter=limit={limit:.4f}:level=disabled"]

    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-filter:a", ",".join(filters), "-ar", "24000", "-f", "wav", "pipe:1"],
        input=data, capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", "ignore")[-300:]
        logger.warning("[norm] ffmpeg 归一失败，返回原始音频: %s", stderr)
        return data
    return fix_wav_header(result.stdout)


def apply_loudness(data: bytes) -> bytes:
    """把一段音频（24kHz/单声道/PCM16 WAV）归一到 -16 LUFS，峰值 ≤ -1.5 dBFS。

    输入格式由引擎池的 `normalize_pcm` 保证，本函数**只管响度、不变速**。

    任何失败（无 ffmpeg / 调用异常 / 滤镜报错）都返回**原始数据**并记告警 ——
    音频已合成，后处理失败不该毁掉那一段。
    """
    if not NORM_ENABLED:
        return data
    try:
        return _apply_loudness(data)
    except Exception as exc:  # noqa: BLE001 —— 后处理失败绝不向上抛，见 docstring
        logger.warning("[norm] 响度归一异常，返回原始音频: %s", exc)
        return data
