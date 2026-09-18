#!/usr/bin/env python3
"""真机并发验证：302.ai 与 autodl.art 各 3 段并发 3，实测墙钟时间与实际并发数。

用法：cd podcast-webui && env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  INDEXTTS302_API_KEY=sk-xxx python3 tools/test_live_concurrency.py
"""

import asyncio
import sys
import time
import wave
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webui-backend"))

from app.engines import Indextts302aiEngine, IndexttsArtEngine, SegmentRequest, VoiceRef  # noqa: E402

VOICE = str(ROOT.parent / "preset-voices" / "男-播客1.mp3")  # 302ai 缓存已有此路径键
SEGMENTS = [
    "今天我们来聊一个特别现实的话题。",
    "三十五岁之后，再就业到底有多难？",
    "有人说这是职场上的一道坎。",
]


async def run_engine(name: str, engine, voice: VoiceRef, conc: int = 3) -> None:
    sem = asyncio.Semaphore(conc)
    inflight = {"n": 0, "max": 0}
    done = {"n": 0}

    async def one(idx: int, text: str) -> bytes:
        async with sem:
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
            t0 = time.monotonic()
            audio = await engine.synthesize_segment(SegmentRequest(text=text, voice=voice, emotion_label=None))
            dt = time.monotonic() - t0
            inflight["n"] -= 1
            done["n"] += 1
            print(f"  [{name}] 段{idx + 1} 完成 {dt:.1f}s（在途 {inflight['n']}，累计 {done['n']}/3）")
            return audio

    t0 = time.monotonic()
    results = await asyncio.gather(*[one(i, t) for i, t in enumerate(SEGMENTS)])
    wall = time.monotonic() - t0

    ok = all(len(r) > 1000 for r in results)
    # 校验音频头
    for r in results:
        with wave.open(io.BytesIO(r), "rb") as w:
            pass
    print(f"[{name}] 墙钟 {wall:.1f}s | 实际最大并发 {inflight['max']} | 3 段全部合法 wav: {ok}")
    print(f"[{name}] 若串行约需 3 × 单段耗时，实际对比看上面各段时间戳")


async def main() -> None:
    voice = VoiceRef(local_path=VOICE, tts_path=VOICE, display_name="男-播客1.mp3")

    print("=== 302.ai 并发 3 ===")
    e302 = Indextts302aiEngine(cache_path=ROOT / "webui-backend" / "data" / "ai302_voices.json")
    if await e302.health():
        await run_engine("302ai", e302, voice, conc=3)
    else:
        print("302.ai 探活失败，跳过")

    print()
    print("=== autodl.art 并发 3 ===")
    eart = IndexttsArtEngine()
    if await eart.health():
        await run_engine("art", eart, voice, conc=3)
    else:
        print("art 探活失败（token 缺失？）")


if __name__ == "__main__":
    asyncio.run(main())
