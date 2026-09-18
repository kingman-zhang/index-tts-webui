"""302.ai IndexTTS-2 引擎适配器（indextts_302ai）单测，全 mock 不打真实 API。

运行：webui-backend 目录下
  /Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_engine_302ai.py
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
from app.engines.indextts_302ai import Indextts302aiEngine

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
        w.setframerate(22050)
        w.writeframes(b"\x00\x01" * int(22050 * dur))


class Fake302:
    """模拟 302.ai：记录调用、可注入错误与轮询状态序列。"""

    def __init__(self):
        self.upload_calls = 0
        self.submit_calls = 0
        self.poll_calls = 0
        self.health_calls = 0
        self.download_calls = 0
        self.health_status = 200
        self.upload_error: tuple[int, str] | None = None
        self.poll_states: list[str] = []  # 依次弹出的 state；空则保持最后一个
        self.fail_submit: tuple[int, str] | None = None
        self.original_download_error = False  # file.302.ai 直连抛 ConnectError
        self.last_submit_payload: dict | None = None
        self.last_upload_name: str = ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "file.302.ai":
            self.download_calls += 1
            if self.original_download_error:
                raise httpx.ConnectError("sandbox proxy killed TLS")
            return httpx.Response(200, content=b"audio-from-original-host")
        if url.host in ("file.302ai.cn", "file.302ai.com"):
            self.download_calls += 1
            return httpx.Response(200, content=b"audio-from-mirror")
        path = url.path
        if path.endswith("/302/index_tts2/task") and request.method == "GET":
            # health 与轮询共用端点：task_id 为全零 → health；否则轮询
            if url.params.get("task_id", "").startswith("00000000"):
                self.health_calls += 1
                if self.health_status != 200:
                    return httpx.Response(self.health_status, json={"error": "auth"})
                return httpx.Response(200, json={"state": "PENDING", "message": "任务等待执行中"})
            self.poll_calls += 1
            if self.poll_states:
                state = self.poll_states.pop(0)
            else:
                state = "SUCCESS"
            if state == "SUCCESS":
                return httpx.Response(200, json={
                    "state": "SUCCESS",
                    "audio_url": "https://file.302.ai/gpt/imgs/x/out.wav",
                    "duration": 3.2, "total_text_tokens": 40,
                    "sample_rate": 22050, "generation_time": 5.1,
                })
            return httpx.Response(200, json={"state": state, "message": "进行中"})
        if path.endswith("/302/index_tts2/task") and request.method == "POST":
            self.submit_calls += 1
            self.last_submit_payload = json.loads(request.content.decode("utf-8"))
            if self.fail_submit:
                return httpx.Response(self.fail_submit[0], json={"message": self.fail_submit[1]})
            return httpx.Response(200, json={"task_id": "tid-1234"})
        if path.endswith("/302/upload-file"):
            self.upload_calls += 1
            self.last_upload_name = url.params.get("name", "")
            if self.upload_error:
                return httpx.Response(self.upload_error[0], json={"message": self.upload_error[1]})
            return httpx.Response(200, json={"code": 200, "data": "https://file.302.ai/gpt/imgs/x/ref.mp3", "message": "success"})
        return httpx.Response(404, json={"message": "not found"})


def run(coro):
    return asyncio.run(coro)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="t302_"))
    ref = tmp / "ref.mp3"
    ref.write_bytes(b"fake-mp3-bytes")

    # 1) 无 Key → health False，不发请求
    api = Fake302()
    eng = Indextts302aiEngine(api_key="", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)))
    check(run(eng.health()) is False, "无 Key 时 health=False 且不请求")
    check(api.health_calls == 0, "无 Key 不发探活请求")

    # 2) 有 Key → health True（200），TTL 内只探活一次；401 → False
    api = Fake302()
    eng = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    )
    check(run(eng.health()) is True, "有 Key 探活成功")
    check(run(eng.health()) is True, "TTL 内二次探活")
    check(api.health_calls == 1, "探活请求只发一次（缓存生效）")
    api2 = Fake302()
    api2.health_status = 401
    eng2 = Indextts302aiEngine(
        api_key="sk-bad", client=httpx.AsyncClient(transport=httpx.MockTransport(api2.handler))
    )
    check(run(eng2.health()) is False, "401 → health=False")

    # 3) voice_map 显式映射：不触发上传；payload 正确（happy → one-hot）
    api = Fake302()
    eng = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        voice_map={"男-播客1": "https://file.302.ai/mapped/ref.mp3"},
    )
    seg = SegmentRequest(
        text="测试文本",
        voice=VoiceRef(local_path=str(ref), tts_path="/server/ref.mp3", display_name="男-播客1"),
        emotion_label="happy",
        speed=1.5,
    )
    audio = run(eng.synthesize_segment(seg))
    check(audio == b"audio-from-original-host", "voice_map 命中直接合成并下载")
    check(api.upload_calls == 0, "voice_map 命中不上传")
    p = api.last_submit_payload or {}
    check(p.get("speaker_audio_url") == "https://file.302.ai/mapped/ref.mp3", "speaker_audio_url 用映射 URL")
    check(p.get("emotion_vector") == [1, 0, 0, 0, 0, 0, 0, 0], "happy → one-hot 向量（EMO_VECTOR_ORDER 对齐）")
    check(p.get("text") == "测试文本", "text 透传")
    check("speed" not in p, "平台无 speed 参数（不透传）")

    # 4) neutral / None → 不带 emotion_vector（跟随音色）
    seg.emotion_label = "neutral"
    run(eng.synthesize_segment(seg))
    check("emotion_vector" not in (api.last_submit_payload or {}), "neutral → 无 emotion_vector 字段")
    seg.emotion_label = None
    run(eng.synthesize_segment(seg))
    check("emotion_vector" not in (api.last_submit_payload or {}), "None → 无 emotion_vector 字段")

    # 5) 无映射 → 自动上传 + 磁盘缓存；第二次不再上传
    api = Fake302()
    cache_file = tmp / "ai302_voices.json"
    eng = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        cache_path=cache_file,
    )
    seg2 = SegmentRequest(
        text="缓存测试",
        voice=VoiceRef(local_path=str(ref), display_name="男-播客1"),
        emotion_label=None,
        speed=1.0,
    )
    run(eng.synthesize_segment(seg2))
    check(api.upload_calls == 1, "首次自动上传参考音频")
    check((api.last_submit_payload or {}).get("speaker_audio_url") == "https://file.302.ai/gpt/imgs/x/ref.mp3", "上传返回 URL 进 payload")
    run(eng.synthesize_segment(seg2))
    check(api.upload_calls == 1, "第二次命中缓存不再上传")

    # 6) 上传失败 → RuntimeError
    api = Fake302()
    api.upload_error = (500, "boom")
    eng3 = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
    )
    try:
        run(eng3.synthesize_segment(seg2))
        check(False, "上传失败应抛 RuntimeError")
    except RuntimeError as e:
        check("上传失败" in str(e) or "boom" in str(e), "上传失败抛 RuntimeError")

    # 7) 参考音频不可读且无映射 → ValueError
    api = Fake302()
    eng4 = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
    )
    seg_bad = SegmentRequest(
        text="x", voice=VoiceRef(local_path="/nonexistent/a.mp3", display_name="某音色"),
        emotion_label=None, speed=1.0,
    )
    try:
        run(eng4.synthesize_segment(seg_bad))
        check(False, "不可读参考音频应抛 ValueError")
    except ValueError:
        check(True, "不可读参考音频抛 ValueError")

    # 8) 轮询状态机：PENDING → STARTED → SUCCESS
    api = Fake302()
    api.poll_states = ["PENDING", "STARTED", "SUCCESS"]
    eng5 = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        voice_map={"男-播客1": "https://file.302.ai/mapped/ref.mp3"},
    )
    audio = run(eng5.synthesize_segment(seg))
    check(audio == b"audio-from-original-host", "轮询三态后成功下载")
    check(api.poll_calls == 3, "按状态序列轮询 3 次")

    # 9) 失败态 → RuntimeError
    api = Fake302()
    api.poll_states = ["FAILURE"]
    eng6 = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        voice_map={"男-播客1": "https://file.302.ai/mapped/ref.mp3"},
    )
    try:
        run(eng6.synthesize_segment(seg))
        check(False, "FAILURE 态应抛 RuntimeError")
    except RuntimeError as e:
        check("任务失败" in str(e), "FAILURE 态抛 RuntimeError（含 state）")

    # 10) 轮询超时 → TimeoutError
    api = Fake302()
    api.poll_states = ["STARTED"] * 100
    eng7 = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        voice_map={"男-播客1": "https://file.302.ai/mapped/ref.mp3"},
    )
    eng7.poll_timeout = 0.5  # 测试用短超时（POLL_INTERVAL=2s，第一轮未到即超时）
    try:
        run(eng7.synthesize_segment(seg))
        check(False, "轮询超时应抛 TimeoutError")
    except TimeoutError:
        check(True, "轮询超时抛 TimeoutError")
    except RuntimeError:
        # 0.5s 内第一轮轮询已发生且返回 STARTED，2s 间隔后超时 — 视实现可能轮询一次
        check(True, "轮询超时（经 RuntimeError 包装）")

    # 11) 下载镜像 fallback：原 host 连接失败 → .cn 镜像成功
    api = Fake302()
    api.original_download_error = True
    eng8 = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        voice_map={"男-播客1": "https://file.302.ai/mapped/ref.mp3"},
    )
    audio = run(eng8.synthesize_segment(seg))
    check(audio == b"audio-from-mirror", "file.302.ai 失败 → 镜像 host 下载成功")

    # 12) extra_params 透传（emotion_alpha）
    api = Fake302()
    eng9 = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        voice_map={"男-播客1": "https://file.302.ai/mapped/ref.mp3"},
        extra_params={"emotion_alpha": 0.6},
    )
    run(eng9.synthesize_segment(seg))
    check((api.last_submit_payload or {}).get("emotion_alpha") == 0.6, "extra_params 透传 emotion_alpha")

    # 13) 超长文本 → ValueError
    api = Fake302()
    eng10 = Indextts302aiEngine(
        api_key="sk-test", client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        voice_map={"男-播客1": "https://file.302.ai/mapped/ref.mp3"},
    )
    seg_long = SegmentRequest(
        text="字" * 2001, voice=VoiceRef(local_path=str(ref), display_name="男-播客1"),
        emotion_label=None, speed=1.0,
    )
    try:
        run(eng10.synthesize_segment(seg_long))
        check(False, "超长文本应抛 ValueError")
    except ValueError:
        check(True, "超长文本抛 ValueError（chunker 提示）")

    print(f"\n===== {PASS}/{PASS + FAIL} passed =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
