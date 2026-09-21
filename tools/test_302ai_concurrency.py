"""302.ai IndexTTS-2 并发能力实测脚本。

目的：验证 302.ai 平台是否允许同一 token 下多个合成任务并行
（此前 3 并发任务总耗时 ≈ 各段耗时之和，疑似平台侧串行）。

方法：
  Phase 1  单段基线：提交 1 段（~180 字），测单段耗时 T1（顺带验证 token 可用）；
  Phase 2  并发组：同时提交 3 段等长文本，记录每段的提交/完成时间窗，
           用「总耗时 vs 单段耗时之和」与时间窗重叠判断真实并发。

证据口径：
  - 客户端时间窗重叠：3 个任务的 [submit, done] 区间互相覆盖 → 排队并行；
  - 服务端 generation_time：单段自身生成耗时；若各段 generation_time
    与串行时相当、但总耗时 ≈ 单段耗时 → 平台并行处理。
  - speedup = sum(各段 wall) / 总 wall；≈N 为完全并行，≈1 为完全串行。

费用：每段 ~180 字 ≈ 2.09 token/字 × 0.015 PTC/1k ≈ 0.006 PTC，4 段合计 <¥0.3。
音色：复用 webui-backend/data/ai302_voices.json 里已上传的公网 URL，不产生上传费。

用法：
  /path/to/python tools/test_302ai_concurrency.py <API_KEY> [concurrency]
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui-backend"))

from app.engines import Indextts302aiEngine, SegmentRequest, VoiceRef  # noqa: E402

CACHE_FILE = Path(__file__).resolve().parent.parent / "webui-backend" / "data" / "ai302_voices.json"

# 三段等长播客风格文本（各 ~180 字，内容不同避免平台缓存干扰）
TEXTS = [
    "你有没有过这样的体验，明明忙了一整天，晚上回想起来却什么都没做成？"
    "其实这不是你不够努力，而是你的注意力被切碎了。大脑天生喜欢即时反馈，"
    "刷一下手机就能获得一次小小的满足，而深度工作需要长时间专注才能进入状态。"
    "所以真正拉开差距的，不是时间的总量，而是你能不能把时间整块地留给自己。",

    "很多人以为自律靠的是意志力，其实靠的是环境设计。想减肥就别在家里囤零食，"
    "想读书就把手机放到另一个房间。心理学上有个说法叫默认选项，人会不自觉地"
    "顺着阻力最小的方向走。聪明人不是对抗欲望，而是重新设计自己的处境，"
    "让正确的事情变成最容易做的事情。",

    "为什么你定了那么多目标，最后都不了了之？因为目标只能指方向，习惯才能带你到终点。"
    "每天读十页书很轻松，但想着读完五十本就会吓退自己。把大目标拆成小到不可能失败的"
    "动作，先让行动发生，再谈坚持。成长从来不是靠爆发，而是靠那些看起来微不足道的"
    "重复积累。",
]


def pick_voice_url() -> str:
    data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    for url in data.values():
        if str(url).startswith("http"):
            return url
    raise SystemExit(f"缓存文件里没有可用的音色 URL: {CACHE_FILE}")


async def run_segment(engine: Indextts302aiEngine, text: str, tag: str,
                      timeline: list, t0: float) -> dict:
    """提交一段并记录时间窗（不落盘，只取结果元数据 + 抽查音频头）。"""
    payload = {"text": text, "speaker_audio_url": engine.voice_map["test"]}
    t_submit = time.monotonic() - t0
    try:
        task_id = await engine._submit(payload)
    except Exception as e:
        timeline.append({"tag": tag, "submit": t_submit, "done": time.monotonic() - t0, "error": str(e)[:200]})
        return {"tag": tag, "error": str(e)[:200]}
    t_submitted = time.monotonic() - t0
    result = await engine._wait_result(task_id)
    t_done = time.monotonic() - t0
    audio = await engine._download_audio(result["audio_url"])
    rec = {
        "tag": tag,
        "task_id": task_id,
        "submit": t_submit,
        "submitted": t_submitted,
        "done": t_done,
        "wall": t_done - t_submit,
        "gen_time": result.get("generation_time"),
        "audio_bytes": len(audio),
        "is_wav": audio[:4] == b"RIFF",
    }
    timeline.append(rec)
    return rec


def analyze(phase: str, records: list[dict], t1: float | None = None) -> tuple:
    print(f"\n── {phase} ──")
    ok = [r for r in records if "error" not in r]
    for r in records:
        if "error" in r:
            print(f"  [{r['tag']}] 失败: {r['error']}")
            continue
        print(f"  [{r['tag']}] submit@{r['submit']:6.1f}s done@{r['done']:6.1f}s "
              f"wall={r['wall']:5.1f}s server_gen={r.get('gen_time') or '?':>5} "
              f"audio={r['audio_bytes']}B wav={r['is_wav']}")
    if len(ok) >= 2:
        total_wall = max(r["done"] for r in ok) - min(r["submit"] for r in ok)
        gen_sum = sum(r.get("gen_time") or 0 for r in ok)
        # 关键口径：GPU 有效算力占用率 = 服务端各段纯生成时间之和 / 实际总耗时。
        # 完全并行 → ≈n；完全串行 → ≈1（各段生成时间首尾相接排满总时长）。
        # （不要用 sum(客户端wall)/total——串行 FIFO 下排队等待会让它虚高到 ~n/2）
        util = gen_sum / total_wall if total_wall else 0
        print(f"  ✓ 总耗时={total_wall:.1f}s 服务端生成时间之和={gen_sum:.1f}s "
              f"GPU占用率={util:.2f} (≈1=串行, ≈{len(ok)}=完全并行)")
        return util, total_wall
    return None, None, None


async def main() -> None:
    token = sys.argv[1] if len(sys.argv) > 1 else ""
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if not token:
        raise SystemExit("用法: test_302ai_concurrency.py <API_KEY> [concurrency]")

    voice_url = pick_voice_url()
    print(f"音色 URL: {voice_url[:70]}...")

    engine = Indextts302aiEngine(api_key=token, cache_path=None, voice_map={"test": voice_url})
    engine.poll_timeout = 300.0

    ok = await engine.health()
    print(f"探活: {'✓ 连通且鉴权有效' if ok else '✗ 不可用（key 无效或网络不通）'}")
    if not ok:
        return

    # Phase 1: 单段基线（第三次参数传 skip-base 可跳过，只跑并发组）
    skip_base = len(sys.argv) > 3 and sys.argv[3] == "skip-base"
    t1 = None
    if not skip_base:
        t0 = time.monotonic()
        timeline1: list = []
        base_rec = await run_segment(engine, TEXTS[0], "baseline", timeline1, t0)
        analyze("Phase 1 单段基线", timeline1)
        if "error" in base_rec:
            return
        t1 = base_rec["wall"]

    # Phase 2: N 段并发（复用前 N 段文本，索引 0..n-1）
    n = min(conc, len(TEXTS))
    print(f"\n同时提交 {n} 段（每段 ~{len(TEXTS[0])} 字）...")
    t0 = time.monotonic()
    timeline2: list = []
    records = await asyncio.gather(*[
        run_segment(engine, TEXTS[i], f"par-{i}", timeline2, t0) for i in range(n)
    ])
    speedup, total_wall = analyze(f"Phase 2 {n} 段并发", timeline2)

    print("\n── 结论 ──")
    if speedup is None:
        print("并发组有失败，无法判定")
        return
    if speedup >= 1.5:
        print(f"并行生效：GPU占用率={speedup:.2f}，总耗时≈单段耗时的 1/{speedup:.1f}")
        print("→ 平台支持同账号并行，客户端并发能线性缩短任务总时长")
    elif speedup < 1.0:
        print(f"近乎串行：GPU占用率={speedup:.2f}，总耗时≈各段生成时间之和（首尾相接排满）")
        if t1:
            print(f"→ 单段基线 {t1:.1f}s × {n} = {n*t1:.1f}s ≈ 并发总耗时 {total_wall:.1f}s，并发无加速")
        print("→ 平台按账号串行执行（排队 FIFO），客户端并发无法加速；"
              "TTS_CONCURRENCY 调大只会改变段间排队顺序，不改变总时长")
    else:
        print(f"部分并行：GPU占用率={speedup:.2f}，存在一定重叠但算力受限")
        print("→ 平台可能有少量并行额度，收益有限")


if __name__ == "__main__":
    asyncio.run(main())
