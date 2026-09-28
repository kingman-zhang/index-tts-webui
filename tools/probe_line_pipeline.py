#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""句子级 A/B 真机对照：**原文直发** vs **backend 归一化后**。

用途：用户反馈「这句话被读出怪音」时，把他给的**原句**按两种文本送同一个引擎，
逐条试听对照，直接判定问题是否出在「送出去之前的预处理」这一环。

为什么必须真机对照：本项目默认引擎可能是云端（`TTS_ENGINE_PREFERRED=indextts_art`
/ `indextts_302ai`），此时 index-tts 的 `front.py` 与本仓库 tts-server 的
`_sanitize_text` **都不在链路上**。所以「本地文本前端干净」≠「送出去的文本干净」。
结合 `tools/diagnose_text.py` 看链路、再用本脚本听结果，两头才闭合。

两个变体：
  01 raw    : 用户原句一字不改 —— 等价于「跑的那份代码没有归一化」的那条路
  02 fixed  : 走 queue_worker 的真实顺序 术语表 → 人名分隔号 → 数字读法

**重复采样**：IndexTTS `do_sample=True` 且无 seed，单次对照没有意义
（见 skill `indextts-text-frontend-repro`），每个变体默认重复 3 次。

用法：
    python tools/probe_line_pipeline.py --text '作家卡仑・墨菲，历史学家阿瑟・施莱辛格爵士……'
    python tools/probe_line_pipeline.py --text-file /tmp/line.txt --reps 5
    python tools/probe_line_pipeline.py --text '…' --engine 302ai --out /tmp/out

成本：art 约 ¥0.001/s，两句 × 3 次 ≈ ¥0.06。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND = TOOLS_DIR.parent / "webui-backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(TOOLS_DIR))

# 复用既有探针的 .env 加载 / 时长探测 / 引擎构造（避免三份实现漂移）
from probe_name_middot import build_engine, load_env, probe_duration  # noqa: E402


def build_fixed_text(text: str) -> dict:
    """复刻 queue_worker._execute_task 的文本预处理，返回各阶段。"""
    from app import name_punct, number_norm, stores

    terms = stores.load_glossary_for_synthesis(None)
    hits: list[tuple[str, str]] = []
    stage = text
    for t in terms:
        if t["original"] and t["original"] in stage:
            hits.append((t["original"], t["replacement"]))
            stage = stage.replace(t["original"], t["replacement"])
    after_glossary = stage
    if name_punct.ENABLED:
        stage = name_punct.normalize_name_separators(stage)
    after_punct = stage
    if number_norm.ENABLED:
        stage = number_norm.apply_number_rules([{"text": stage}])[0]["text"]
    return {
        "text": stage,
        "hits": hits,
        "after_glossary": after_glossary,
        "after_punct": after_punct,
        "flags": {
            "name_punct": name_punct.ENABLED,
            "name_punct_target": name_punct.TARGET,
            "number_norm": number_norm.ENABLED,
            "glossary_terms": len(terms),
        },
    }


def codes(text: str) -> str:
    """把「可疑」字符连码位一起列出来（中点类必须能一眼看出码位）。"""
    plain = set("，。！？；：“”‘’（）《》〈〉—…、％")
    out = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or ch.isascii() or ch in plain:
            continue
        out.append(f"{ch!r}U+{ord(ch):04X}")
    return "  ".join(out) or "（无）"


async def main() -> int:
    ap = argparse.ArgumentParser(description="句子级 A/B 真机对照")
    ap.add_argument("--text", default=None, help="要对照的句子")
    ap.add_argument("--text-file", default=None, help="从文件读一句（取第一行非空行）")
    ap.add_argument("--engine", default="art", choices=["art", "302ai"])
    ap.add_argument("--voice", default=None, help="参考音频（默认挑一个本地音色）")
    ap.add_argument("--reps", type=int, default=3, help="每变体重复次数（默认 3）")
    ap.add_argument("--out", default=None, help="输出目录")
    args = ap.parse_args()

    if args.text_file:
        lines = [ln.strip() for ln in
                 Path(args.text_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            raise SystemExit("文本文件是空的")
        raw_text = lines[0]
    elif args.text:
        raw_text = args.text
    else:
        raise SystemExit("请用 --text 或 --text-file 提供原句")

    load_env(BACKEND / ".env")
    # app 的 DATA_DIR 默认是相对 cwd 的 "./data"，而本脚本的 cwd 是 podcast-webui。
    # 必须在任何 `import app.*` 之前指到 webui-backend/data，否则读不到词表。
    os.environ.setdefault("DATA_DIR", str(BACKEND / "data"))

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

    fixed = build_fixed_text(raw_text)
    stamps = [
        ("01_raw", "原文直发（未归一化）", raw_text),
        ("02_fixed", "backend 归一化后", fixed["text"]),
    ]

    print("=" * 72)
    print("原句   :", raw_text)
    print("  码位 :", codes(raw_text))
    print("术语表 :", "命中 " + "、".join(f"{o}→{r}" for o, r in fixed["hits"])
          if fixed["hits"] else "无命中")
    print("归一化 :", fixed["text"])
    print("  码位 :", codes(fixed["text"]))
    print(f"开关   : name_punct={fixed['flags']['name_punct']}"
          f"({fixed['flags']['name_punct_target']})"
          f"  number_norm={fixed['flags']['number_norm']}"
          f"  生效词条={fixed['flags']['glossary_terms']}")
    print("=" * 72)

    if fixed["text"] == raw_text:
        print("\n[!] 归一化后与原文完全一致 —— 这句话里没有可被处理的字符。")
    print()

    out_dir = Path(args.out) if args.out else (
        TOOLS_DIR.parent / "data" / "outputs" / "line-pipeline-ab"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    from app.engines.base import SegmentRequest, VoiceRef

    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        engine = build_engine(args.engine, client)
        if not await engine.health():
            raise SystemExit(f"引擎 {args.engine} 不可用（缺 Key / 余额？）")

        for key, label, text in stamps:
            print(f"[{args.engine}][{key}] {label}\n    提交: {text!r}", flush=True)
            for rep in range(1, args.reps + 1):
                req = SegmentRequest(text=text,
                                     voice=VoiceRef(local_path=voice_path,
                                                    display_name=Path(voice_path).stem))
                try:
                    audio = await engine.synthesize_segment(req)
                except Exception as exc:  # noqa: BLE001
                    print(f"    r{rep} 失败: {exc}", flush=True)
                    results.append({"key": key, "label": label, "text": text, "rep": rep,
                                    "file": None, "error": str(exc)[:300],
                                    "seconds": 0.0, "bytes": 0})
                    continue
                ext = ".mp3" if audio[:3] == b"ID3" or audio[:2] == b"\xff\xfb" else ".wav"
                dst = out_dir / f"{args.engine}_{key}_r{rep}{ext}"
                dst.write_bytes(audio)
                seconds = probe_duration(dst)
                print(f"    r{rep} 完成: {dst.name}  {len(audio)} bytes  {seconds:.2f}s",
                      flush=True)
                results.append({"key": key, "label": label, "text": text, "rep": rep,
                                "file": dst.name, "error": None,
                                "seconds": round(seconds, 3), "bytes": len(audio)})
            print(flush=True)

    (out_dir / "results.json").write_text(
        json.dumps({"engine": args.engine, "voice": Path(voice_path).name,
                    "raw": raw_text, "fixed": fixed["text"],
                    "hits": fixed["hits"], "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    write_index(out_dir, stamps, results, args.engine, Path(voice_path).name, args.reps)
    print(f"引擎={args.engine}  音色={Path(voice_path).name}  重复={args.reps}")
    print(f"试听页: {out_dir / 'index.html'}")
    return 0


def write_index(out_dir: Path, stamps: list, results: list,
                engine: str, voice: str, reps: int) -> None:
    blocks = []
    for key, label, text in stamps:
        rows = [r for r in results if r["key"] == key]
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
        note = ("原文一字未改 —— 等价于跑的那份代码没有归一化"
                if key == "01_raw" else "backend 实际发给引擎的文本")
        blocks.append(f"""    <tr>
      <td class="lbl"><b>{html.escape(label)}</b><code>{html.escape(note)}</code></td>
      <td class="txt">{html.escape(text)}</td>
      <td class="takes">{players}</td>
      <td class="num mean">{mean}</td>
    </tr>""")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>句子级 A/B 对照试听</title>
<style>
  :root {{ --ink:#1f2328; --muted:#6b7280; --line:#e5e7eb; --bg:#fbfbf9; --card:#fff;
           --accent:#2f6f4e; --warn:#8a5a00; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:32px 28px 64px; background:var(--bg); color:var(--ink);
         font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif; }}
  h1 {{ font:600 24px/1.3 Georgia,"Songti SC",serif; margin:0 0 6px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
  .meta code {{ background:#eef2ef; padding:2px 6px; border-radius:4px; }}
  .note {{ background:#f2f7f4; border-left:3px solid var(--accent); padding:12px 16px;
           border-radius:6px; margin:0 0 22px; font-size:13.5px; color:#33443c; }}
  .note b {{ color:#1f4d38; }}
  .raw {{ background:#fff; border:1px solid var(--line); border-radius:10px;
          padding:14px 16px; margin:0 0 22px; font-size:14px; }}
  .raw .k {{ color:var(--muted); font-size:12.5px; display:block; margin-bottom:4px;
             text-transform:uppercase; letter-spacing:.4px; }}
  .raw .v {{ margin-bottom:10px; }}
  .raw .v:last-child {{ margin-bottom:0; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
           border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th,td {{ text-align:left; padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }}
  th {{ background:#f6f7f5; font-weight:600; font-size:13px; color:#4b5563; }}
  tr:last-child td {{ border-bottom:none; }}
  .lbl b {{ display:block; font-weight:600; }}
  .lbl code {{ color:var(--muted); font-size:12px; }}
  .txt {{ color:#3f4650; font-size:13px; max-width:420px; }}
  .takes {{ display:flex; flex-direction:column; gap:6px; }}
  .take {{ display:flex; align-items:center; gap:8px; }}
  .tno {{ color:var(--muted); font-size:12px; width:18px; }}
  .take audio {{ height:32px; }}
  .sec {{ color:var(--muted); font-size:12.5px; }}
  .num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .err {{ color:#b4232a; font-size:12.5px; }}
</style></head>
<body>
  <h1>句子级 A/B 对照</h1>
  <div class="meta">引擎 <code>{html.escape(engine)}</code> · 音色 <code>{html.escape(voice)}</code>
    · 每变体 <b>{reps}</b> 次重复 · 同一句话，只改「送出去的文本」</div>
  <p class="note"><b>重点听名字之间</b>：<code>卡仑・墨菲</code> / <code>阿瑟・施莱辛格</code> /
    <code>索伦・克尔凯郭尔</code> 这几个位置有没有多出一个不属于任何字的音。
    1 行是原文直发（等价于没部署归一化代码的那条路），2 行才是 backend 真正发出去的文本。</p>
  <div class="raw">
    <span class="k">原句（用户输入）</span><div class="v">{html.escape(stamps[0][2])}</div>
    <span class="k">归一化后（实际送给引擎）</span><div class="v">{html.escape(stamps[1][2])}</div>
  </div>
  <table>
    <thead><tr><th>变体</th><th>送入文本</th><th>试听</th><th>均值</th></tr></thead>
    <tbody>
{chr(10).join(blocks)}
    </tbody>
  </table>
</body></html>
"""
    (out_dir / "index.html").write_text(doc, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
