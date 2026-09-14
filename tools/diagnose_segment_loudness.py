#!/usr/bin/env python3
"""成品音频逐段响度诊断。

用途：定位播客成品里「某一段音量明显偏小」的问题。

原理：用 ffmpeg 的 silencedetect 把成品切成语音区间，逐个区间量
积分响度（EBU R128 / LUFS）和采样峰值，再和全体中位数比对。
偏低的区间会直接标出来。

为什么要这么量：tts-server 的 podcast_engine.py 对**每一段**音频
独立跑 `loudnorm=I=-16:TP=-1.5:LRA=11`（单遍动态模式）。如果某段
原始输出里存在一个极短的瞬时高峰（起音爆音、咔哒、或 int16 截顶
造成的平顶波），loudnorm 为了守住 TP=-1.5 的峰值上限，会把整段的
增益压住。段越短，这个瞬时峰对整段的影响越大 —— 2~3 秒的短句可以
被压掉 6 dB 以上，而 10 秒以上的长句几乎不受影响。

用法：
    python3 diagnose_segment_loudness.py <成品.wav>
"""

from __future__ import annotations

import argparse
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

SILENCE_FLOOR_DB = -40
SILENCE_MIN_SEC = 0.35
DEVIATION_FLAG_DB = 2.0
# 太短或太轻的区间不是语音，是拼接边界的碎片，需剔除后再统计
MIN_SPEECH_SEC = 0.30
MIN_SPEECH_LUFS = -45.0


FALLBACK_FFMPEG_PATHS = (
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    "/root/miniconda3/bin/ffmpeg",
)


def find_ffmpeg(explicit: str | None = None) -> str:
    """定位 ffmpeg；PATH 里没有时回退到常见安装路径。"""
    if explicit:
        if Path(explicit).exists():
            return explicit
        sys.exit(f"指定的 ffmpeg 不存在: {explicit}")

    found = shutil.which("ffmpeg")
    if found:
        return found

    for candidate in FALLBACK_FFMPEG_PATHS:
        if Path(candidate).exists():
            return candidate

    sys.exit("找不到 ffmpeg。请安装，或用 --ffmpeg 指定完整路径")


def run(ffmpeg: str, args: list[str]) -> str:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", *args],
        capture_output=True, text=True,
    )
    return proc.stdout + proc.stderr


def probe_duration(ffmpeg: str, path: str) -> float:
    out = run(ffmpeg, ["-i", path])
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not match:
        return 0.0
    hour, minute, second = match.groups()
    return int(hour) * 3600 + int(minute) * 60 + float(second)


def detect_speech_regions(ffmpeg: str, path: str) -> list[tuple[float, float]]:
    out = run(ffmpeg, [
        "-i", path,
        "-af", f"silencedetect=n={SILENCE_FLOOR_DB}dB:d={SILENCE_MIN_SEC}",
        "-f", "null", "-",
    ])
    duration = probe_duration(ffmpeg, path)

    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[\d.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[\d.]+)", out)]

    regions: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in zip(starts, ends + [duration] * (len(starts) - len(ends))):
        if start - cursor > 0.05:
            regions.append((cursor, start))
        cursor = end
    if duration - cursor > 0.05:
        regions.append((cursor, duration))
    return regions


def integrated_lufs(ffmpeg: str, path: str) -> float | None:
    out = run(ffmpeg, ["-i", path, "-af", "ebur128=framelog=quiet", "-f", "null", "-"])
    match = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+)\s*LUFS", out)
    return float(match.group(1)) if match else None


def peak_dbfs(ffmpeg: str, path: str) -> float | None:
    out = run(ffmpeg, ["-i", path, "-af", "volumedetect", "-f", "null", "-"])
    match = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", out)
    return float(match.group(1)) if match else None


def main() -> None:
    global SILENCE_FLOOR_DB, SILENCE_MIN_SEC

    parser = argparse.ArgumentParser(description="成品音频逐段响度诊断")
    parser.add_argument("audio", help="待诊断的成品 WAV 路径")
    parser.add_argument("--floor", type=float, default=SILENCE_FLOOR_DB,
                        help=f"静音判定阈值 dB（默认 {SILENCE_FLOOR_DB}）")
    parser.add_argument("--min-silence", type=float, default=SILENCE_MIN_SEC,
                        help=f"静音最短时长秒（默认 {SILENCE_MIN_SEC}）")
    parser.add_argument("--ffmpeg", default=None,
                        help="ffmpeg 可执行文件的完整路径（PATH 里没有时使用）")
    args = parser.parse_args()

    SILENCE_FLOOR_DB = args.floor
    SILENCE_MIN_SEC = args.min_silence

    path = Path(args.audio)
    if not path.exists():
        sys.exit(f"文件不存在: {path}")

    ffmpeg = find_ffmpeg(args.ffmpeg)
    regions = detect_speech_regions(ffmpeg, str(path))
    if not regions:
        sys.exit("没有检出任何语音区间，请调低 --floor 或检查文件")

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for index, (start, end) in enumerate(regions, start=1):
            clip = str(Path(tmp) / f"r{index:03d}.wav")
            run(ffmpeg, ["-y", "-i", str(path), "-ss", f"{start:.3f}",
                         "-to", f"{end:.3f}", "-ar", "24000", "-ac", "1", clip])
            rows.append({
                "index": index,
                "start": start,
                "dur": end - start,
                "lufs": integrated_lufs(ffmpeg, clip),
                "peak": peak_dbfs(ffmpeg, clip),
            })

    kept = [
        r for r in rows
        if r["dur"] >= MIN_SPEECH_SEC
        and r["lufs"] is not None
        and r["lufs"] >= MIN_SPEECH_LUFS
    ]
    dropped = len(rows) - len(kept)

    measured = [r["lufs"] for r in kept]
    median = statistics.median(measured) if measured else None

    print(f"\n文件: {path}")
    print(f"总时长: {probe_duration(ffmpeg, str(path)):.1f}s   "
          f"检出语音区间: {len(kept)}"
          + (f"（已剔除 {dropped} 处拼接碎片）" if dropped else ""))
    print()
    print(f"{'#':>3}  {'起始':>8}  {'时长':>7}  {'响度LUFS':>9}  "
          f"{'峰值dBFS':>9}  {'峰均比':>7}  判定")
    print("-" * 66)

    for row in kept:
        lufs = f"{row['lufs']:.1f}"
        peak = f"{row['peak']:.1f}" if row["peak"] is not None else "n/a"
        crest = (f"{row['peak'] - row['lufs']:.1f}"
                 if row["peak"] is not None else "n/a")

        verdict = ""
        if median is not None:
            delta = row["lufs"] - median
            if delta <= -DEVIATION_FLAG_DB:
                verdict = f"<<< 偏低 {delta:+.1f} dB"
            elif delta >= DEVIATION_FLAG_DB:
                verdict = f"<<< 偏高 {delta:+.1f} dB"

        print(f"{row['index']:>3}  {row['start']:>7.2f}s  {row['dur']:>6.2f}s  "
              f"{lufs:>9}  {peak:>9}  {crest:>7}  {verdict}")

    if median is not None and len(measured) > 1:
        print("-" * 66)
        print(f"响度中位数: {median:.1f} LUFS   "
              f"极差: {max(measured) - min(measured):.1f} dB")
        crests = [r["peak"] - r["lufs"] for r in kept if r["peak"] is not None]
        if crests:
            print(f"峰均比中位数: {statistics.median(crests):.1f} dB")
        print("提示: 峰均比明显高于其他区间的，说明段内存在瞬时高峰，"
              "是 loudnorm 压住整段增益的典型特征。\n")


if __name__ == "__main__":
    main()
