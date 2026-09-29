#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数字读法真机对照探针：**阿拉伯数字** vs **数值汉字** vs **逐位汉字**。

用途：用户反馈「某个数字被读成了逐位 / 读成怪音」时，判定责任在哪一层、
以及**修法是否真的有效**，全程不依赖人耳。

为什么需要这个探针（2026-09-29，`230 倍` 事件）
=================================================
IndexTTS 的 bpe 词表（12000）里**没有阿拉伯数字**，实测：

    "230倍"   -> ['▁', '230', '倍']        其中 230 是 <unk>(id=2)
    "230 倍"  -> ['▁', '230', '▁', '倍']   同样 unk
    "两百三十倍" -> ['▁','两','百','三','十','倍']   无 unk

⇒ **阿拉伯数字只要没被换成汉字，进模型就是 unk，模型在该位置自由发挥。**
而「数字→汉字」本该由引擎自带的文本归一化（TN）完成。所以数字读法不对，
只可能是：A. TN 没在链路上/没生效（阿拉伯数字进了模型）；B. TN 生效但读法不同。

**本探针用时长把这个区分开。** 关键：`230倍` 这个串，读法不同则音节数不同：

| 读法 | 汉字 | 音节 |
|---|---|---|
| 数值（TN 正常） | 两百三十倍 | 5 |
| 逐位（unk 自由发挥） | 二三零倍 | 4 |

差 1 个音节 ≈ 0.13s。用**总时长**量不出来（噪声 ±0.17s），必须用
**去静音后的有效时长** —— 总时长里含的头尾静音随采样抖动，把 0.13s 的信号淹没。

⚠️ 判据纪律（2026-09-28 教训）：**只有两组区间完全不重叠时，时长才算证据。**
重叠就只输出「不可判」，别硬下结论。每变体至少重复 3~4 次。

已用本探针得到的结论（2026-09-29，art 引擎，各 4 次，`邻家男孩00.mp3`）
------------------------------------------------------------------
| 送入文本 | 有效时长 | 音节核 | 判定 |
|---|---|---|---|
| `两百三十倍`（数值基准） | 0.85–0.99s | 9,8,11,8 | 5 音节 |
| `二三零倍`（逐位基准） | 0.74–0.79s | 7,8,9,8 | 4 音节 |
| **`230 倍`（带空格）** | **0.76–0.79s** | 7,8,8,7 | **= 逐位，与数值组完全不重叠** |
| **`230倍`（无空格）** | **0.80–0.86s** | 7,7,9,8 | **= 数值，与逐位组不重叠** |

⇒ **唯一变量是「数字与量词之间的那个空格」。** 带空格 → 服务端 TN 静默失效 →
阿拉伯数字原样进模型 → unk → 念成「二三零」。这与年份的坑同源
（`2011 年` 也被 TN 退化成数值读，见 `app/year_norm.py` 顶部说明）。

用法：
    python3 tools/probe_number_reading.py                    # digits 组，art，4 次
    python3 tools/probe_number_reading.py --suite context     # 同一语境下的修复前后对照
    python3 tools/probe_number_reading.py --engine 302ai --reps 6
    python3 tools/probe_number_reading.py --only 01_arabic_space,03_value

成本：art 按 0.001 元/s。digits 组（6 变体 × 4 次，短句为主）≈ ¥0.1、约 10 分钟。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import math
import struct
import sys
import wave
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND = TOOLS_DIR.parent / "webui-backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(TOOLS_DIR))

# 复用既有探针的 .env 加载 / 时长探测 / 引擎构造（避免多份实现漂移）
from probe_name_middot import build_engine, load_env, probe_duration  # noqa: E402

# ── 变体组 ────────────────────────────────────────────────────────────────
# role: value / digit 是两个时长基准，probe 是待判定的。
# 短句刻意做短：背景词越少，1 个音节的差异越容易量出来。
SUITES: dict[str, list[tuple[str, str, str, str]]] = {
    # 隔离变量：只改数字的写法，句尾量词「倍」
    "digits": [
        ("01_arabic_space", "阿拉伯数字 + 空格（用户原文形态）", "230 倍", "probe"),
        ("02_arabic", "阿拉伯数字（无空格）", "230倍", "probe"),
        ("03_value", "数值汉字（5 音节）", "两百三十倍", "value"),
        ("04_digit", "逐位汉字（4 音节）", "二三零倍", "digit"),
        ("05_user_arabic", "用户整句 · 阿拉伯",
         "美国自二零零五年到二零零七年的飞行安全度是汽车的 230 倍。", "probe"),
        ("06_user_value", "用户整句 · 数值汉字",
         "美国自二零零五年到二零零七年的飞行安全度是汽车的两百三十倍。", "value"),
    ],
    # 同一语境下比「修复前 vs 修复后」，外部效度更好（都带「汽车的」前缀）
    "context": [
        ("01_ctx_space", "同一语境 · 带空格（修复前）", "汽车的 230 倍", "probe"),
        ("02_ctx_nospace", "同一语境 · 删空格（修复后）", "汽车的 230倍", "probe"),
        ("03_ctx_value", "同一语境 · 数值汉字（基准）", "汽车的两百三十倍", "value"),
        ("04_ctx_digit", "同一语境 · 逐位汉字（基准）", "汽车的二三零倍", "digit"),
    ],
}

USER_SENTENCE = "美国自 2005 年至 2007 年的飞行安全度是汽车的 230 倍。"


def wav_profile(path: Path) -> tuple[float, int]:
    """(去静音有效时长, 音节核数)。非 wav / 读不出返回 (0.0, 0)。

    为什么要去静音：总时长含头尾静音，随采样抖动 ±0.1s 以上，会把
    「4 音节 vs 5 音节」这 0.13s 的信号淹没。去掉静音后区间立刻分离。
    音节核 = 能量包络的局部峰（间隔 ≥60ms），作辅助佐证，不作主判据。
    """
    try:
        with wave.open(str(path)) as w:
            ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except Exception:  # noqa: BLE001   （mp3 等非 wav）
        return 0.0, 0
    if sw == 2:
        data = struct.unpack("<%dh" % (len(raw) // 2), raw)
    elif sw == 4:
        data = struct.unpack("<%di" % (len(raw) // 4), raw)
    else:
        return 0.0, 0
    if ch > 1:
        data = data[::ch]
    hop = max(1, sr // 100)                      # 10ms 帧
    frames = []
    for i in range(0, len(data) - hop, hop):
        seg = data[i:i + hop]
        frames.append(math.sqrt(sum(v * v for v in seg) / len(seg)))
    if not frames:
        return 0.0, 0
    peak = max(frames) or 1.0
    thr = peak * 0.06
    voiced = [i for i, v in enumerate(frames) if v > thr]
    if not voiced:
        return 0.0, 0
    eff = (voiced[-1] - voiced[0] + 1) * 0.01
    peaks: list[int] = []
    for i in range(1, len(frames) - 1):
        if frames[i] >= frames[i - 1] and frames[i] > frames[i + 1] and frames[i] > thr * 1.2:
            if not peaks or (i - peaks[-1]) >= 6:
                peaks.append(i)
    return eff, len(peaks)


def span(rows: list[dict], field: str) -> tuple[float, float] | None:
    """取某变体各次的 (min, max)；无有效数据返回 None。"""
    ts = [r[field] for r in rows if r["file"] and r.get(field)]
    if not ts:
        return None
    return min(ts), max(ts)


def verdict(probe: tuple[float, float], value: tuple[float, float],
            digit: tuple[float, float]) -> str:
    """按有效时长区间与两个基准的重叠情况判读。**不重叠才算证据。**"""
    lo, hi = probe
    ov_v = not (hi < value[0] or lo > value[1])
    ov_d = not (hi < digit[0] or lo > digit[1])
    if ov_v and ov_d:
        return f"不可判（与两个基准都重叠；基准间距 {abs(value[0] - digit[1]):.2f}s）"
    if ov_v:
        return "≈ 数值读法（TN 生效，读成「两百三十」）"
    if ov_d:
        return "≈ 逐位读法（阿拉伯数字进了模型 → unk 自由发挥）"
    return "落在两个基准之外（另有读法或语速偏移）"


async def main() -> int:
    ap = argparse.ArgumentParser(description="数字读法真机对照")
    ap.add_argument("--suite", default="digits", choices=sorted(SUITES))
    ap.add_argument("--engine", default="art", choices=["art", "302ai"])
    ap.add_argument("--voice", default=None, help="参考音频（默认挑一个本地音色）")
    ap.add_argument("--reps", type=int, default=4, help="每变体重复次数（默认 4）")
    ap.add_argument("--only", default=None, help="只跑指定变体前缀，逗号分隔")
    ap.add_argument("--out", default=None, help="输出目录")
    args = ap.parse_args()

    load_env(BACKEND / ".env")

    import httpx

    voice_path = args.voice
    if not voice_path:
        for c in (BACKEND / "data/voices/邻家男孩00.mp3",
                  BACKEND / "data/voices/test_voice.wav",
                  BACKEND / "data/voices/my_voice_4.wav"):
            if c.is_file():
                voice_path = str(c)
                break
    if not voice_path or not Path(voice_path).is_file():
        raise SystemExit("找不到参考音频，请用 --voice 指定")

    only = set(args.only.split(",")) if args.only else None
    variants = [v for v in SUITES[args.suite] if not only or v[0] in only]

    out_dir = Path(args.out) if args.out else (
        TOOLS_DIR.parent / "data" / "outputs" / f"number-reading-{args.suite}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"数字读法真机对照 · suite={args.suite}")
    print(f"  用户原句: {USER_SENTENCE}")
    print(f"  引擎={args.engine}  音色={Path(voice_path).name}  重复={args.reps}  输出={out_dir}")
    print("  判据: 数值读法「两百三十倍」5 音节 vs 逐位读法「二三零倍」4 音节，差 ≈0.13s。")
    print("        用**去静音有效时长**（总时长的头尾静音会淹没这 0.13s）。")
    print("        只有区间完全不重叠才算证据（重叠记「不可判」）。")
    print("=" * 78)

    from app.engines.base import SegmentRequest, VoiceRef

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        engine = build_engine(args.engine, client)
        if not await engine.health():
            raise SystemExit(f"引擎 {args.engine} 不可用（缺 Key / 余额？）")

        for key, label, text, role in variants:
            print(f"\n[{args.engine}][{key}] {label}\n    提交: {text!r}", flush=True)
            for rep in range(1, args.reps + 1):
                req = SegmentRequest(text=text,
                                     voice=VoiceRef(local_path=voice_path,
                                                    display_name=Path(voice_path).stem))
                try:
                    audio = await engine.synthesize_segment(req)
                except Exception as exc:  # noqa: BLE001
                    print(f"    r{rep} 失败: {exc}", flush=True)
                    results.append({"engine": args.engine, "key": key, "label": label,
                                    "role": role, "text": text, "rep": rep,
                                    "file": None, "error": str(exc)[:300],
                                    "seconds": 0.0, "eff": 0.0, "peaks": 0, "bytes": 0})
                    continue
                ext = ".mp3" if audio[:3] == b"ID3" or audio[:2] == b"\xff\xfb" else ".wav"
                dst = out_dir / f"{args.engine}_{key}_r{rep}{ext}"
                dst.write_bytes(audio)
                seconds = probe_duration(dst)
                eff, peaks = wav_profile(dst)
                print(f"    r{rep} 完成: {dst.name}  {len(audio)} bytes  总 {seconds:.2f}s"
                      f"  有效 {eff:.2f}s  音节核 {peaks}", flush=True)
                results.append({"engine": args.engine, "key": key, "label": label,
                                "role": role, "text": text, "rep": rep,
                                "file": dst.name, "error": None,
                                "seconds": round(seconds, 3), "eff": round(eff, 3),
                                "peaks": peaks, "bytes": len(audio)})

    (out_dir / "results.json").write_text(
        json.dumps({"engine": args.engine, "suite": args.suite,
                    "voice": Path(voice_path).name, "user_sentence": USER_SENTENCE,
                    "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    rows_by_key: dict[str, list[dict]] = {}
    print("\n" + "=" * 78)
    print(f"引擎 {args.engine}　suite {args.suite}")
    print(f"{'变体':<34}{'有效时长各次(s)':<28}{'区间':>14}{'音节核':>10}")
    for key, label, _t, _r in variants:
        rows = sorted([r for r in results if r["key"] == key], key=lambda r: r["rep"])
        rows_by_key[key] = rows
        sp = span(rows, "eff")
        effs = " ".join(f"{r['eff']:.2f}" for r in rows if r["file"]) or "—"
        span_s = f"{sp[0]:.2f}–{sp[1]:.2f}" if sp else "—"
        pk = " ".join(str(r["peaks"]) for r in rows if r["file"]) or "—"
        print(f"{'':<16}{label:<26}{effs:<28}{span_s:>14}{pk:>10}")

    value = span(rows_by_key.get(next((k for k, _l, _t, r in variants if r == "value"), ""), []), "eff")
    digit = span(rows_by_key.get(next((k for k, _l, _t, r in variants if r == "digit"), ""), []), "eff")
    print("-" * 78)
    if value and digit:
        gap = value[0] - digit[1]
        print(f"基准区间 数值读法 {value[0]:.2f}–{value[1]:.2f}s"
              f"　逐位读法 {digit[0]:.2f}–{digit[1]:.2f}s"
              f"　间距 {gap:+.2f}s {'（分离，可用作判据）' if gap > 0 else '（重叠，不可判）'}")
        for key, label, _t, role in variants:
            if role != "probe":
                continue
            sp = span(rows_by_key.get(key, []), "eff")
            if not sp:
                print(f"  {label:<28} —（无数据）")
                continue
            print(f"  {label:<28} {sp[0]:.2f}–{sp[1]:.2f}s  → {verdict(sp, value, digit)}")
    else:
        print("基准变体数据不足（value / digit 缺失），跳过自动判读。")
    print("=" * 78)

    write_index(out_dir, variants, rows_by_key, Path(voice_path).name,
                args.reps, args.engine, value, digit)
    print(f"\n试听页: {out_dir / 'index.html'}")
    return 0


def write_index(out_dir: Path, variants: list, rows_by_key: dict,
                voice: str, reps: int, engine: str,
                value, digit) -> None:
    blocks = []
    for key, label, text, role in variants:
        rows = rows_by_key.get(key, [])
        players = "".join(
            f'<div class="take"><span class="tno">r{r["rep"]}</span>'
            + (f'<audio controls preload="none" src="{html.escape(r["file"])}"></audio>'
               f'<span class="sec">{r["eff"]:.2f}s</span>'
               if r["file"] else f'<span class="err">{html.escape(r["error"] or "")}</span>')
            + "</div>"
            for r in rows
        )
        sp = span(rows, "eff")
        span_s = f"{sp[0]:.2f}–{sp[1]:.2f}s" if sp else "—"
        badge = {"value": "基准 · 5 音节", "digit": "基准 · 4 音节"}.get(role, "")
        if role == "probe" and sp and value and digit:
            badge = verdict(sp, value, digit)
        badge_html = f' <span class="star">{html.escape(badge)}</span>' if badge else ""
        blocks.append(f"""    <tr>
      <td class="lbl"><b>{html.escape(label)}</b>{badge_html}<code>{html.escape(text)}</code></td>
      <td class="takes">{players}</td>
      <td class="num mean">{span_s}</td>
    </tr>""")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>数字读法 A/B 对照</title>
<style>
  :root {{ --ink:#1f2328; --muted:#6b7280; --line:#e5e7eb; --bg:#fbfbf9; --card:#fff;
           --accent:#2f6f4e; --star:#8a6d1f; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:32px 28px 64px; background:var(--bg); color:var(--ink);
         font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif; }}
  h1 {{ font:600 24px/1.3 Georgia,"Songti SC",serif; margin:0 0 6px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:18px; }}
  .meta code {{ background:#eef2ef; padding:2px 6px; border-radius:4px; }}
  .note {{ background:#f2f7f4; border-left:3px solid var(--accent); padding:12px 16px;
           border-radius:6px; margin:0 0 22px; font-size:13.5px; color:#33443c; }}
  .note b {{ color:#1d4635; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
           border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th,td {{ text-align:left; padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ background:#f6f7f5; font-weight:600; font-size:13px; color:#4b5563;
        text-transform:uppercase; letter-spacing:.4px; }}
  tr:last-child td {{ border-bottom:none; }}
  .lbl b {{ display:block; font-weight:600; }}
  .lbl code {{ color:var(--muted); font-size:12px; }}
  .star {{ display:inline-block; background:#fdf6e3; color:var(--star); font-weight:500;
           font-size:11px; padding:1px 6px; border-radius:10px; margin-top:3px; }}
  .takes {{ white-space:nowrap; }}
  .take {{ display:flex; align-items:center; gap:7px; }}
  .take + .take {{ margin-top:5px; }}
  .tno {{ color:var(--muted); font-size:12px; width:18px; }}
  .sec {{ color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }}
  .take audio {{ height:30px; vertical-align:middle; }}
  .num {{ font-variant-numeric:tabular-nums; color:#374151; white-space:nowrap; }}
  .mean {{ font-weight:600; color:#1d4635; }}
  .err {{ color:#b4232a; font-size:12.5px; }}
</style></head>
<body>
  <h1>数字读法真机对照</h1>
  <div class="meta">引擎 <code>{html.escape(engine)}</code> · 音色 <code>{html.escape(voice)}</code>
    · 每变体 <b>{reps}</b> 次重复 · 只改数字的写法，其余一字不动</div>
  <p class="note">听 <code>230 倍</code> 念的是<b>「两百三十倍」（5 音节）</b>
     还是<b>「二三零倍」（4 音节）</b>。播放器旁的秒数是<b>去静音有效时长</b> ——
     它就是尺子：5 音节基准明显更长。若某一行的时长落在 4 音节基准的区间里，
     说明那个数字走了 unk 的路（模型逐位念）。</p>
  <table>
    <thead><tr><th>变体 / 送入文本</th><th>试听（{reps} 次）</th><th>有效时长区间</th></tr></thead>
    <tbody>
{chr(10).join(blocks)}
    </tbody>
  </table>
</body></html>
"""
    (out_dir / "index.html").write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
