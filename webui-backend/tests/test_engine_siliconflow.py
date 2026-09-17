"""SiliconFlow 引擎适配器（indextts_siliconflow）单测，全 mock 不打真实 API。

运行：webui-backend 目录下
  /Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_engine_siliconflow.py
"""
import asyncio
import json
import sys
import tempfile
import wave
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.base import SegmentRequest, VoiceRef
from app.engines.indextts_siliconflow import IndexttsSiliconflowEngine

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {msg}")
    else:
        FAIL += 1
        print(f"FAIL {msg}")


def make_wav(path: Path, dur: float = 0.3):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x01" * int(24000 * dur))


class FakeAPI:
    """模拟 SiliconFlow：记录调用、可注入错误。"""

    def __init__(self):
        self.upload_calls = 0
        self.speech_calls = 0
        self.health_calls = 0
        self.speech_error: tuple[int, str] | None = None
        self.upload_error: tuple[int, str] | None = None
        self.last_payload: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url.path
        if url.endswith("/audio/voice/list"):
            self.health_calls += 1
            return httpx.Response(200, json={"result": []})
        if url.endswith("/uploads/audio/voice"):
            self.upload_calls += 1
            if self.upload_error:
                return httpx.Response(self.upload_error[0], json={"message": self.upload_error[1]})
            return httpx.Response(200, json={"uri": "speech:test_voice:abc:def"})
        if url.endswith("/audio/speech"):
            self.speech_calls += 1
            self.last_payload = json.loads(request.content.decode("utf-8"))
            if self.speech_error:
                return httpx.Response(self.speech_error[0], json={"message": self.speech_error[1]})
            return httpx.Response(
                200, headers={"content-type": "audio/wav"}, content=b"RIFFfake-audio-bytes"
            )
        return httpx.Response(404, json={"message": "not found"})


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def main():
    # 1) 无 Key → health False，不发请求
    api = FakeAPI()
    eng = IndexttsSiliconflowEngine(api_key="", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)))
    check(run(eng.health()) is False, "无 Key 时 health=False 且不请求")
    check(api.health_calls == 0, "无 Key 不发探活请求")

    # 2) 有 Key → health True，且 TTL 内只探活一次
    eng = IndexttsSiliconflowEngine(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
    )
    check(run(eng.health()) is True, "有 Key 探活成功")
    check(run(eng.health()) is True, "TTL 内二次探活")
    check(api.health_calls == 1, "探活请求只发一次（缓存生效）")

    # 3) voice_map 显式映射：不触发上传，payload 正确
    req = httpx.Request("GET", "http://x")  # 占位避免误用
    seg = SegmentRequest(
        text="测试文本",
        voice=VoiceRef(local_path="/nonexistent/a.mp3", tts_path="/nonexistent/a.mp3", display_name="男-播客1"),
        emotion_label="happy",
        speed=1.5,
    )
    eng.voice_map = {"男-播客1": "speech:mapped:uri:1"}
    audio = run(eng.synthesize_segment(seg))
    check(audio == b"RIFFfake-audio-bytes", "voice_map 命中直接合成")
    check(api.upload_calls == 0, "voice_map 命中不上传")
    p = api.last_payload or {}
    check(p.get("voice") == "speech:mapped:uri:1", "voice 参数使用映射 URI")
    check(p.get("model") == "IndexTeam/IndexTTS-2", "模型 ID 正确")
    check(p.get("input") == "测试文本", "input 为纯文本")
    check(p.get("response_format") == "wav" and p.get("sample_rate") == 24000, "输出 wav/24k")
    check(abs((p.get("speed") or 0) - 1.5) < 1e-6, "语速透传")
    check("emotion" not in p, "情绪参数不上传（SiliconFlow 未文档化）")

    # 4) 语速钳制
    seg.speed = 99
    run(eng.synthesize_segment(seg))
    check((api.last_payload or {}).get("speed") == 4.0, "语速超上限钳制到 4.0")
    seg.speed = 0.01
    run(eng.synthesize_segment(seg))
    check((api.last_payload or {}).get("speed") == 0.25, "语速超下限钳制到 0.25")

    # 5) 自动上传克隆 + 缓存：参考音频可读时上传一次，二次命中缓存
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wav = tmp / "ref.wav"
        make_wav(wav)
        cache = tmp / "sf_voices.json"
        eng2 = IndexttsSiliconflowEngine(
            api_key="sk-test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
            cache_path=cache,
        )
        seg2 = SegmentRequest(
            text="克隆测试",
            voice=VoiceRef(local_path=str(wav), display_name="ref.wav"),
            emotion_label=None,
            speed=1.0,
        )
        run(eng2.synthesize_segment(seg2))
        check(api.upload_calls == 1, "首次合成触发上传克隆")
        check(cache.exists(), "缓存文件已落盘")
        check((api.last_payload or {}).get("voice", "").startswith("speech:"), "克隆 URI 用于合成")
        run(eng2.synthesize_segment(seg2))
        check(api.upload_calls == 1, "第二次命中缓存不再上传")
        # 新引擎实例复用同一缓存文件
        eng3 = IndexttsSiliconflowEngine(
            api_key="sk-test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
            cache_path=cache,
        )
        run(eng3.synthesize_segment(seg2))
        check(api.upload_calls == 1, "跨实例缓存复用")

        # 6) 参考音频不可读且无映射 → ValueError
        seg3 = SegmentRequest(
            text="x",
            voice=VoiceRef(local_path="/nonexistent/ghost.mp3", display_name="ghost"),
            emotion_label=None,
            speed=1.0,
        )
        try:
            run(eng3.synthesize_segment(seg3))
            check(False, "不可读参考音频应报错")
        except ValueError:
            check(True, "不可读参考音频报 ValueError")

    # 7) 超长文本拒单
    seg.speed = 1.0
    seg.text = "长" * 2049
    try:
        run(eng.synthesize_segment(seg))
        check(False, "超长文本应拒单")
    except ValueError:
        check(True, "超长文本报 ValueError（应走 chunker）")

    # 8) 合成失败 → RuntimeError
    api.speech_error = (503, "Model service overloaded")
    seg.text = "正常文本"
    try:
        run(eng.synthesize_segment(seg))
        check(False, "HTTP 503 应报错")
    except RuntimeError:
        check(True, "HTTP 503 报 RuntimeError")
    api.speech_error = None

    # 9) 上传失败 → RuntimeError
    api.upload_error = (401, "Invalid token")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "r.wav"
        make_wav(wav)
        eng4 = IndexttsSiliconflowEngine(
            api_key="sk-test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        )
        seg4 = SegmentRequest(
            text="x",
            voice=VoiceRef(local_path=str(wav), display_name="r.wav"),
            emotion_label=None,
            speed=1.0,
        )
        try:
            run(eng4.synthesize_segment(seg4))
            check(False, "上传 401 应报错")
        except RuntimeError:
            check(True, "上传失败报 RuntimeError")

    print(f"\n===== {PASS} passed, {FAIL} failed =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
