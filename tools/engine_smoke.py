#!/usr/bin/env python3
"""引擎适配层冒烟工具：同一份参数在自建 / autodl.art 引擎上合成单段音频。

用法（在 webui-backend 目录下）：
  python tools/engine_smoke.py --engine local  --voice /path/ref.mp3 --text "你好"
  python tools/engine_smoke.py --engine art    --voice /path/ref.mp3 --text "你好" \
      [--token $AUTODL_API_TOKEN] [--emotion happy]
  python tools/engine_smoke.py --engine auto   ...   # 主引擎不可用自动切换

依赖后端环境（fastapi/httpx 已装）。不修改任何现有服务，仅验证适配层。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.engines import (  # noqa: E402
    EngineRegistry,
    IndexttsArtEngine,
    IndexttsLocalEngine,
    SegmentRequest,
    VoiceRef,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="引擎适配层冒烟测试")
    parser.add_argument("--engine", choices=["local", "art", "auto"], default="auto")
    parser.add_argument("--voice", required=True, help="参考音频本地路径")
    parser.add_argument("--text", default="哈喽，大家好，欢迎收听我们的播客。")
    parser.add_argument("--emotion", default=None, choices=["happy", "sad", "angry", "afraid", "disgusted", "surprised", "calm", "neutral"])
    parser.add_argument("--tts-url", default="http://localhost:8000")
    parser.add_argument("--token", default=None, help="autodl.art Token，也可用 AUTODL_API_TOKEN")
    parser.add_argument("--output", default="engine_smoke.wav")
    args = parser.parse_args()

    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    registry = EngineRegistry()
    if args.engine in ("local", "auto"):
        registry.register(IndexttsLocalEngine(args.tts_url, client))
    if args.engine in ("art", "auto"):
        registry.register(IndexttsArtEngine(token=args.token))

    req = SegmentRequest(
        text=args.text,
        voice=VoiceRef(local_path=args.voice, display_name=pathlib.Path(args.voice).name),
        emotion_label=args.emotion,
    )

    try:
        engine, audio = await registry.synthesize(req)
        out = pathlib.Path(args.output)
        out.write_bytes(audio)
        print(f"[OK] engine={engine.name} bytes={len(audio)} -> {out}")
        return 0
    except Exception as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
