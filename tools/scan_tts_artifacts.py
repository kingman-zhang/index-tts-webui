#!/usr/bin/env python3
"""离线扫描 TTS 产物里的「孤立高频噪声包」（俗称"撕"音）——只读，可选生成修补副本。

背景
----
IndexTTS 是「自回归 LM + 声码器」两段式。句子读完、模型预测停止符前后，若多解码了
几帧低幅度 latent，声码器会把它们解成 0.05–0.3 s 的宽带噪声，听感即一声"嘶/撕"。

它随机出现在句间停顿内，与文本、标点、切片点都无关：
2026-10-09 实证 —— 同文本重生成即消失；265 s 成品里 3 处，位置各异、时长各异。

这类 artifact 有两型，判据不同（2026-10-09 用全章 26 个文件、约 80 分钟标定）：

  A 型「停顿里的孤立包」  高频 + 短促(40–300 ms) + 前后各有安静空隙
  B 型「连续长高频块」    高频 + 无周期 + 时长 >= 150 ms（**不看孤立性**）

两型共有：高频（帧 ZCR >= 2500，实测 3000–6400）、无周期（自相关 < 0.35，
实测 0.066–0.137；语音 0.33–0.75）。频率维度只有「有周期/无周期」这一刀 ——
频谱上 artifact 与正常擦音完全同族，分不开（实测频带占比重叠）。

A 型的「孤立」条件是首版骨架，它放过了**紧贴语音尾**的那一类：用户反馈
ch12-s02-p03 的 14.14–14.40 漏检就是这个原因（紧贴前段语音、后接 0.2 s
拼接静音，gap_before ≈ 0 被挡）。B 型不看孤立，补上这个缺口。

B 型的时长门槛有实测依据：9384 个高频块里，
  <=0.05s 3824 | 0.05–0.08 3768 | 0.08–0.12 1633 | 0.12–0.15 144
  0.15–0.20    6 | 0.20–0.30    9 | >=0.30      0
0.12 s 后断崖式下降，且 >= 0.15 s 的 15 个块**全部无周期**。

本工具用这几条做检出，属于**离线 QA**：不进合成链路、不改任何现有代码。
默认只读；要修补得显式加 --apply（另存副本）或 --in-place（覆盖原文件）。

每次运行都会把「在哪些位置检出了撕音」追加写进当前目录的 scan_tts_artifacts.log
—— 终端输出会滚走，日志留着可回溯。

为什么不用 ffmpeg 全片滤镜
------------------------
实测过 `agate` 噪声门：噪声段峰值 -18 dBFS / 帧电平 -28 dBFS，而语音的弱音节、
气声、尾音同样落在 -35 ~ -25 dBFS，**电平重叠，一个阈值分不开**。压得住噪声就切掉
语音尾音。所以正解是「先定位 → 再局部写零」，本工具做定位（并可选写零）。

用法（在 podcast-webui 根目录下）
--------------------------------
    # 只扫描，打印清单（默认动作）；同时在当前目录追加 scan_tts_artifacts.log
    python3 tools/scan_tts_artifacts.py ~/Downloads/ch12-s01-p01.wav

    # 目录批量（顶层 *.wav；加 -r 递归）
    python3 tools/scan_tts_artifacts.py ~/Downloads/成品/ -r

    # 输出机器可读结果（'-' 走 stdout，可管道）
    python3 tools/scan_tts_artifacts.py a.wav --json report.json

    # 生成修补副本（原文件一律不动）
    python3 tools/scan_tts_artifacts.py a.wav --apply              # -> a.fixed.wav
    python3 tools/scan_tts_artifacts.py a.wav --apply --out clean.wav

    # 直接覆盖原文件：先写临时文件 → 备份 .orig.wav → 原子替换
    python3 tools/scan_tts_artifacts.py a.wav --in-place
    python3 tools/scan_tts_artifacts.py a.wav --in-place --no-backup

    # 展开"真零区段"（链路插入的拼接静音）清单
    python3 tools/scan_tts_artifacts.py a.wav --list-boundaries

    # 换日志路径 / 关掉日志
    python3 tools/scan_tts_artifacts.py a.wav --log /tmp/scan.log
    python3 tools/scan_tts_artifacts.py a.wav --no-log

    # 有检出即返回码 1（便于批处理判错）
    python3 tools/scan_tts_artifacts.py 成品/ -r --fail-on-hit

判据阈值默认值经 2026-10-09 实测标定，见 --help 与 .workbuddy/memory/音频响度与质量.md。
不确定就先跑默认，再按清单里的「判定」列自行取舍。

依赖：仅 Python 标准库（wave / array / math / shutil / os）——零依赖，任何 python3 都能跑，
与 backend requirements.txt 无关（那里连 numpy 都没有）。
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import shutil
import sys
import time
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# 判据阈值默认值（2026-10-09 用 ch12-s01-p01.wav 实测标定）
#   实测样本：176.180–176.400 s，峰值 -18.0 dBFS，帧电平约 -28 dBFS，
#             ZCR 3410 Hz（半周期定义），自相关 0.17，前后静音 -54 / -60 dBFS
#
#   标定教训（三次踩坑，都记在这）
#   ① 别用「电平够轻」筛候选：第一版设「帧电平 <= -35 dBFS」，把这个样本本身
#      挡在门外（它的 RMS 是 -28 dBFS）；放宽到 -20 dBFS 又放进 396 处语音擦音。
#      电平维度和语音完全重叠，这条路走不通。
#   ② 正解是拿「孤立」当骨架：先找停顿，再看停顿里有没有短促的有声片段。
#      语音里的 /s/、/ʃ/ 天然不在停顿中，一个都不会入选。
#   ③ 「孤立」只对 A 型成立（B 型的存在就是为了捞不孤立的）。想用绝对的「紧邻帧
#      <= -40 dBFS」去补，会误杀真样本（它们紧邻帧 -38.9，恰在停顿线上方一点）。
#      能跨两型复用、又不吃响度偏差的，是**相对**的 Δ（见 DEF_DELTA_DB）。
# ---------------------------------------------------------------------------
DEF_FRAME_MS = 20.0          # 分析帧长
DEF_PAUSE_DBFS = -40.0       # 帧电平低于此值即视作「停顿」
DEF_GAP_MS = 100.0           # 候选前后各需的空隙长度（见 --gap-ms 的取舍说明）
DEF_MIN_DUR_MS = 40.0        # 候选最小持续时长（另两处实测 60–80 ms）
DEF_MAX_DUR_MS = 300.0       # 候选最大持续时长。实测 artifact 最长 0.22 s，而最短的
                             # 「整词被停顿包围」语音段是 0.36 s（"义工。" 0.44 s）——
                             # 时长是把两者分开的那把刀，留 36% 余量到 300 ms
DEF_PEAK_MIN = 0.002         # 候选的样本峰值下限（-54 dBFS）：排除纯零与编码抖动
DEF_ZCR_HZ = 2500.0          # 候选的零交叉率下限（半周期定义，见 frame_stats）
DEF_QUIET_DBFS = -40.0       # 写零时扩展边界允许的最大帧电平（与停顿线一致）：
                             # 噪声包是渐强的，主峰前还有一段 -35 ~ -45 dBFS 的起音，
                             # 线卡在 -45 会留下这段尾巴（实测残留 -34.7 dBFS）
DEF_AUTOCORR_MAX = 0.35      # 自相关峰值上限，低于此判为无周期
DEF_PAD_MS = 100.0           # 写零时向两侧静音扩展的最大距离

# B 型（连续长高频块）—— 2026-10-09 全章 26 个文件标定
DEF_LONG_MIN_MS = 150.0      # 时长下限。9384 个高频块的时长分布：
                             #   <=0.05s 3824 | 0.05–0.08 3768 | 0.08–0.12 1633
                             #   0.12–0.15  144 | 0.15–0.20    6 | 0.20–0.30    9 | >=0.30  0
                             # 0.12 s 之后断崖；>=0.15 s 的 15 个块全部无周期。门槛落在这条断崖上。
DEF_LONG_MAX_MS = 600.0      # 时长上限（实测最长 0.28 s，留一倍余量，防止把连续擦音序列误合）

# 相对邻域电平（两型共用）—— 2026-10-10 用 18 段人工标注标定（15 真 / 3 假）
#
#   Δ = 候选帧电平 − 候选左右各 DEF_EDGE_FRAMES 帧内最响的一帧电平
#
#   物理依据：artifact 悬在没有文本的安静处，是它 ±60 ms 邻域里的电平峰（实测 Δ ≥ +6.8）；
#   人声擦音（s/ʃ/x）是音节声母，必被更响的元音包着，比邻域轻（实测 3 处假的 Δ ≤ -4.0）。
#   两个簇之间空出 12 dB，门槛落在 0：被砍的最近一个假样本在 -5.6，保留的最近一个真样本在 +6.8。
#
#   为什么用「相对」不用绝对 dBFS：各章响度差 4 dB（第一章/第十二章 -20.2，其余 -16.3），
#   绝对门槛会按章偏置。绝对版（「紧邻帧 ≤ -40 dBFS」）实测 404 → 249 处，但会误杀
#   3 处已确认的真样本（它们紧邻帧是 -38.9/-39.7，恰在停顿线上方一点点）。
#   相对版 404 → 354 处，15/15 真样本全保留。
#
#   注意 A 型（burst）按构造必然过这一关（它的 gap 规则本就要求前后各有安静帧），
#   所以这条判据**实际作用在 B 型（long）上**。--delta-db -999 关闭该判据。
DEF_EDGE_FRAMES = 3          # 邻域半宽（帧）；3 帧 = ±60 ms
DEF_DELTA_DB = 0.0           # 候选电平须 >= 邻域最响帧 + 此值；0 = 必须是本地电平峰

DEF_LOG_NAME = "scan_tts_artifacts.log"  # 默认日志名（写在当前目录，追加）
DEF_BACKUP_SUFFIX = ".orig.wav"          # --in-place 的备份后缀：a.wav -> a.orig.wav

INT16_SCALE = 32768.0
SUPPORTED_SAMPWIDTH = 2  # PCM16
PAUSE_LINE = 10.0 ** (DEF_PAUSE_DBFS / 20.0) * INT16_SCALE  # 「停顿」的样本电平门限


class WavError(Exception):
    """无法处理的 WAV：给出原因 + 下一步，而不是只抛位置。"""


class FixError(Exception):
    """修补落盘阶段的失败。抛出时保证原文件未被改动。"""


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def mmss(t: float) -> str:
    """秒 → m:ss.mmm。人报「2:56 那处」比 176.26 直观。"""
    m = int(t // 60)
    return f"{m}:{t - m * 60:06.3f}"


def dbfs(amp: float) -> float:
    if amp <= 0.0:
        return -math.inf
    return 20.0 * math.log10(amp / INT16_SCALE)


def db_field(amp: float):
    """给 JSON 用的 dBFS 取值：全零信号没有 dBFS，记 None 而不是 -Infinity
    （-Infinity 是 Python 的 JSON 扩展，标准解析器读不了）。"""
    v = dbfs(amp)
    return None if v == -math.inf else round(v, 1)


def db_text(v) -> str:
    return "—" if v is None else f"{v}"


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def read_wav(path: Path):
    """读 PCM16 WAV。

    返回 (interleaved, mono, sr, nch)：
      interleaved —— array('h')，原始交错的全部样本（写零用，保真不丢声道）
      mono        —— array('h')，下混单声道（分析用；nch==1 时与前者同一对象）

    非 PCM16 一律明确报错并给出转换命令；不做宽容解析（宽容的代价是判定失真）。
    """
    try:
        with wave.open(str(path), "rb") as w:
            nch = w.getnchannels()
            sw = w.getsampwidth()
            sr = w.getframerate()
            n = w.getnframes()
            comptype = w.getcomptype()
            raw = w.readframes(n)
    except wave.Error as e:
        raise WavError(f"不是可解析的 WAV（wave 模块报错：{e}）") from e

    if comptype != "NONE":
        raise WavError(f"压缩 WAV（comptype={comptype}）不支持，请先解码为 PCM")
    if sw != SUPPORTED_SAMPWIDTH:
        hint_w = {1: "8-bit PCM", 3: "24-bit PCM", 4: "32-bit PCM / 32-bit float"}.get(sw, "未知")
        raise WavError(
            f"本工具只处理 16-bit PCM，该文件是 {sw * 8}-bit（{hint_w}）。\n"
            f"    请先转换：ffmpeg -i \"{path}\" -c:a pcm_s16le \"{path.stem}.s16le.wav\""
        )
    if nch < 1:
        raise WavError(f"声道数异常：{nch}")

    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder != "little":  # WAV 恒为 little-endian
        a.byteswap()

    if nch == 1:
        return a, a, sr, nch

    frames = len(a) // nch
    mono = array.array("h", bytes(2 * frames))
    for i in range(frames):
        base = i * nch
        mono[i] = int(sum(a[base + c] for c in range(nch)) / nch)
    return a, mono, sr, nch


# ---------------------------------------------------------------------------
# 帧级统计（单次遍历，同时得到峰值 / 电平 / 零交叉率 / 是否全零）
# ---------------------------------------------------------------------------
def frame_stats(mono, sr: int, frame_ms: float):
    flen = max(2, int(sr * frame_ms / 1000.0))
    n = len(mono)
    peaks: list[int] = []
    rmss: list[float] = []
    zcrs: list[float] = []
    for k in range(0, n, flen):
        end = min(k + flen, n)
        peak = 0
        ss = 0
        zc = 0
        prev = 0
        for i in range(k, end):
            v = mono[i]
            if v:
                if v < 0:
                    if prev > 0:
                        zc += 1
                    prev = -1
                else:
                    if prev < 0:
                        zc += 1
                    prev = 1
                av = v if v > 0 else -v
                if av > peak:
                    peak = av
                ss += v * v
            elif prev != 0:
                prev = 0
        cnt = end - k
        peaks.append(peak)
        rmss.append(math.sqrt(ss / cnt) if cnt else 0.0)
        zcrs.append((zc / (cnt - 1)) * sr / 2.0 if cnt > 1 else 0.0)
    return peaks, rmss, zcrs, flen


def sample_zero_runs(mono, sr: int, min_ms: float):
    """样本级精确找「严格为 0」的连续区段（>= min_ms）。

    用 bytes.find 做块级定位（C 速度），再逐样本把两端补齐 —— 比按帧判「整帧是否
    全零」精确：帧级会漏掉「长度刚过 20 ms 但不含完整帧」的短区段（实测漏了 2 处
    30 ms 的）。这些区段是链路插入拼接静音的指纹，拼接口径不能凑合。
    """
    min_len = max(1, int(sr * min_ms / 1000.0))
    n = len(mono)
    if n < min_len:
        return []
    raw = mono.tobytes()
    pat = b"\x00" * (2 * min_len)
    runs = []
    pos = 0
    while True:
        k = raw.find(pat, pos)
        if k < 0:
            break
        k -= k % 2  # 对齐到样本起点（pat 全零，落在区段内任意位置都等价）
        i = k // 2
        lo = i
        while lo > 0 and mono[lo - 1] == 0:
            lo -= 1
        hi = k // 2 + min_len
        while hi < n and mono[hi] == 0:
            hi += 1
        runs.append((lo / sr, hi / sr, (hi - lo) / sr))
        pos = 2 * hi
    return runs


# ---------------------------------------------------------------------------
# 候选检出
#
# 两型 artifact，判据不同。2026-10-09 用全章 26 个文件（约 80 分钟）标定后确立：
#
#   A 型「停顿里的孤立包」  高频 + 短促(40–300 ms) + 前后各有安静空隙
#      首版骨架。要求「孤立」，所以只覆盖句中停顿里那种。
#      实测命中：ch12-s01-p01 的 176.26 / 224.40 / 265.26。
#
#   B 型「连续长高频块」    高频 + 无周期 + 时长 >= --min-long-ms（默认 150 ms）
#      补漏。用户反馈 ch12-s02-p03 的 14.14–14.40 漏检，根因是它**紧贴前段语音**
#      （gap_before ≈ 0），被 A 型的「孤立」条件挡在门外。B 型不看孤立性。
#
# 为什么 B 型只能用「时长」当判据：频谱上它与正常擦音**完全无法区分**（实测
# 3–6 kHz / 6–11 kHz 频带占比与句中正常擦音重叠），因为 artifact 本身就是宽带
# 高频噪声、和人的齿龈擦音同族。频率维度只剩「有周期 / 无周期」这一刀（浊音 = 语音）。
# 有辨识力的只剩时长，且实测分布是断崖式的（见 DEF_LONG_MIN_MS 处的数字）。
# ---------------------------------------------------------------------------
def _edge_peak_dbfs(rmss, i0, i1, k):
    """候选左右各 k 帧内最响的一帧的电平（dBFS）；两侧都没有可用帧时返回 None。

    「相对邻域电平」判据的分母。全零帧（链路拼接静音）没有 dBFS，跳过不看 ——
    整段被真零包围的候选视为通过（那正是 artifact 的标准形态）。
    """
    vals = [dbfs(rmss[i]) for i in range(max(0, i0 - k), i0)]
    vals += [dbfs(rmss[i]) for i in range(i1, min(len(rmss), i1 + k))]
    vals = [v for v in vals if v != -math.inf]
    return max(vals) if vals else None


def _cand(peaks, rmss, zcrs, flen, sr, a, i0, i1, kind, gap_before, gap_after):
    """把一段帧区间组装成候选记录（两型共用）。"""
    s0, s1 = i0 * flen, min(i1 * flen, len(a))
    ac, ac_hz = autocorr_peak(a[s0:s1], sr)
    aperiodic = ac < DEF_AUTOCORR_MAX
    lvl_db = dbfs(sum(rmss[i0:i1]) / (i1 - i0))
    edge = _edge_peak_dbfs(rmss, i0, i1, DEF_EDGE_FRAMES)
    return {
        "start": round(s0 / sr, 4),
        "end": round(s1 / sr, 4),
        "dur": round((i1 - i0) * flen / sr, 4),
        "kind": kind,
        "peak_dbfs": round(dbfs(max(peaks[i0:i1])), 1),
        "level_dbfs": round(lvl_db, 1),
        "edge_dbfs": None if edge is None else round(edge, 1),
        "delta_db": None if edge is None else round(lvl_db - edge, 1),
        "zcr_hz": round(sum(zcrs[i0:i1]) / (i1 - i0)),
        "autocorr": round(ac, 3),
        "periodic_hz": round(ac_hz) if not aperiodic else None,
        "gap_before_ms": None if gap_before is None else round(gap_before),
        "gap_after_ms": None if gap_after is None else round(gap_after),
        "aperiodic": aperiodic,
        "verdict": "hit" if aperiodic else "weak",
        "_i0": i0,
        "_i1": i1,
    }


def _groups(mask, allow=2):
    """把帧索引列表连成簇（间隔 <= allow 帧视作同簇）。"""
    out = []
    for i in mask:
        if out and i - out[-1][-1] <= allow:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def _find_bursts(peaks, rmss, zcrs, flen, sr, a, gap_ms, min_dur_ms):
    """A 型：停顿里的短促孤立高频包（首版骨架，判据原样不动）。"""
    frame_s = flen / sr
    clusters = _groups([i for i in range(len(peaks)) if rmss[i] > PAUSE_LINE])
    out = []
    for ci, cl in enumerate(clusters):
        i0, i1 = cl[0], cl[-1] + 1
        dur = (i1 - i0) * frame_s
        if not (min_dur_ms <= dur * 1000.0 <= DEF_MAX_DUR_MS):
            continue

        # 空隙：与相邻有声簇的距离。文件首尾没有相邻簇时记为 None（不构成漏检）
        # —— 早先版本把末尾的 gap_after 算成 0，文件最后那处 artifact 被自己的规则挡掉了。
        if ci > 0:
            gap_before = (i0 - (clusters[ci - 1][-1] + 1)) * frame_s * 1000.0
        else:
            gap_before = i0 * frame_s * 1000.0
        if ci + 1 < len(clusters):
            gap_after = (clusters[ci + 1][0] - i1) * frame_s * 1000.0
        else:
            gap_after = None

        if gap_before < gap_ms:
            continue
        if gap_after is not None and gap_after < gap_ms:
            continue
        if max(peaks[i0:i1]) < DEF_PEAK_MIN * INT16_SCALE:
            continue
        if sum(zcrs[i0:i1]) / (i1 - i0) < DEF_ZCR_HZ:
            continue

        out.append(_cand(peaks, rmss, zcrs, flen, sr, a, i0, i1, "burst", gap_before, gap_after))
    return out


def _quiet_run(rmss, start, step):
    """从 start 沿 step 方向，数连续「安静帧」（电平 <= 停顿线）的个数。"""
    k = start
    cnt = 0
    while 0 <= k < len(rmss) and rmss[k] <= PAUSE_LINE:
        cnt += 1
        k += step
    return cnt


def _find_longs(peaks, rmss, zcrs, flen, sr, a, min_long_ms):
    """B 型：连续长高频块。**不看孤立性** —— 这正是它补上的缺口。

    入场：帧 ZCR >= 2500 且帧电平高于停顿线，连通段时长落在
          --min-long-ms ~ DEF_LONG_MAX_MS。判定仍看自相关（无周期 = 疑似）。
    gap_* 只作信息展示（该块两侧连续安静有多长），不参与判定。
    """
    frame_s = flen / sr
    hi = [i for i in range(len(peaks)) if zcrs[i] >= DEF_ZCR_HZ and rmss[i] > PAUSE_LINE]
    n = len(peaks)
    out = []
    for g in _groups(hi):
        i0, i1 = g[0], g[-1] + 1
        dur = (i1 - i0) * frame_s
        if not (min_long_ms <= dur * 1000.0 <= DEF_LONG_MAX_MS):
            continue
        if max(peaks[i0:i1]) < DEF_PEAK_MIN * INT16_SCALE:
            continue
        gb = _quiet_run(rmss, i0 - 1, -1) * frame_s * 1000.0 if i0 > 0 else None
        ga = _quiet_run(rmss, i1, 1) * frame_s * 1000.0 if i1 < n else None
        out.append(_cand(peaks, rmss, zcrs, flen, sr, a, i0, i1, "long", gb, ga))
    return out


def _overlap_ratio(x, y):
    i0, i1 = x["_i0"], x["_i1"]
    j0, j1 = y["_i0"], y["_i1"]
    inter = max(0, min(i1, j1) - max(i0, j0))
    span = max(i1 - i0, j1 - j0)
    return inter / span if span else 0.0


def find_candidates(peaks, rmss, zcrs, flen, sr, a, gap_ms=DEF_GAP_MS, min_dur_ms=DEF_MIN_DUR_MS,
                    min_long_ms=DEF_LONG_MIN_MS, use_long=True, delta_db=DEF_DELTA_DB):
    """返回两型候选的并集，按位置排序。

    两型会指向同一段（A 型命中的包几乎都 >= 150 ms），此时只保留 A 型 ——
    它判据更严（额外要求孤立）。

    最后统一过一遍「相对邻域电平」判据：没过的**不删**，只把 verdict 降成 "faint"
    （--show-weak 仍看得到）。被砍了什么永远可查，不做静默丢弃。
    """
    bursts = _find_bursts(peaks, rmss, zcrs, flen, sr, a, gap_ms, min_dur_ms)
    if use_long:
        merged = list(bursts)
        for c in _find_longs(peaks, rmss, zcrs, flen, sr, a, min_long_ms):
            if any(_overlap_ratio(c, b) > 0.5 for b in bursts):
                continue
            merged.append(c)
    else:
        merged = bursts
    for c in merged:
        if c["verdict"] == "hit" and c["delta_db"] is not None and c["delta_db"] < delta_db:
            c["verdict"] = "faint"
    merged.sort(key=lambda c: c["start"])
    return merged, flen


def autocorr_peak(seg, sr: int):
    """在语音基频范围（70–350 Hz）找自相关最大峰，返回 (峰值, 对应频率)。

    直接在原始采样率上算 —— 早先版本先做 4:1 直接抽取，遇到 3.5 kHz 的窄带噪声会
    混叠成 2.5 kHz 的假周期信号，自相关冲到 1.0，把「疑似」误降成「弱」。
    成本用「粗扫 + 细化」压住：步长 4 找峰，再在 ±4 内精算。
    """
    max_len = int(sr * 0.3)
    if len(seg) > max_len:
        seg = seg[:max_len]
    n = len(seg)
    if n < 256:
        return 0.0, 0.0
    mean = sum(seg) / n
    c = [v - mean for v in seg]
    denom = sum(v * v for v in c)
    if denom <= 0:
        return 0.0, 0.0

    lo = max(1, int(sr / 350.0))
    hi = min(n - 1, int(sr / 70.0))
    if hi <= lo:
        return 0.0, 0.0

    def corr(lag):
        s = 0.0
        for i in range(lag, n):
            s += c[i] * c[i - lag]
        return s / denom

    best = 0.0
    blag = lo
    for lag in range(lo, hi + 1, 4):
        r = corr(lag)
        if r > best:
            best = r
            blag = lag
    for lag in range(max(lo, blag - 4), min(hi, blag + 4) + 1):
        r = corr(lag)
        if r > best:
            best = r
            blag = lag
    return best, (sr / blag if blag else 0.0)


# ---------------------------------------------------------------------------
# 单个文件分析
# ---------------------------------------------------------------------------
def analyze(path: Path, gap_ms=DEF_GAP_MS, min_dur_ms=DEF_MIN_DUR_MS, min_long_ms=DEF_LONG_MIN_MS,
            use_long=True, delta_db=DEF_DELTA_DB):
    interleaved, mono, sr, nch = read_wav(path)
    peaks, rmss, zcrs, flen = frame_stats(mono, sr, DEF_FRAME_MS)
    bounds = sample_zero_runs(mono, sr, 30.0)
    cands, _ = find_candidates(peaks, rmss, zcrs, flen, sr, mono, gap_ms, min_dur_ms,
                               min_long_ms, use_long, delta_db)

    # 全片整电平：用帧电平的均方根，避免再全量遍历一遍样本
    g_rms = math.sqrt(sum(r * r for r in rmss) / len(rmss)) if rmss else 0.0
    g_peak = max(peaks) if peaks else 0

    return {
        "file": str(path),
        "duration": round(len(mono) / sr, 3),
        "sample_rate": sr,
        "channels": nch,
        "sample_width_bits": 16,
        "overall_peak_dbfs": db_field(g_peak),
        "overall_rms_dbfs": db_field(g_rms),
        "zero_runs": [[round(a, 3), round(b, 3), round(d, 3)] for a, b, d in bounds],
        "candidates": cands,
        "_sr": sr,
        "_nch": nch,
        "_flen": flen,
        "_interleaved": interleaved,
        "_rmss": rmss,
    }


# ---------------------------------------------------------------------------
# 写零修补
# ---------------------------------------------------------------------------
def _write_wav(path: Path, samples, sr: int, nch: int):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())


def apply_fix(report, out_path: Path, pad_ms: float, backup: bool = True):
    """把「疑似」区段置零并写出。

    返回 (changes, info)：
      changes —— 实际写零的区间 [(t0, t1), ...]；**空列表表示无可修补项，不写任何文件**
      info    —— 描述本次如何落盘（终端与日志共用）

    覆盖原文件时（out_path 与源文件是同一路径），步骤顺序保证任何一步失败原文件都完好：
      ① 先写同目录临时文件     —— 失败 ⇒ 原文件一个字没动
      ② 检查备份是否已存在     —— 存在则报错并清理临时文件，不碰任何东西
      ③ 复制原文件为 .orig.wav
      ④ os.replace 原子替换    —— 到这一步才真正覆盖
    """
    sr = report["_sr"]
    nch = report["_nch"]
    flen = report["_flen"]
    a = report["_interleaved"]
    rmss = report["_rmss"]
    src = Path(report["file"])
    n_frames_total = len(a) // nch

    quiet_line = PAUSE_LINE
    n_extra = max(1, int(pad_ms / DEF_FRAME_MS))

    changes = []
    for c in report["candidates"]:
        if c["verdict"] != "hit":
            continue
        i0, i1 = c["_i0"], c["_i1"]
        # 向两侧扩展，直到遇到高于 -40 dBFS 的帧或达到上限（让边界落在静音里）
        lo = i0
        ext = 0
        while lo > 0 and ext < n_extra and rmss[lo - 1] <= quiet_line:
            lo -= 1
            ext += 1
        hi = i1
        ext = 0
        while hi < len(rmss) and ext < n_extra and rmss[hi] <= quiet_line:
            hi += 1
            ext += 1

        f0 = lo * flen
        f1 = min(hi * flen, n_frames_total)
        # 整段置零。不做 fade —— 这是「删除一段」不是「淡入淡出」，加 fade 反而会在
        # 区间两端留下原值的平台。边界安全性由扩展逻辑保证：两侧都停在 <= -40 dBFS
        # 的帧上（相对全片峰值约 -91 dB），置零不存在可闻跳变。
        for f in range(f0, f1):
            base = f * nch
            for ch in range(nch):
                a[base + ch] = 0
        changes.append((round(f0 / sr, 4), round(f1 / sr, 4)))

    if not changes:
        return [], {"written": False, "out": None, "backup": None, "in_place": False}

    in_place = out_path.resolve() == src.resolve()
    if not in_place:
        _write_wav(out_path, a, sr, nch)
        return changes, {
            "written": True,
            "out": str(out_path),
            "backup": None,
            "in_place": False,
        }

    # —— 覆盖模式 ——
    tmp = out_path.parent / (out_path.name + ".tmp")
    _write_wav(tmp, a, sr, nch)  # ①
    shutil.copymode(out_path, tmp)  # os.replace 会把 tmp 的元数据带过去，先补回原权限
    backup_path = None
    if backup:
        backup_path = out_path.with_suffix(DEF_BACKUP_SUFFIX)  # ②
        if backup_path.exists():
            tmp.unlink(missing_ok=True)
            raise FixError(
                f"备份已存在：{backup_path}\n"
                f"    这通常说明该文件已经被 --in-place 处理过一次。若继续，就会把"
                f"「已修复版」覆盖成备份，真正的原件再也找不回来，所以在此停下。\n"
                f"    请先确认那份备份的去留（改名/删除），或改用 --no-backup 跳过备份。"
            )
        shutil.copy2(out_path, backup_path)  # ③
    os.replace(tmp, out_path)  # ④
    return changes, {
        "written": True,
        "out": str(out_path),
        "backup": str(backup_path) if backup_path else None,
        "in_place": True,
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def verdict_text(c) -> str:
    """判定列：疑似（A 型停顿包）/ 长块（B 型连续长块）/ 弱·周期（有周期 = 像语音）/ 弱·电平（不是本地电平峰）。"""
    v = c["verdict"]
    if v == "weak":
        return "弱·周期"
    if v == "faint":
        return "弱·电平"
    return "疑似" if c["kind"] == "burst" else "长块"


def fmt_row(idx, c):
    ga = "—" if c["gap_after_ms"] is None else str(c["gap_after_ms"])
    gb = "—" if c["gap_before_ms"] is None else str(c["gap_before_ms"])
    d = "—" if c["delta_db"] is None else f"{c['delta_db']:+.1f}"
    return (
        f"  {idx:<3} {c['start']:>9.3f} – {c['end']:<9.3f} "
        f"{c['dur']:>6.3f}  {c['peak_dbfs']:>7.1f} dB  {c['level_dbfs']:>7.1f} dB  "
        f"{c['zcr_hz']:>6} Hz  {c['autocorr']:>5.2f}   "
        f"{gb:>4}/{ga:<4} ms  {d:>6} dB  "
        f"{verdict_text(c)}"
    )


HEADER = (
    "  #   区间 (s)                          时长    峰值        帧电平       "
    "ZCR       自相关   前后空隙         Δ邻域  判定"
)


def hit_mix(hits) -> str:
    nb = sum(1 for c in hits if c["kind"] == "burst")
    return f"（停顿型 {nb} / 长块型 {len(hits) - nb}）" if len(hits) - nb else ""


def print_report(report, args):
    r = report
    print("-" * 116)
    print(f"文件   {Path(r['file']).name}")
    ch = {1: "mono", 2: "stereo"}.get(r["channels"], f"{r['channels']}ch")
    print(
        f"规格   {r['duration']} s · {r['sample_rate']} Hz · {ch} · PCM16"
        f"   （全片峰值 {db_text(r['overall_peak_dbfs'])} dBFS / 电平 "
        f"{db_text(r['overall_rms_dbfs'])} dBFS）"
    )
    print(f"静音块 {len(r['zero_runs'])} 处真零（>=30 ms）  ← 链路插入的拼接静音指纹")
    if args.list_boundaries and r["zero_runs"]:
        for a, b, d in r["zero_runs"]:
            print(f"         {a:>9.3f} – {b:<9.3f}  {d:.3f} s")
    hits = [c for c in r["candidates"] if c["verdict"] == "hit"]
    weak = [c for c in r["candidates"] if c["verdict"] != "hit"]
    if hits:
        more = f"（另有 {len(weak)} 处弱：周期/电平判据未过）" if weak else ""
        print(f"结论   ⚠ 检出 {len(hits)} 处疑似 artifact{hit_mix(hits)}{more}")
    elif weak:
        print(f"结论   · 未检出确定 artifact（{len(weak)} 处弱，判据未全过）")
    else:
        print("结论   ✓ 未检出孤立高频噪声包")
    if r["candidates"]:
        shown = r["candidates"] if args.show_weak else hits
        if shown:
            print(HEADER)
            for i, c in enumerate(shown, 1):
                print(fmt_row(i, c))
        hidden = len(r["candidates"]) - len(shown)
        if hidden:
            print(f"       （另有 {hidden} 处「弱」，加 --show-weak 展开）")
    print("-" * 116)


# ---------------------------------------------------------------------------
# 运行日志（默认追加写到当前目录）
# ---------------------------------------------------------------------------
def fmt_changes(changes) -> str:
    return "  ".join(f"{a:.3f}–{b:.3f} s" for a, b in changes)


def fix_summary(info, changes) -> str:
    """一句话描述本次落盘方式，终端与日志共用。"""
    if info is None or not info["written"]:
        return "无可修补项，未改动任何文件"
    line = ("覆盖原文件 " if info["in_place"] else "写入副本 ") + info["out"]
    if info["backup"]:
        line += f"（备份 → {info['backup']}）"
    return line + "；写零 " + fmt_changes(changes)


class RunLog:
    """把「在哪些位置找到撕音」记进一个纯文本日志。

    终端输出会滚走（尤其是批量跑），日志留着可回溯；默认 append 到当前目录，
    多次运行按时间累积。--no-log 关闭，--log PATH 换位置。
    """

    def __init__(self, path):
        self.path = None if path is None else Path(path).expanduser()
        self.buf: list[str] = []

    def add(self, msg: str = ""):
        self.buf.append(f"[{stamp()}] {msg}" if msg else "")

    def commit(self):
        """把缓冲区落到磁盘并清空。放在每个文件处理完之后调用，中途崩溃也不丢已完成的记录。"""
        if self.path is None or not self.buf:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(self.buf) + "\n")
        except OSError as e:
            print(f"[警告] 日志写入失败（{self.path}）：{e}", file=sys.stderr)
        self.buf = []


def log_report(log: RunLog, rep, fix_info, changes):
    ch = {1: "mono", 2: "stereo"}.get(rep["channels"], f"{rep['channels']}ch")
    log.add(f"文件  {rep['file']}")
    log.add(
        f"规格  {rep['duration']} s · {rep['sample_rate']} Hz · {ch} · PCM16 · "
        f"峰值 {db_text(rep['overall_peak_dbfs'])} dBFS / 电平 {db_text(rep['overall_rms_dbfs'])} dBFS"
        f" · 真零 {len(rep['zero_runs'])} 处"
    )
    hits = [c for c in rep["candidates"] if c["verdict"] == "hit"]
    weak = [c for c in rep["candidates"] if c["verdict"] != "hit"]
    if hits:
        more = f"（另有 {len(weak)} 处弱：周期/电平判据未过）" if weak else ""
        log.add(f"结论  ⚠ 检出 {len(hits)} 处疑似「撕」音{hit_mix(hits)}{more}")
        for i, c in enumerate(hits, 1):
            gb = "文件开头" if c["gap_before_ms"] is None else f"{c['gap_before_ms']} ms"
            ga = "文件末尾" if c["gap_after_ms"] is None else f"{c['gap_after_ms']} ms"
            d = "—" if c["delta_db"] is None else f"{c['delta_db']:+.1f} dB"
            log.add(
                f"  #{i}  {mmss(c['start'])}–{mmss(c['end'])}"
                f"  （{c['start']:.3f}–{c['end']:.3f} s）"
                f"  时长 {c['dur']:.3f} s  峰值 {c['peak_dbfs']} dBFS"
                f"  ZCR {c['zcr_hz']} Hz  自相关 {c['autocorr']:.3f}（无周期）"
                f"  空隙 前 {gb} / 后 {ga}  邻域差 {d}  [{verdict_text(c)}]"
            )
    elif weak:
        log.add(f"结论  · 未检出疑似（{len(weak)} 处弱，判据未全过）")
    else:
        log.add("结论  ✓ 未检出孤立高频噪声包")
    if fix_info is None:
        log.add("处理  只读扫描，未改动文件")
    else:
        log.add("处理  " + fix_summary(fix_info, changes))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def collect_inputs(paths, recursive):
    files = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            files.extend(sorted(p.rglob("*.wav") if recursive else p.glob("*.wav")))
        elif p.exists():
            files.append(p)
        else:
            print(f"[跳过] 不存在：{p}", file=sys.stderr)
    return files


def build_parser():
    ap = argparse.ArgumentParser(
        prog="scan_tts_artifacts.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="离线扫描 TTS 产物中的孤立高频噪声包（\"撕\"音）。默认只读。",
        epilog="""判据：两型 artifact（判定列会标出属于哪一型）

  A 型 · 疑似   停顿里的短促孤立高频包         判定列显示「疑似」
      · 帧电平 > -40 dBFS、帧 ZCR >= 2500 Hz
      · 时长在 --min-dur-ms ~ 300 ms
      · 前后各 >= --gap-ms 的空隙（「孤立」）
  B 型 · 长块   连续长高频块，**不看孤立性**    判定列显示「长块」
      · 帧电平 > -40 dBFS、帧 ZCR >= 2500 Hz
      · 时长 >= --min-long-ms（默认 150 ms）
  判定一（两型共用，用自相关）
      · < 0.35  → 疑似 / 长块（无周期 = 宽带噪声，不是语音）
      · >= 0.35 → 弱·周期（有周期，多半是短促语音，人工过一眼）
  判定二（两型共用，用相对邻域电平 Δ）—— 2026-10-10 用 18 段人工标注标定
      · Δ = 候选帧电平 − 候选左右各 3 帧（±60 ms）内最响的一帧
      · Δ >= 0  → 保留（候选是本地电平峰）
      · Δ <  0  → 弱·电平（比邻域的语音轻 ⇒ 多半是嵌在语音里的正常擦音）

为什么需要 B 型：A 型要求「前后有安静空隙」，紧贴语音尾的那类会被整个漏掉。
实测 ch12-s02-p03 的 14.14–14.40 就因此漏检 —— 它紧贴前段语音、后接 0.2 s 拼接
静音，gap_before ≈ 0。B 型不看孤立，只看「高频 + 无周期 + 够长」。
--no-long 可退回只跑 A 型（首版行为）。

B 型的时长门槛从哪来：全章 26 个文件（约 80 分钟）共 9384 个高频块
      <=0.05s 3824 | 0.05–0.08 3768 | 0.08–0.12 1633 | 0.12–0.15 144
      0.15–0.20    6 | 0.20–0.30    9 | >=0.30      0
0.12 s 后是断崖；>= 0.15 s 的 15 个块全部无周期。门槛就落在这条断崖上。

⚠ B 型的误报风险与「Δ 邻域」判据：频谱上 artifact 与正常擦音**分不开**（同族宽带
  噪声，3–6 kHz / 6–11 kHz 频带占比完全重叠），能分的只有**电平相对关系** ——
  artifact 悬在没有文本的安静处，是它 ±60 ms 邻域里最响的东西；正常擦音是音节声母，
  必被更响的元音包着。2026-10-10 的 18 段人工标注（15 真 / 3 假）验证：
      真样本 Δ ∈ [+6.8, +30.1]，假样本 Δ ∈ [-9.2, -4.0]，中间空 12 dB ⇒ 门槛取 0。
  全量语料（12 章 / 1349 分钟）效果：404 → 354 处，15/15 真样本保留、3/3 假样本剔除。
  ⚠ 不要再改回「绝对 dBFS 门槛」（如「紧邻帧 <= -40 dBFS」）：它砍到 249 处，会误杀
    3 处已确认的真样本（紧邻帧 -38.9/-39.7，恰在停顿线上方一点点），且各章响度差
    4 dB（-20.2 / -16.3），绝对门槛会按章偏置。
  --min-long-ms 调大更保守，调小更全；--delta-db 调 Δ 门槛（-999 关闭该判据）。

为什么不用「电平够轻」筛 A 型候选：噪声包 RMS 是 -28 dBFS，语音的弱音节、气声
同样落在 -35 ~ -25 dBFS，两个维度完全重叠 —— 收紧就漏检目标，放宽就放进
几百处语音擦音。所以骨架必须建在「孤立 + 短促」上。

为什么用时长当 B 型的刀：最短的「整词被停顿包围」语音段是 0.36 s（"义工。"
0.44 s），而实测 artifact 最长 0.28 s —— 时长把两者干净分开。

--gap-ms 的取舍（A 型，实测本样本）
  200（保守）  只报句间停顿里的包，段末/句末那些会漏
  100（默认）  三种形态都报：句中停顿里的、段末拼接静音前的、文件末尾的
  80 以下      更全，误报开始上升，需人工过一遍清单

--delta-db 的取舍（两型共用，实测 18 段标注）
   -6  更全，把 3 处假样本里的 2 处也放进清单
    0（默认）  15 处真样本全报、3 处假样本全排除（两簇间的空档正中间）
   +3  更保守，接近门槛的那处真样本（Δ +6.8）仍在，但余量只剩 3.8 dB
  -999  关闭该判据，退回「只看自相关」的旧行为

写零策略（--apply / --in-place）
  以检出区间为核，向两侧静音区扩展最多 --pad-ms，遇到高于 -40 dBFS 的帧即停，
  然后整段置零（边界已落在静音里，无可闻跳变）。
    --apply     另存副本，原文件一律不动（默认 <name>.fixed.wav，可用 --out 指定）
    --in-place  覆盖原文件：先写临时文件 → 备份 <name>.orig.wav → 原子替换，
                任何一步失败原文件都完好；--no-backup 可跳过备份（不可回滚）
  两型都只对「疑似 / 长块」动手，「弱·周期」「弱·电平」一律不动。

日志
  每次运行把检出位置追加写进 ./scan_tts_artifacts.log（--log 换路径，--no-log 关闭）。
  内容是纯文本、带时间戳，记录每个文件的结论、每处撕音的 m:ss 位置与判据数值。

已知局限
  · 自相关在 70–350 Hz（基频）范围取值。若 artifact 是**纯窄带单音**，其谐波
    可能在该范围对齐而虚高（实测人造 3.5 kHz 纯音可到 0.96）；真实 artifact 是
    宽带噪声，不受影响（实测 0.066–0.137）。
  · 「弱·周期」和「弱·电平」项不一定是问题，只是与疑似共享了外形；
    --show-weak 能看全，被 Δ 判据砍掉的不会静默消失。
  · B 型（长块型）与正常长擦音在频谱上不可分，靠 Δ 邻域判据分开 —— 若某处真以
    长擦音收尾、又恰好比 ±60 ms 邻域响，仍会被误报（18 段标注里未出现）。
""",
    )
    ap.add_argument("inputs", nargs="+", help="WAV 文件或目录")
    ap.add_argument("-r", "--recursive", action="store_true", help="目录递归查找 *.wav")
    ap.add_argument("--json", metavar="PATH", help="输出 JSON 报告（'-' 表示 stdout）")
    ap.add_argument("--apply", action="store_true", help="生成修补副本（原文件不动）")
    ap.add_argument("--out", metavar="PATH", help="修补副本路径（仅单个输入时可用，默认 <name>.fixed.wav）")
    ap.add_argument(
        "--in-place",
        action="store_true",
        help=f"修补后覆盖原文件（先备份为 <name>{DEF_BACKUP_SUFFIX}，原子替换；与 --out 互斥）",
    )
    ap.add_argument("--no-backup", action="store_true", help="配合 --in-place：不生成备份（不可回滚，慎用）")
    ap.add_argument("--pad-ms", type=float, default=DEF_PAD_MS, help=f"写零向两侧扩展上限，默认 {DEF_PAD_MS:g}")
    ap.add_argument(
        "--gap-ms", type=float, default=DEF_GAP_MS, help=f"候选前后各需的空隙长度，默认 {DEF_GAP_MS:g}（调小=更全）"
    )
    ap.add_argument(
        "--min-dur-ms", type=float, default=DEF_MIN_DUR_MS, help=f"候选最小持续时长，默认 {DEF_MIN_DUR_MS:g}"
    )
    ap.add_argument(
        "--min-long-ms",
        type=float,
        default=DEF_LONG_MIN_MS,
        help=f"长块型候选的时长下限，默认 {DEF_LONG_MIN_MS:g}（调小=更全、误报上升）",
    )
    ap.add_argument("--no-long", action="store_true", help="关闭长块型判据，只报停顿型（首版行为）")
    ap.add_argument(
        "--delta-db",
        type=float,
        default=DEF_DELTA_DB,
        help=f"候选电平须比 ±60 ms 邻域最响帧高出多少 dB，默认 {DEF_DELTA_DB:g}"
             f"（调小=更全，-999 关闭该判据）",
    )
    ap.add_argument("--list-boundaries", action="store_true", help="展开真零区段清单")
    ap.add_argument("--show-weak", action="store_true", help="清单同时列出「弱」项（默认只列疑似）")
    ap.add_argument("--fail-on-hit", action="store_true", help="有「疑似」时返回码 1")
    ap.add_argument("--log", metavar="PATH", default=DEF_LOG_NAME, help=f"日志文件（追加），默认 ./{DEF_LOG_NAME}")
    ap.add_argument("--no-log", action="store_true", help="不写日志文件")
    return ap


def strip_private(rep):
    """剥掉内部字段（_i0/_i1 等），JSON 报告里不该出现。"""
    out = {k: v for k, v in rep.items() if not k.startswith("_")}
    out["candidates"] = [
        {k: v for k, v in c.items() if not k.startswith("_")} for c in rep["candidates"]
    ]
    return out


def main(argv=None):
    args = build_parser().parse_args(argv)
    files = collect_inputs(args.inputs, args.recursive)
    if not files:
        print("没有可处理的 .wav 输入。", file=sys.stderr)
        return 2
    if args.out and len(files) != 1:
        print("--out 只适用于单个输入文件。", file=sys.stderr)
        return 2
    if args.in_place and args.out:
        print("--in-place 与 --out 互斥：覆盖原文件时没有第二个输出路径。", file=sys.stderr)
        return 2
    if args.no_backup and not args.in_place:
        print("--no-backup 只在 --in-place 下有意义。", file=sys.stderr)
        return 2

    quiet = args.json == "-"  # --json - 时 stdout 只放 JSON，报告文本让路（可管道解析）
    log = RunLog(None if args.no_log else args.log)
    invoked = " ".join(sys.argv[1:] if argv is None else argv)
    log.add("=" * 88)
    log.add(f"scan_tts_artifacts  {invoked}")

    jsons = []
    hit_total = 0
    fix_failures = 0
    for f in files:
        try:
            rep = analyze(f, gap_ms=args.gap_ms, min_dur_ms=args.min_dur_ms,
                          min_long_ms=args.min_long_ms, use_long=not args.no_long,
                          delta_db=args.delta_db)
        except WavError as e:
            print(f"[跳过] {f.name}：{e}", file=sys.stderr)
            log.add(f"跳过  {f.name}：{e}")
            log.commit()
            continue

        if not quiet:
            print_report(rep, args)
        hit_total += sum(1 for c in rep["candidates"] if c["verdict"] == "hit")

        fix_info = None
        changes = []
        if args.in_place or args.apply:
            if args.in_place:
                out = f
            else:
                out = Path(args.out) if args.out else f.with_suffix(".fixed.wav")
            try:
                changes, fix_info = apply_fix(rep, out, args.pad_ms, backup=not args.no_backup)
            except FixError as e:
                fix_failures += 1
                print(f"[修补失败] {f.name}：{e}", file=sys.stderr)
                log.add(f"处理  修补失败，原文件未改动\n{e}")
                log.commit()
                jsons.append(strip_private(rep))
                continue
            if not quiet:
                print("修补   " + fix_summary(fix_info, changes))
        elif rep["candidates"] and not quiet:
            print("提示   加 --apply 另存副本，或 --in-place 覆盖原文件")

        log_report(log, rep, fix_info, changes)
        log.commit()
        jsons.append(strip_private(rep))

    if args.json:
        payload = jsons[0] if len(jsons) == 1 else jsons
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if quiet:
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
            print(f"JSON 报告写入 {args.json}")

    if log.path is not None:
        log.add(f"完成  {len(files)} 个文件 / {hit_total} 处疑似 / 修补失败 {fix_failures}")
        log.commit()
        if not quiet:
            print(f"日志   {log.path}（追加）")

    if fix_failures:
        return 2
    return 1 if (args.fail_on_hit and hit_total) else 0


if __name__ == "__main__":
    sys.exit(main())
