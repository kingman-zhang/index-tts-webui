#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音频「忽大忽小」批量诊断（章节级响度一致性检查）。

用途
    批量检查一批成品音频（例：第十二章 ~ 第十六章）的**段间电平是否一致**，
    回答「有没有忽大忽小」。查两类问题：
      ① 同一文件内 —— 段落电平散布大 / 相邻段突变 / 前后半程漂移 / 分成两批；
      ② 文件之间   —— 某个文件整体偏响或偏轻，某章整体和别章差一截。

做法
    1. 20 ms 帧算 RMS，自适应门限（P95 − 30 dB）分出语音帧；
    2. 有声段之间停顿 ≥ 150 ms 就切开。本链路合成段之间是**数字零**
       （实测中位 0.200 s；与 scan_tts_artifacts 的拼接指纹交叉验证，
       覆盖 22/23、3/3）⇒ 切出的「语音段」贴近合成段。TTS 段内的停顿
       也会被切得更细，对「段间音量一致性」只会更灵敏，不碍事；
    3. 低于文件语音中位数 12 dB 的段判为「低电平残片」并剔出统计 ——
       停顿里 0.3 s 上下、低 16~26 dB 的微弱碎片不是语音，留着会把散布/突变
       彻底带偏（实测把 8.8 s 的文件抬到 18 dB 散布）。残片单独列出，不丢；
    4. 再做文件内 / 章节内 / 章节间三层统计。

口径（务必知道）
    · 全程是 **未加权 RMS（dBFS）**，不是 ITU-R BS.1770 积分响度。
      做「一致性 / 相对比较」够准；**别把这里的数当 LUFS 用**。
      要绝对 LUFS，用同目录的 diagnose_segment_loudness.py（依赖 ffmpeg）。
    · 只读文件，不修改任何音频。

用法
    python3 diagnose_loudness_consistency.py ~/Downloads/第十二章 ~/Downloads/第十三章
    python3 diagnose_loudness_consistency.py -r ~/Downloads --json out.json --csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:  # 报错要说清「怎么办」
    sys.exit(
        "缺少 numpy。本机 venv 已带，用它对：\n"
        "  /Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/pip install numpy\n"
        "或改用同目录的 diagnose_segment_loudness.py（只需 ffmpeg）。"
    )

# ---------------------------------------------------------------- 默认参数
DEF_FRAME_MS = 20.0        # 帧长
DEF_GAP_MS = 150.0         # 静音 ≥ 此值即切段（小于链路 200 ms 的段间零）
DEF_MIN_UTT_MS = 250.0     # 短于此值的段视作碎片丢弃
DEF_VAD_DROP_DB = 30.0     # 语音门限 = P95 帧电平 − 此值
DEF_FLOOR_DBFS = -60.0     # 语音门限的下限
DEF_FLAG_SPREAD_DB = 6.0   # 段间散布告警线（P90 − P10）
DEF_FLAG_JUMP_DB = 8.0     # 相邻段突变告警线
DEF_FLAG_DRIFT_DB = 3.0    # 前后半程漂移告警线
DEF_FLAG_FILE_DEV_DB = 2.0 # 文件整体偏离章节中位数的告警线
CLUSTER_MIN_GAP_DB = 3.0   # 疑似「两批」的最小间距
CLUSTER_MIN_FRAC = 0.15    # 两批各自至少要占的段数比例
# 低于「文件语音中位数」此值 ⇒ 判为「低电平残片」，剔出统计但单独列出。
# 实测：真实语音段都落在中位数 ±5 dB 内；标出的三例分别低 25.9 / 26.0 / 16.6 dB，
# 是停顿里 0.26~0.46 s 的微弱碎片（非语音）。设 12 dB 有足够余量、零误伤。
DEF_RESIDUE_DROP_DB = 12.0

FULL_SCALE = 32768.0
AUDIO_EXT = {".wav", ".WAV"}


# ---------------------------------------------------------------- 读取
def read_mono(path: Path):
    """读 wav → (float32 单声道样本, 采样率)。多声道取均值。"""
    with wave.open(str(path), "rb") as w:
        sw = w.getsampwidth()
        nch = w.getnchannels()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())

    if sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) * 256.0
    elif sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 0x800000, v - 0x1000000, v)
        a = (v >> 8).astype(np.float32)
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 65536.0
    else:
        raise ValueError(f"不支持的采样位宽 {sw * 8} bit：{path}")

    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a, sr


def frame_rms(x, sr: int, frame_ms: float):
    """非重叠帧 RMS（线性），返回 (rms 数组, 每帧样本数)。分块算，省内存。"""
    fl = max(1, int(round(sr * frame_ms / 1000.0)))
    n = len(x) // fl
    if n <= 0:
        return np.empty(0, dtype=np.float64), fl
    out = np.empty(n, dtype=np.float64)
    step = 1 << 18
    for s in range(0, n, step):
        e = min(n, s + step)
        blk = x[s * fl:e * fl].astype(np.float64).reshape(-1, fl)
        out[s:e] = np.sqrt((blk * blk).mean(axis=1))
    return out, fl


def to_db(rms: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(rms, 1e-12) / FULL_SCALE)


# ---------------------------------------------------------------- 切段
def split_utterances(levels_db, hop_s: float, gap_s: float, min_utt_s: float,
                     drop_db: float, floor_dbfs: float):
    """返回 (语音段列表[(帧起, 帧止)], 用的门限 dB)。"""
    if levels_db.size == 0:
        return [], None

    ref = float(np.percentile(levels_db, 95))
    thr = max(ref - drop_db, floor_dbfs)
    active = levels_db > thr

    runs = []
    n = int(active.size)
    i = 0
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < n and active[j]:
            j += 1
        runs.append([i, j])
        i = j

    merged = []
    for r in runs:
        if merged and (r[0] - merged[-1][1]) * hop_s < gap_s:
            merged[-1][1] = r[1]
        else:
            merged.append(r)

    kept = [tuple(r) for r in merged if (r[1] - r[0]) * hop_s >= min_utt_s]
    return kept, thr


def utt_level(x, sr: int, i0: int, i1: int, fl: int):
    """一个语音段的 RMS 电平（dBFS）。"""
    seg = x[i0 * fl:min(i1 * fl, len(x))].astype(np.float64)
    if seg.size == 0:
        return None
    rms = math.sqrt(float((seg * seg).mean()))
    if rms <= 0.0:
        return None
    return 20.0 * math.log10(rms / FULL_SCALE)


def find_cluster(levels) -> tuple[float, float, float] | None:
    """在排序后的段电平里找最大的「两批」间隔。返回 (间距, 低批均值, 高批均值)。"""
    s = np.sort(np.asarray(levels, dtype=np.float64))
    n = s.size
    if n < 8:
        return None
    best = None
    for i in range(n - 1):
        gap = float(s[i + 1] - s[i])
        if gap < CLUSTER_MIN_GAP_DB:
            continue
        if (i + 1) < n * CLUSTER_MIN_FRAC or (n - i - 1) < n * CLUSTER_MIN_FRAC:
            continue
        if best is None or gap > best[0]:
            best = (gap, float(s[:i + 1].mean()), float(s[i + 1:].mean()))
    return best


# ---------------------------------------------------------------- 单文件
def analyze_file(path: Path, label: str, cfg) -> dict:
    x, sr = read_mono(path)
    dur = len(x) / sr if sr else 0.0
    if dur <= 0:
        raise ValueError("空文件")

    levels_rms, fl = frame_rms(x, sr, cfg.frame_ms)
    levels_db = to_db(levels_rms)
    hop_s = fl / sr

    runs, thr = split_utterances(
        levels_db, hop_s, cfg.gap_ms / 1000.0, cfg.min_utt_ms / 1000.0,
        cfg.vad_drop_db, cfg.floor_dbfs,
    )

    utts = []
    for i0, i1 in runs:
        lv = utt_level(x, sr, i0, i1, fl)
        if lv is None or lv < cfg.floor_dbfs:
            continue
        utts.append({
            "t": round(i0 * hop_s, 3),
            "dur": round((i1 - i0) * hop_s, 3),
            "db": round(lv, 2),
        })

    # 两遍：先按「全段中位数」挑出低电平残片 —— 停顿里的微弱碎片不是语音，
    # 留在统计里会把散布和突变彻底带偏（实测能把 8.8 s 的文件抬到 18 dB 散布）。
    residues: list = []
    if utts and cfg.residue_drop_db > 0:
        ref = float(np.median([u["db"] for u in utts]))
        keep, drop = [], []
        for u in utts:
            (drop if u["db"] <= ref - cfg.residue_drop_db else keep).append(u)
        if keep:
            residues, utts = drop, keep

    res = {
        "label": label,
        "path": str(path),
        "sr": int(sr),
        "dur": round(dur, 2),
        "n_utt": len(utts),
        "n_residue": len(residues),
        "speech_dur": round(sum(u["dur"] for u in utts), 2),
        "thr_dbfs": None if thr is None else round(thr, 1),
        "median": None, "p10": None, "p90": None, "min": None, "max": None,
        "spread": None, "max_jump": None, "max_jump_at": None, "drift": None,
        "cluster": None, "flags": [], "utts": utts,
    }
    if not utts:
        res["flags"] = ["未检出语音段"]
        return res

    lv = np.array([u["db"] for u in utts], dtype=np.float64)
    res["median"] = round(float(np.median(lv)), 2)
    res["p10"] = round(float(np.percentile(lv, 10)), 2)
    res["p90"] = round(float(np.percentile(lv, 90)), 2)
    res["min"] = round(float(lv.min()), 2)
    res["max"] = round(float(lv.max()), 2)
    spread = float(res["p90"] - res["p10"])
    res["spread"] = round(spread, 2)

    if lv.size > 1:
        jumps = np.abs(np.diff(lv))
        k = int(jumps.argmax())
        res["max_jump"] = round(float(jumps[k]), 2)
        res["max_jump_at"] = utts[k + 1]["t"]

    if lv.size >= 6:
        h = lv.size // 2
        res["drift"] = round(float(np.median(lv[h:]) - np.median(lv[:h])), 2)

    cl = find_cluster(lv)
    if cl:
        res["cluster"] = {"gap": round(cl[0], 2),
                          "low": round(cl[1], 2), "high": round(cl[2], 2)}

    flags = []
    if spread > cfg.flag_spread_db:
        flags.append(f"段间散布 {spread:.1f} dB")
    if res["max_jump"] is not None and res["max_jump"] > cfg.flag_jump_db:
        flags.append(f"相邻段突变 {res['max_jump']:.1f} dB")
    if res["drift"] is not None and abs(res["drift"]) > cfg.flag_drift_db:
        flags.append(f"前后半程漂移 {res['drift']:+.1f} dB")
    if cl and cl[0] >= CLUSTER_MIN_GAP_DB:
        flags.append(f"疑似两批（差 {cl[0]:.1f} dB）")
    res["flags"] = flags

    res.pop("utts")  # 明细按需再取
    res["_utts"] = utts
    res["_residues"] = residues
    return res


# ---------------------------------------------------------------- 汇总
def collect_files(inputs, recursive: bool):
    files = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            files += sorted(q for q in it if q.suffix in AUDIO_EXT and q.is_file())
        elif p.is_file():
            files.append(p)
        else:
            print(f"⚠ 跳过（不存在）：{p}", file=sys.stderr)
    # 去重（保持顺序）
    seen, out = set(), []
    for f in files:
        key = f.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def chapter_of(path: Path, root_map) -> str:
    return root_map.get(path, path.parent.name)


def fmt_flags(flags):
    return "；".join(flags) if flags else ""


def print_report(rows, chapters, cfg, elapsed: float, n_files: int, total_dur: float):
    line = "=" * 92
    print(line)
    print("音频响度一致性诊断（段间「忽大忽小」）")
    print(line)
    print(f"文件 {n_files} 个 / 总时长 {total_dur / 60:.1f} 分钟 / 扫描 {elapsed:.0f} s")
    print(f"切段：帧 {cfg.frame_ms:g} ms，静音 ≥ {cfg.gap_ms:g} ms 断开，"
          f"最短语音段 {cfg.min_utt_ms:g} ms")
    print(f"告警线：散布 > {cfg.flag_spread_db:g} dB / 突变 > {cfg.flag_jump_db:g} dB / "
          f"漂移 > {cfg.flag_drift_db:g} dB / 文件偏离章节中位数 > {cfg.flag_file_dev_db:g} dB")

    # ---- 章节总览
    print()
    print("【章节总览】")
    print(f"{'章节':<10} {'文件':>4} {'时长(分)':>9} {'语音段':>7} "
          f"{'中位dBFS':>9} {'P10':>7} {'P90':>7} {'散布中位':>9} {'告警文件':>8}")
    print("-" * 92)
    for name, ch in chapters.items():
        spread_med = ch["spread_med"]
        print(f"{name:<10} {ch['nfiles']:>4} {ch['dur'] / 60:>9.1f} {ch['n_utt']:>7} "
              f"{ch['median']:>9.2f} {ch['p10']:>7.2f} {ch['p90']:>7.2f} "
              f"{(f'{spread_med:.1f}' if spread_med is not None else '—'):>9} "
              f"{ch['n_flag']:>8}")

    # ---- 跨章对比
    if len(chapters) > 1:
        print()
        print("【跨章对比】（各章语音段电平中位数）")
        base = min(ch["median"] for ch in chapters.values())
        for name, ch in sorted(chapters.items(), key=lambda kv: kv[1]["median"]):
            dev = ch["median"] - base
            bar = "█" * max(1, int(round(dev * 3)))
            print(f"  {name:<8} {ch['median']:>8.2f} dBFS  相对最轻章 +{dev:>5.1f} dB  {bar}")

    # ---- 问题文件
    bad = [r for r in rows if r["flags"]]
    print()
    print(f"【需复核的文件】{len(bad)} / {len(rows)}")
    if bad:
        bad.sort(key=lambda r: (-len(r["flags"]), r["median"]))
        print(f"{'文件':<46} {'时长s':>7} {'段数':>5} {'中位':>7} {'散布':>6} "
              f"{'突变':>6} {'漂移':>7}  说明")
        print("-" * 92)
        for r in bad[: cfg.top]:
            name = Path(r["path"]).name
            rel = r["label"]
            show = rel if len(rel) <= 44 else "…" + rel[-43:]
            print(f"{show:<46} {r['dur']:>7.1f} {r['n_utt']:>5} {r['median']:>7.2f} "
                  f"{(r['spread'] if r['spread'] is not None else 0):>6.2f} "
                  f"{(r['max_jump'] if r['max_jump'] is not None else 0):>6.2f} "
                  f"{(r['drift'] if r['drift'] is not None else 0):>+7.2f}  "
                  f"{fmt_flags(r['flags'])}")
        if len(bad) > cfg.top:
            print(f"  … 另有 {len(bad) - cfg.top} 个，见 --json / --csv 全量输出")
    else:
        print("  （无）")

    # ---- 低电平残片（不是语音，已剔出统计）
    res_files = [r for r in rows if r.get("n_residue")]
    if res_files:
        tot = sum(r["n_residue"] for r in res_files)
        print()
        print(f"【低电平残片】{tot} 处 / {len(res_files)} 个文件"
              f"（低于文件中位数 {cfg.residue_drop_db:g} dB，已剔出统计）")
        print(f"{'文件':<46} {'处数':>4} {'最低dBFS':>9} {'最长s':>7}")
        print("-" * 92)
        res_files.sort(key=lambda r: -r["n_residue"])
        for r in res_files[: cfg.top]:
            show = r["label"] if len(r["label"]) <= 44 else "…" + r["label"][-43:]
            lo = min(x["db"] for x in r["_residues"])
            mx = max(x["dur"] for x in r["_residues"])
            print(f"{show:<46} {r['n_residue']:>4} {lo:>9.1f} {mx:>7.2f}")
        if len(res_files) > cfg.top:
            print(f"  … 另有 {len(res_files) - cfg.top} 个文件，见 --json")
        if cfg.detail:
            print()
            print("  残片明细：")
            for r in res_files[: cfg.top]:
                for x in r["_residues"]:
                    print(f"    {Path(r['path']).name}  t={x['t']:>8.2f}s  "
                          f"dur={x['dur']:>5.2f}s  {x['db']:>8.2f} dBFS")

    # ---- 最大突变排行
    jumps = [r for r in rows if r["max_jump"] is not None]
    if jumps:
        jumps.sort(key=lambda r: -r["max_jump"])
        print()
        print(f"【最大相邻段突变 Top {min(cfg.top, len(jumps))}】")
        for r in jumps[: cfg.top]:
            print(f"  {r['max_jump']:>6.2f} dB  @ {r['max_jump_at']:>8.2f} s  "
                  f"{Path(r['path']).name}  ({r['label']})")

    # ---- 直方图
    if cfg.hist:
        allv = []
        for r in rows:
            allv += [u["db"] for u in r["_utts"]]
        if allv:
            print()
            print("【全部语音段电平分布】")
            lo, hi = math.floor(min(allv)), math.ceil(max(allv))
            bins = {}
            for v in allv:
                k = math.floor(v)
                bins[k] = bins.get(k, 0) + 1
            mx = max(bins.values())
            for b in range(lo, hi + 1):
                c = bins.get(b, 0)
                bar = "█" * int(round(c * 40 / mx)) if mx else ""
                print(f"  {b:>4} ~ {b + 1:>4} dBFS  {c:>5}  {bar}")

    # ---- 明细
    if cfg.detail and bad:
        print()
        print("【告警文件的段级明细】")
        for r in bad[: cfg.top]:
            print()
            print(f"— {r['label']}  （{r['n_utt']} 段，中位 {r['median']:.2f} dBFS）")
            med = r["median"]
            for i, u in enumerate(r["_utts"], 1):
                d = u["db"] - med
                mark = "  <<<" if abs(d) > 3.0 else ""
                print(f"    {i:>4}  {u['t']:>8.2f}s  {u['dur']:>6.2f}s  "
                      f"{u['db']:>8.2f} dBFS  {d:>+6.2f}{mark}")

    # ---- 判读
    print()
    print(line)
    if not bad:
        print("结论  ✓ 未发现段间电平异常（所有文件都在告警线内）")
    else:
        kinds = {}
        for r in bad:
            for f in r["flags"]:
                kinds[f.split(" ")[0]] = kinds.get(f.split(" ")[0], 0) + 1
        summary = "，".join(f"{k} {v} 个" for k, v in kinds.items())
        print(f"结论  ⚠ {len(bad)} 个文件需复核（{summary}）")
    print()
    print("判读要点（被标出 ≠ 一定有缺陷）")
    print("  · 自然语音相邻句本身就有 ±2~3 dB 起伏，散布 6 dB 以内属正常。")
    print("  · 散布大 / 突变大        → 多半是增益不一致，挑那几处直接听。")
    print(f"  · 疑似两批（相差 ≥{CLUSTER_MIN_GAP_DB:g} dB）→ 典型的「一段归一了、一段没归一」，最该先查。")
    print(f"  · 文件偏离章节中位数 >{cfg.flag_file_dev_db:g} dB → 该文件是异类，先单独听它。")
    print("  · 这是 RMS 相对电平，不是 LUFS；绝对值别当响度规格用。")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="音频「忽大忽小」批量诊断（段间响度一致性）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("inputs", nargs="+", help="音频文件或目录")
    ap.add_argument("-r", "--recursive", action="store_true", help="目录递归查找 .wav")
    ap.add_argument("--json", metavar="PATH", help="写 JSON 报告")
    ap.add_argument("--csv", metavar="PATH", help="写 CSV（每文件一行）")
    ap.add_argument("--frame-ms", type=float, default=DEF_FRAME_MS)
    ap.add_argument("--gap-ms", type=float, default=DEF_GAP_MS,
                    help=f"静音 ≥ 此值即切段（默认 {DEF_GAP_MS:g}）")
    ap.add_argument("--min-utt-ms", type=float, default=DEF_MIN_UTT_MS)
    ap.add_argument("--vad-drop-db", type=float, default=DEF_VAD_DROP_DB,
                    help=f"语音门限 = P95 帧电平 − 此值（默认 {DEF_VAD_DROP_DB:g}）")
    ap.add_argument("--floor-dbfs", type=float, default=DEF_FLOOR_DBFS)
    ap.add_argument("--flag-spread-db", type=float, default=DEF_FLAG_SPREAD_DB)
    ap.add_argument("--flag-jump-db", type=float, default=DEF_FLAG_JUMP_DB)
    ap.add_argument("--flag-drift-db", type=float, default=DEF_FLAG_DRIFT_DB)
    ap.add_argument("--flag-file-dev-db", type=float, default=DEF_FLAG_FILE_DEV_DB)
    ap.add_argument("--residue-drop-db", type=float, default=DEF_RESIDUE_DROP_DB,
                    help=f"低于文件语音中位数此值判为残片、剔出统计（默认 {DEF_RESIDUE_DROP_DB:g}，0 关闭）")
    ap.add_argument("--top", type=int, default=15, help="问题清单显示条数（默认 15）")
    ap.add_argument("--detail", action="store_true", help="打印告警文件的段级明细")
    ap.add_argument("--hist", action="store_true", help="打印全部语音段电平直方图")
    ap.add_argument("--fail-on-flag", action="store_true",
                    help="有文件告警时退出码返回 1（默认始终 0）")
    cfg = ap.parse_args()

    files = collect_files(cfg.inputs, cfg.recursive)
    if not files:
        sys.exit("没有找到任何 .wav 文件")

    # 章节归属：以「命令行里给的目录」为根
    root_map = {}
    for raw in cfg.inputs:
        p = Path(raw).expanduser()
        if p.is_dir():
            for f in (p.rglob("*") if cfg.recursive else p.glob("*")):
                if f.suffix in AUDIO_EXT and f.is_file():
                    root_map.setdefault(f, p.name)

    rows, chapters = [], {}
    total_dur = 0.0
    t0 = time.time()
    for f in files:
        try:
            r = analyze_file(f, chapter_of(f, root_map) + "/" + f.name, cfg)
        except Exception as exc:  # 单个文件坏掉不该拖垮整批
            print(f"⚠ 跳过 {f.name}：{exc}", file=sys.stderr)
            continue
        r["chapter"] = root_map.get(f, f.parent.name)
        rows.append(r)
        total_dur += r["dur"]

    # 文件整体偏离章节中位数
    for name in {r["chapter"] for r in rows}:
        grp = [r for r in rows if r["chapter"] == name]
        meds = [r["median"] for r in grp if r["median"] is not None]
        if not meds:
            continue
        ch_med = float(np.median(meds))
        for r in grp:
            if r["median"] is None:
                continue
            dev = r["median"] - ch_med
            if abs(dev) > cfg.flag_file_dev_db:
                r["flags"].append(f"整体偏离本章 {dev:+.1f} dB")

    # 章节汇总
    for name in dict.fromkeys(r["chapter"] for r in rows):
        grp = [r for r in rows if r["chapter"] == name]
        lv = [r["median"] for r in grp if r["median"] is not None]
        if not lv:
            continue
        allc = lv  # 文件中位数的分布
        spreads = [r["spread"] for r in grp if r["spread"] is not None]
        chapters[name] = {
            "nfiles": len(grp),
            "dur": sum(r["dur"] for r in grp),
            "n_utt": sum(r["n_utt"] for r in grp),
            "median": round(float(np.median(lv)), 2),
            "p10": round(float(np.percentile(lv, 10)), 2),
            "p90": round(float(np.percentile(lv, 90)), 2),
            "spread_med": round(float(np.median(spreads)), 2) if spreads else None,
            "n_flag": sum(1 for r in grp if r["flags"]),
        }

    elapsed = time.time() - t0
    print_report(rows, chapters, cfg, elapsed, len(rows), total_dur)

    # ---- 落盘
    if cfg.json:
        allv = [u["db"] for r in rows for u in r["_utts"]]
        hist: dict = {}
        for v in allv:
            k = math.floor(v)
            hist[k] = hist.get(k, 0) + 1
        payload = {
            "histogram": {str(k): hist[k] for k in sorted(hist)},
            "inputs": [str(Path(i).expanduser()) for i in cfg.inputs],
            "n_files": len(rows),
            "total_dur": round(total_dur, 2),
            "elapsed_s": round(elapsed, 1),
            "config": {
                "frame_ms": cfg.frame_ms, "gap_ms": cfg.gap_ms,
                "min_utt_ms": cfg.min_utt_ms, "vad_drop_db": cfg.vad_drop_db,
                "floor_dbfs": cfg.floor_dbfs,
                "flag_spread_db": cfg.flag_spread_db,
                "flag_jump_db": cfg.flag_jump_db,
                "flag_drift_db": cfg.flag_drift_db,
                "flag_file_dev_db": cfg.flag_file_dev_db,
            },
            "chapters": chapters,
            "files": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        }
        if cfg.detail:
            payload["utterances"] = {r["path"]: r["_utts"] for r in rows}
            payload["residues"] = {r["path"]: r["_residues"] for r in rows if r["_residues"]}
        Path(cfg.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nJSON → {cfg.json}")

    if cfg.csv:
        with open(cfg.csv, "w", newline="", encoding="utf-8-sig") as fh:
            wr = csv.writer(fh)
            wr.writerow(["chapter", "file", "path", "dur_s", "n_utt", "n_residue",
                         "speech_s", "median_dbfs", "p10", "p90", "spread_db",
                         "max_jump_db", "max_jump_at_s", "drift_db", "flags"])
            for r in rows:
                wr.writerow([
                    r["chapter"], Path(r["path"]).name, r["path"], r["dur"],
                    r["n_utt"], r["n_residue"], r["speech_dur"], r["median"],
                    r["p10"], r["p90"], r["spread"], r["max_jump"],
                    r["max_jump_at"], r["drift"], fmt_flags(r["flags"]),
                ])
        print(f"CSV  → {cfg.csv}")

    if cfg.fail_on_flag and any(r["flags"] for r in rows):
        sys.exit(1)


if __name__ == "__main__":
    main()
