"""SiliconFlow 引擎真机实测（用户测试 Key，国内站 CosyVoice2 链路）。

用法：
  SILICONFLOW_API_KEY=sk-xxx /path/to/python tools/test_siliconflow_live.py
"""
import asyncio
import json
import os
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui-backend"))

import httpx

from app.engines.indextts_siliconflow import IndexttsSiliconflowEngine
from app.engines.base import SegmentRequest, VoiceRef

KEY = os.environ.get("SILICONFLOW_API_KEY", "")
REF = Path("/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/preset-voices/女-初恋女友.mp3")
TMP = Path("/tmp/sf_live")
if not TMP.exists():
    TMP.mkdir()

results = []


def report(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name} {detail}")


def wavinfo(b: bytes):
    with wave.open(__import__("io").BytesIO(b)) as w:
        return (w.getnchannels(), w.getsampwidth() * 8, w.getframerate()), w.getnframes() / w.getframerate()


async def main():
    api = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
    cache = TMP / "voices.json"
    eng = IndexttsSiliconflowEngine(api_key=KEY, client=api, cache_path=cache)

    report("1. health 探活", await eng.health())

    # 2) 首次合成：自动克隆上传 + 坏头修复
    seg = SegmentRequest(
        text="认知觉醒告诉我们，情绪不是敌人，而是信使。这是备援引擎的真机测试。",
        voice=VoiceRef(local_path=str(REF), display_name=REF.name),
        emotion_label=None,
        speed=1.0,
    )
    try:
        t0 = asyncio.get_event_loop().time()
        audio = await eng.synthesize_segment(seg)
        dt = asyncio.get_event_loop().time() - t0
        fmt, dur = wavinfo(audio)
        (TMP / "clone_test.wav").write_bytes(audio)
        report("2. 克隆+合成+坏头修复", fmt == (1, 16, 24000), f"fmt={fmt} dur={dur:.1f}s 耗时{dt:.1f}s")
    except Exception as e:
        report("2. 克隆+合成+坏头修复", False, str(e)[:300])
        return

    # 3) 缓存复用
    before = len(json.loads(cache.read_text()))
    seg.text = "第二次合成，验证音色缓存复用是否生效。"
    try:
        audio2 = await eng.synthesize_segment(seg)
        after = len(json.loads(cache.read_text()))
        report("3. 缓存复用", before == after == 1, f"缓存条目 {before}->{after}")
    except Exception as e:
        report("3. 缓存复用", False, str(e)[:300])

    # 4) 情绪内联提示（引擎自动加前缀）
    seg.emotion_label = "happy"
    seg.text = "太好了，今天真的太开心了！"
    try:
        audio3 = await eng.synthesize_segment(seg)
        _, dur3 = wavinfo(audio3)
        (TMP / "emo_happy.wav").write_bytes(audio3)
        report("4. 情绪(happy)内联提示", dur3 > 0.5, f"dur={dur3:.1f}s")
    except Exception as e:
        report("4. 情绪(happy)内联提示", False, str(e)[:300])

    # 5) 语速参数
    seg.emotion_label = None
    seg.text = "语速一点二倍的真实测试。"
    seg.speed = 1.2
    try:
        audio4 = await eng.synthesize_segment(seg)
        _, dur4 = wavinfo(audio4)
        (TMP / "speed_120.wav").write_bytes(audio4)
        report("5. 语速 1.2x", dur4 > 0.5, f"dur={dur4:.1f}s")
    except Exception as e:
        report("5. 语速 1.2x", False, str(e)[:300])

    await api.aclose()
    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {npass}/{len(results)} passed =====")


if __name__ == "__main__":
    asyncio.run(main())
