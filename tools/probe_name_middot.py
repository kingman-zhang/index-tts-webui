#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外国人名「中点」真机对照探针（A/B 听感实验）。

背景：用户反馈「保罗・萨特」这个点被读出怪音。
已知（代码层）：
  - 本地 index-tts `front.py` 的 `char_rep_map` 有 `"·": "-"`，
    但当前生产引擎是 autodl.art（`TTS_ENGINE_PREFERRED=indextts_art`），
    那条链路**不经过** front.py，也不经过 tts-server 的 `_sanitize_text`。
  - 所以中点字符是**原样**发给第三方平台的，第三方 TN 行为未知。

本脚本把同一句话里的**分隔符**逐一替换，用真实引擎各合成一遍，
产出可对比试听的音频 —— 用听感而不是猜测定「该归一化成什么」。

**为什么要重复**：IndexTTS 推理 `do_sample=True` 且无 seed，同参数两次调用读法可能不同
（见 skill `indextts-text-frontend-repro`）。单次 A/B 对照没有意义 —— 每个变体至少
重复 3 次，看「怪音是否稳定出现」，而不是「这一条听起来怪不怪」。

用法：
    python tools/probe_name_middot.py                 # art 引擎、7 个变体 × 3 次
    python tools/probe_name_middot.py --reps 5        # 提高重复次数
    python tools/probe_name_middot.py --engine 302ai
    python tools/probe_name_middot.py --only 01_u30fb,05_none

成本：art 按 0.001 元/s 计费，7 变体 × 3 次 ≈ ¥0.5 上下。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "webui-backend"
sys.path.insert(0, str(BACKEND))

# ── 变体表：(ASCII 文件名, 展示标签, 分隔符, Unicode 说明) ──
VARIANTS = [
    ("01_u30fb", "U+30FB 片假名中点（用户原文）", "\u30fb", "0x30FB"),
    ("02_u00b7", "U+00B7 中文间隔号（标准）", "\u00b7", "0x00B7"),
    ("03_u2022", "U+2022 项目符号 •", "\u2022", "0x2022"),
    ("04_u2027", "U+2027 连字点 ‧", "\u2027", "0x2027"),
    ("05_none", "删除分隔符（保罗萨特）", "", "-"),
    ("06_space", "半角空格", " ", "0x20"),
    ("07_dunhao", "顿号「、」", "\u3001", "0x3001"),
]

# 模板句：只换中间那个分隔符，其余完全一致
TEMPLATE = "法国哲学家保罗{sep}萨特说过，人是自己选择的结果。"


def load_env(path: Path) -> None:
    """极简 .env 加载（不引第三方库）。已存在的环境变量不覆盖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def probe_duration(path: Path) -> float:
    """用 ffprobe 读时长；不可用则返回 0。"""
    for exe in ("ffprobe", "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"):
        try:
            out = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0 and out.stdout.strip():
                return float(out.stdout.strip())
        except Exception:
            continue
    return 0.0


def build_engine(name: str, http_client):
    from app.engines import Indextts302aiEngine, IndexttsArtEngine

    if name == "art":
        return IndexttsArtEngine(client=http_client)
    if name == "302ai":
        return Indextts302aiEngine(client=http_client)
    raise SystemExit(f"暂不支持的引擎: {name}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="art", choices=["art", "302ai"])
    ap.add_argument("--voice", default=None, help="参考音频路径（默认挑一个本地中文音色）")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--only", default=None, help="只跑指定变体文件名前缀，逗号分隔")
    ap.add_argument("--reps", type=int, default=3, help="每个变体重复次数（默认 3）")
    args = ap.parse_args()

    load_env(BACKEND / ".env")

    import httpx

    voice_path = args.voice
    if not voice_path:
        candidates = [
            BACKEND / "data/voices/邻家男孩00.mp3",
            BACKEND / "data/voices/test_voice.wav",
            BACKEND / "data/voices/my_voice_4.wav",
        ]
        for c in candidates:
            if c.is_file():
                voice_path = str(c)
                break
    if not voice_path or not Path(voice_path).is_file():
        raise SystemExit("找不到参考音频，请用 --voice 指定")

    out_dir = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[1] / "data" / "outputs" / "name-middot-ab"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    only = set(args.only.split(",")) if args.only else None
    variants = [v for v in VARIANTS if not only or v[0] in only]

    print(f"引擎={args.engine}  音色={Path(voice_path).name}  重复={args.reps}  输出={out_dir}\n",
          flush=True)

    from app.engines.base import SegmentRequest, VoiceRef

    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        engine = build_engine(args.engine, client)
        if not await engine.health():
            raise SystemExit(f"引擎 {args.engine} 不可用（缺 Key？）")

        for key, label, sep, code in variants:
            text = TEMPLATE.format(sep=sep)
            print(f"[{args.engine}][{key}] {label}\n    提交: {text!r}", flush=True)
            for rep in range(1, args.reps + 1):
                req = SegmentRequest(text=text,
                                     voice=VoiceRef(local_path=voice_path,
                                                    display_name=Path(voice_path).stem))
                try:
                    audio = await engine.synthesize_segment(req)
                except Exception as exc:  # noqa: BLE001
                    print(f"    r{rep} 失败: {exc}", flush=True)
                    results.append({"engine": args.engine, "key": key, "label": label,
                                    "code": code, "text": text,
                                    "rep": rep, "file": None, "error": str(exc)[:300],
                                    "seconds": 0.0, "bytes": 0})
                    continue
                ext = ".mp3" if audio[:3] == b"ID3" or audio[:2] == b"\xff\xfb" else ".wav"
                dst = out_dir / f"{args.engine}_{key}_r{rep}{ext}"
                dst.write_bytes(audio)
                seconds = probe_duration(dst)
                print(f"    r{rep} 完成: {dst.name}  {len(audio)} bytes  {seconds:.2f}s", flush=True)
                results.append({"engine": args.engine, "key": key, "label": label,
                                "code": code, "text": text,
                                "rep": rep, "file": dst.name, "error": None,
                                "seconds": round(seconds, 3), "bytes": len(audio)})
            print(flush=True)

    # 与历史结果合并（同一 out 目录可能跑过不同引擎）；同名文件以本次为准
    fresh_names = {r["file"] for r in results if r["file"]}
    merged = list(results)
    old_file = out_dir / "results.json"
    if old_file.is_file():
        try:
            for r in json.loads(old_file.read_text(encoding="utf-8")):
                if r.get("file") and r["file"] in fresh_names:
                    continue
                r.setdefault("engine", "art")
                merged.append(r)
        except Exception as exc:  # noqa: BLE001
            print(f"（历史 results.json 读取失败，忽略: {exc}）")

    (out_dir / "results.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    engines = sorted({r.get("engine", "art") for r in merged})
    order = {v[0]: i for i, v in enumerate(VARIANTS)}
    for eng in engines:
        print("=" * 72)
        print(f"引擎 {eng}")
        print(f"{'变体':<36}{'各次时长(s)':<26}{'均值':>7}")
        for key, label, _sep, _code in variants:
            rows = [r for r in merged if r.get("engine") == eng and r["key"] == key and r["file"]]
            if not rows:
                continue
            rows.sort(key=lambda r: r["rep"])
            times = " ".join(f"{r['seconds']:.2f}" for r in rows)
            mean = sum(r["seconds"] for r in rows) / len(rows)
            flag = "  ← 用户原文" if key == "01_u30fb" else ""
            print(f"{label:<36}{times:<26}{mean:>7.2f}{flag}")
    print("=" * 72)
    print(f"\n音频与 results.json 在: {out_dir}")

    merged.sort(key=lambda r: (r.get("engine", "art"), order.get(r["key"], 99), r["rep"]))
    write_index(out_dir, merged, variants, Path(voice_path).name, args.reps)
    return 0


def write_index(out_dir: Path, results: list, variants: list,
                voice: str, reps: int) -> None:
    """生成分组试听页：按引擎分节，变体一行，行内是 N 次重复的播放器。"""
    import html

    blocks = []
    for eng in sorted({r.get("engine", "art") for r in results}):
        blocks.append(f'    <tr class="engrow"><td colspan="4">{html.escape(eng)} 引擎</td></tr>')
        for key, label, _sep, code in variants:
            rows = sorted([r for r in results
                           if r.get("engine") == eng and r["key"] == key],
                          key=lambda r: r["rep"])
            if not rows:
                continue
            players = "".join(
                f'<div class="take"><span class="tno">r{r["rep"]}</span>'
                + (f'<audio controls preload="none" src="{html.escape(r["file"])}"></audio>'
                   f'<span class="sec">{r["seconds"]:.2f}s</span>'
                   if r["file"] else f'<span class="err">{html.escape(r["error"] or "")}</span>')
                + "</div>"
                for r in rows
            )
            times = [r["seconds"] for r in rows if r["file"]]
            mean = f"{sum(times)/len(times):.2f}s" if times else "—"
            star = ' <span class="star">用户原文</span>' if key == "01_u30fb" else ""
            blocks.append(f"""    <tr>
      <td class="lbl"><b>{html.escape(label)}{star}</b><code>{html.escape(code)}</code></td>
      <td class="txt">{html.escape(rows[0]['text'])}</td>
      <td class="takes">{players}</td>
      <td class="num mean">{mean}</td>
    </tr>""")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>人名中点 A/B 对照试听</title>
<style>
  :root {{ --ink:#1f2328; --muted:#6b7280; --line:#e5e7eb; --bg:#fbfbf9; --card:#fff;
           --accent:#2f6f4e; --star:#8a6d1f; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:32px 28px 64px; background:var(--bg); color:var(--ink);
         font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif; }}
  h1 {{ font:600 24px/1.3 Georgia,"Songti SC",serif; margin:0 0 6px; letter-spacing:.3px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
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
  .lbl {{ white-space:nowrap; }}
  .lbl b {{ display:block; font-weight:600; }}
  .lbl code {{ color:var(--muted); font-size:12px; }}
  .star {{ display:inline-block; background:#fdf6e3; color:var(--star); font-weight:500;
           font-size:11px; padding:1px 6px; border-radius:10px; margin-top:3px; }}
  .txt {{ color:#3f4650; font-size:13.5px; max-width:280px; }}
  .takes {{ white-space:nowrap; }}
  .take {{ display:flex; align-items:center; gap:7px; }}
  .take + .take {{ margin-top:5px; }}
  .tno {{ color:var(--muted); font-size:12px; width:18px; }}
  .sec {{ color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }}
  .ply audio, .take audio {{ height:30px; vertical-align:middle; }}
  .num {{ font-variant-numeric:tabular-nums; color:#374151; white-space:nowrap; }}
  .mean {{ font-weight:600; color:#1d4635; }}
  .err {{ color:#b4232a; font-size:12.5px; }}
  .engrow td {{ background:#eef2ef; font-weight:600; font-size:13px; color:#2f6f4e;
                letter-spacing:.3px; }}
</style></head>
<body>
  <h1>外国人名「中点」A/B 对照</h1>
  <div class="meta">音色 <code>{html.escape(voice)}</code>
    · 同一句只替换分隔符 · 每变体 <b>{reps}</b> 次重复（推理带采样，单次不算数）</div>
  <p class="note">听的时候只盯<b>「保罗」和「萨特」之间</b>那一小段：
     是干净地过去、有轻微停顿，还是<b>多挤出一个怪音节</b>（像「颤」「戳」之类）。
     同一个变体的 {reps} 次如果<b>每次都出怪音</b>，说明是该字符稳定触发的；
     如果只有某一次出，那更像模型自身的随机抖动，换字符救不了。</p>
  <table>
    <thead><tr><th>变体</th><th>送入文本</th><th>试听（{reps} 次）</th><th>均值</th></tr></thead>
    <tbody>
{chr(10).join(blocks)}
    </tbody>
  </table>
</body></html>
"""
    (out_dir / "index.html").write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
