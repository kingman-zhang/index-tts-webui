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
        self.transcribe_calls = 0
        self.speech_error: tuple[int, str] | None = None
        self.upload_error: tuple[int, str] | None = None
        self.last_payload: dict | None = None
        self.last_upload_content: bytes = b""

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url.path
        if url.endswith("/audio/voice/list"):
            self.health_calls += 1
            return httpx.Response(200, json={"result": []})
        if url.endswith("/audio/transcriptions"):
            self.transcribe_calls += 1
            return httpx.Response(200, json={"text": "自动转写的参考音频内容。"})
        if url.endswith("/uploads/audio/voice"):
            self.upload_calls += 1
            self.last_upload_content = request.content
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
    check(p.get("model") == "FunAudioLLM/CosyVoice2-0.5B", "默认模型为 CosyVoice2（国内站实测可用）")
    check(
        p.get("input") == "请用开心喜悦的语气说。<|endofprompt|>测试文本",
        "happy 情绪 → CosyVoice2 内联提示前缀",
    )
    check(p.get("response_format") == "wav" and p.get("sample_rate") == 24000, "输出 wav/24k")
    check(abs((p.get("speed") or 0) - 1.5) < 1e-6, "语速透传")
    check("emotion" not in p, "无独立 emotion 字段（情绪走 input 内联提示）")

    # 3b) neutral / None 加「自然平稳」兜底前缀（真机实测：无前缀合成极不稳定）；
    #     非 CosyVoice2 模型忽略情绪
    seg.emotion_label = "neutral"
    run(eng.synthesize_segment(seg))
    check(
        (api.last_payload or {}).get("input") == "请用自然平稳的语气说。<|endofprompt|>测试文本",
        "neutral 加自然平稳兜底前缀",
    )
    seg.emotion_label = None
    run(eng.synthesize_segment(seg))
    check(
        (api.last_payload or {}).get("input") == "请用自然平稳的语气说。<|endofprompt|>测试文本",
        "None 同样加兜底前缀",
    )
    eng.model = "IndexTeam/IndexTTS-2"
    seg.emotion_label = "happy"
    run(eng.synthesize_segment(seg))
    check((api.last_payload or {}).get("input") == "测试文本", "IndexTTS-2 模型忽略情绪标签")
    eng.model = "fnlp/MOSS-TTSD-v0.5"
    run(eng.synthesize_segment(seg))
    check((api.last_payload or {}).get("input") == "[S1]测试文本", "MOSS-TTSD 加 [S1] 说话人前缀")
    run(eng.synthesize_segment(seg))
    check((api.last_payload or {}).get("input") == "[S1]测试文本", "MOSS-TTSD 已有标记不重复加")
    eng.model = "FunAudioLLM/CosyVoice2-0.5B"

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
        check(api.transcribe_calls == 1, "克隆前自动 ASR 转写参考音频")
        check("自动转写的参考音频内容".encode() in api.last_upload_content, "上传使用 ASR 转写文本")
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

    # 10) 坏头 wav 修复：SiliconFlow 流式 wav 的 data 大小字段=0xFFFFFFFF
    import io
    import struct
    import wave as wavmod

    def make_broken_wav(path, nframes=12000):
        """生成 data 大小字段损坏的 wav（模拟 SiliconFlow 返回）。"""
        buf = io.BytesIO()
        with wavmod.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x01" * nframes)
        b = bytearray(buf.getvalue())
        # 找到 data 块，把大小字段改成 0xFFFFFFFF
        pos = 12
        while pos + 8 <= len(b):
            cid = bytes(b[pos:pos + 4])
            sz = struct.unpack("<I", b[pos + 4:pos + 8])[0]
            if cid == b"data":
                b[pos + 4:pos + 8] = struct.pack("<I", 0xFFFFFFFF)
                break
            pos += 8 + sz + (sz & 1)
        path.write_bytes(bytes(b))

    with tempfile.TemporaryDirectory() as td:
        broken = Path(td) / "broken.wav"
        make_broken_wav(broken, nframes=12000)
        # 直接验证修复函数
        repaired = IndexttsSiliconflowEngine._repair_wav(broken.read_bytes())
        with wavmod.open(io.BytesIO(repaired)) as w:
            check(
                (w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth())
                == (12000, 24000, 1, 2),
                "坏头 wav 修复后帧数/参数正确",
            )
        # 正常 wav 不被误改
        good = Path(td) / "good.wav"
        make_wav(good)
        check(
            IndexttsSiliconflowEngine._repair_wav(good.read_bytes()) == good.read_bytes(),
            "正常 wav 原样通过",
        )
        # 端到端：mock 返回坏头 wav，引擎应返回修复后的
        api2 = FakeAPI()
        buf = io.BytesIO()
        with wavmod.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x01" * 12000)
        ba = bytearray(buf.getvalue())
        pos = 12
        while pos + 8 <= len(ba):
            cid = bytes(ba[pos:pos + 4])
            sz = struct.unpack("<I", ba[pos + 4:pos + 8])[0]
            if cid == b"data":
                ba[pos + 4:pos + 8] = struct.pack("<I", 0xFFFFFFFF)
                break
            pos += 8 + sz + (sz & 1)
        api2_broken = bytes(ba)
        orig_handler = FakeAPI.handler.__func__ if hasattr(FakeAPI.handler, "__func__") else FakeAPI.handler

        def broken_speech_handler(self, request):
            if request.url.path.endswith("/audio/speech"):
                self.speech_calls += 1
                self.last_payload = json.loads(request.content.decode("utf-8"))
                return httpx.Response(200, headers={"content-type": "audio/wav"}, content=api2_broken)
            return FakeAPI.handler(self, request)

        eng5 = IndexttsSiliconflowEngine(
            api_key="sk-test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: broken_speech_handler(api2, r))),
            voice_map={"x.mp3": "speech:mapped:x"},
        )
        seg5 = SegmentRequest(
            text="坏头测试",
            voice=VoiceRef(local_path="/nonexistent/x.mp3", display_name="x.mp3"),
            emotion_label=None,
            speed=1.0,
        )
        out5 = run(eng5.synthesize_segment(seg5))
        with wavmod.open(io.BytesIO(out5)) as w:
            check(w.getnframes() == 12000, "端到端：引擎返回的 wav 已修复（帧数正确）")

    print(f"\n===== {PASS} passed, {FAIL} failed =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
