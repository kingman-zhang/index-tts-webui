"""Modelverse（Compshare）IndexTTS-2 适配器（engines/modelverse.py）单测，全 mock 不打真实 API。

运行：webui-backend 目录下
  /Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_engine_modelverse.py

重点覆盖的是「写错了会静默失效」的几处（都是实测踩过的）：
  - payload 走 multipart 的 `payload` 字段（JSON 字符串），不是 JSON body
  - 参考音频必须是 `spk_audio_file` 文件部分，缺它就是 400
  - speed 真生效 ⇒ 1.0 时干脆不传（少一个平台侧分支）
  - neutral / None 不带任何 emo_* 字段（跟随参考音频）
  - 平台把 WAV 谎报成 audio/mpeg ⇒ 只能认字节，不能认 Content-Type
  - 输入侧错误（无参考音频 / 超长）必须抛 EngineInputError，否则整台资源被冷却
"""
import asyncio
import json
import re
import sys
import tempfile
import wave
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.base import EngineInputError, SegmentRequest, VoiceRef
from app.engines.modelverse import MAX_INPUT_CHARS, ModelverseEngine, SAMPLE_RATE

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


def make_wav_bytes(dur: float = 0.3, rate: int = SAMPLE_RATE) -> bytes:
    """造一段真正的 RIFF/WAV（适配器只认字节，测试也别用假字节）。"""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(rate * dur))
    return buf.getvalue()


def parse_multipart(request: httpx.Request) -> tuple[dict, dict]:
    """把 MockTransport 收到的 multipart 请求拆成 (文本字段, 文件字段)。"""
    ctype = request.headers.get("content-type", "")
    boundary = ctype.split("boundary=")[1].encode()
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for chunk in request.content.split(b"--" + boundary):
        if b"Content-Disposition" not in chunk:
            continue
        head, _, body = chunk.partition(b"\r\n\r\n")
        body = body[:-2] if body.endswith(b"\r\n") else body
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        if b"filename=" in head:
            files[name] = body
        else:
            fields[name] = body.decode("utf-8")
    return fields, files


class FakeMV:
    """模拟 api.modelverse.cn：记录调用、可注入错误。"""

    def __init__(self):
        self.synth_calls = 0        # 真正打到服务端的提交次数
        self.health_calls = 0
        self.last_fields: dict = {}
        self.last_files: dict = {}
        self.last_model_path = ""
        self.health_status = 200
        self.synth_status = 200
        self.synth_body: bytes | None = None       # None → 返回合法 wav
        self.synth_content_type = "audio/mpeg"     # 平台谎报的类型
        self.connect_fails = 0                     # 前 N 次提交抛 ConnectError

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/models/"):
            self.health_calls += 1
            self.last_model_path = path
            if self.health_status != 200:
                return httpx.Response(self.health_status, json={"error": "invalid api key"})
            return httpx.Response(200, json={"model": "IndexTeam/IndexTTS-2", "pricing": []})
        if request.method == "POST" and path == "/v1/audio/infer":
            if self.connect_fails > 0:
                self.connect_fails -= 1
                raise httpx.ConnectError("tls tunnel killed")
            self.synth_calls += 1
            self.last_fields, self.last_files = parse_multipart(request)
            if self.synth_status != 200:
                # 平台的两种错误体形状之一：error 是**字符串**
                return httpx.Response(self.synth_status, json={"error": "invalid api key"})
            body = self.synth_body if self.synth_body is not None else make_wav_bytes()
            return httpx.Response(
                200, content=body, headers={"content-type": self.synth_content_type}
            )
        return httpx.Response(404, json={"error": "not found"})


def run(coro):
    return asyncio.run(coro)


def build(fake: FakeMV, **kwargs) -> ModelverseEngine:
    return ModelverseEngine(
        api_key=kwargs.pop("api_key", "sk-test"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        **kwargs,
    )


def payload_of(fake: FakeMV) -> dict:
    return json.loads(fake.last_fields.get("payload") or "{}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="tmv_"))
    ref = tmp / "女-嚣张小姐.mp3"
    ref.write_bytes(b"fake-mp3-bytes")
    voice = VoiceRef(local_path=str(ref), display_name="女-嚣张小姐")

    # 1) 能力声明（这几项写错会导致上层多变速一次 / 切错长度）
    caps = ModelverseEngine.capabilities
    check(caps.max_input_chars == 600, "max_input_chars=600（平台硬上限）")
    check(caps.supports_speed is True, "supports_speed=True（实测 speed 真生效）")
    check(caps.supports_emotion is True, "supports_emotion=True")
    check(ModelverseEngine.has_free_probe is True, "有免费探活端点 ⇒ has_free_probe=True")

    # 2) 探活
    fake = FakeMV()
    eng = build(fake, api_key="")
    check(run(eng.health()) is False, "无 Key → health=False")
    check(fake.health_calls == 0, "无 Key 不发探活请求")

    fake = FakeMV()
    eng = build(fake)
    check(run(eng.health()) is True, "有 Key → 探活成功")
    check(fake.last_model_path == "/v1/models/IndexTeam/IndexTTS-2", "探活打的是 /v1/models/{model}")
    check(run(eng.health()) is True, "TTL 内二次探活")
    check(fake.health_calls == 1, "TTL 内只探活一次（有缓存）")

    fake = FakeMV()
    fake.health_status = 401
    eng = build(fake, api_key="sk-bad")
    check(run(eng.health()) is False, "401 → health=False")
    check("401" in (eng.last_health_note or ""), "探活失败留下可读原因（含状态码）")

    # 3) 基本合成：multipart 结构 + payload 字段
    fake = FakeMV()
    eng = build(fake)
    audio = run(eng.synthesize_segment(
        SegmentRequest(text="测试文本", voice=voice, emotion_label=None, speed=1.0)))
    check(audio[:4] == b"RIFF", "返回的是 WAV 字节（尽管 Content-Type 谎报 audio/mpeg）")
    check(fake.last_fields.get("model") == "IndexTeam/IndexTTS-2", "multipart 带 model 字段")
    check(fake.last_files.get("spk_audio_file") == b"fake-mp3-bytes", "参考音频走 spk_audio_file 文件部分")
    p = payload_of(fake)
    check(p.get("input") == "测试文本", "payload.input 透传正文")
    check(p.get("sample_rate") == 24000, "sample_rate=24000（与 NORM_RATE 一致，池里不再重采样）")
    check(p.get("interval_silence") == 200, "interval_silence=200")
    check("speed" not in p, "speed=1.0 时不传 speed（少一个平台分支）")
    check(not any(k.startswith("emo") for k in p), "无情感标签 → 不带任何 emo_* 字段")

    # 4) 语速：非 1.0 必须真传
    fake = FakeMV()
    eng = build(fake)
    run(eng.synthesize_segment(
        SegmentRequest(text="语速测试", voice=voice, emotion_label=None, speed=1.5)))
    check(payload_of(fake).get("speed") == 1.5, "speed=1.5 → payload.speed=1.5")

    # 5) 情感：8 标签 → method=2 + one-hot（顺序对齐 EMO_VECTOR_ORDER）
    fake = FakeMV()
    eng = build(fake)
    run(eng.synthesize_segment(
        SegmentRequest(text="情感测试", voice=voice, emotion_label="happy", speed=1.0)))
    p = payload_of(fake)
    check(p.get("emo_control_method") == 2, "happy → emo_control_method=2（情感向量）")
    check(p.get("emo_vec") == [0.75, 0, 0, 0, 0, 0, 0, 0], "happy → 第 1 维 0.75（与 302.ai 同表）")
    check(p.get("emo_alpha") == 1.0, "emo_alpha=1.0")

    run(eng.synthesize_segment(
        SegmentRequest(text="x", voice=voice, emotion_label="melancholic", speed=1.0)))
    check(payload_of(fake).get("emo_vec") == [0, 0, 0, 0, 0, 0.6, 0, 0],
          "melancholic → 第 6 维 0.6（不是第 3 维：EMO_VECTOR_ORDER 的坑）")

    # neutral / None 两种都必须干净
    for label in ("neutral", None):
        fake = FakeMV()
        eng = build(fake)
        run(eng.synthesize_segment(
            SegmentRequest(text="中性", voice=voice, emotion_label=label, speed=1.0)))
        p = payload_of(fake)
        check(not any(k.startswith("emo") for k in p),
              f"emotion_label={label!r} → 不带任何 emo_* 字段（跟随参考音频）")

    # 6) 停顿标记兜底：正常路径已被 split_by_pauses 切走，旁路调用时不能念出来
    fake = FakeMV()
    eng = build(fake)
    run(eng.synthesize_segment(
        SegmentRequest(text="前半句[pause:1.5]后半句", voice=voice, emotion_label=None, speed=1.0)))
    check(payload_of(fake).get("input") == "前半句，后半句", "[pause:N] 被替换成全角逗号")

    # 7) 输入侧错误：缺参考音频 / 文件不存在 / 超长 —— 都必须是 EngineInputError
    #    （抛 ValueError 会被资源池当成平台故障，白白冷却 120s）
    fake = FakeMV()
    eng = build(fake)
    try:
        run(eng.synthesize_segment(
            SegmentRequest(text="无音色", voice=VoiceRef(local_path=None, display_name="某音色"),
                           emotion_label=None, speed=1.0)))
        check(False, "缺 local_path 应抛 EngineInputError")
    except EngineInputError as e:
        check("local_path" in str(e) or "参考音频" in str(e), "缺 local_path → EngineInputError")
    check(fake.synth_calls == 0, "输入侧错误不提交（不产生费用）")

    try:
        run(eng.synthesize_segment(
            SegmentRequest(text="文件不存在", voice=VoiceRef(local_path=str(tmp / "nope.mp3")),
                           emotion_label=None, speed=1.0)))
        check(False, "参考音频不存在应抛 EngineInputError")
    except EngineInputError:
        check(True, "参考音频不存在 → EngineInputError")

    try:
        run(eng.synthesize_segment(
            SegmentRequest(text="字" * (MAX_INPUT_CHARS + 1), voice=voice,
                           emotion_label=None, speed=1.0)))
        check(False, "超长文本应抛 EngineInputError")
    except EngineInputError as e:
        check(str(MAX_INPUT_CHARS) in str(e), "超长 → EngineInputError（报出上限值）")
    check(fake.synth_calls == 0, "超长输入不提交")

    # 8) 恰好等于上限要放行（边界不能差一）
    fake = FakeMV()
    eng = build(fake)
    run(eng.synthesize_segment(
        SegmentRequest(text="字" * MAX_INPUT_CHARS, voice=voice, emotion_label=None, speed=1.0)))
    check(fake.synth_calls == 1, f"恰好 {MAX_INPUT_CHARS} 字符放行（边界不差一）")

    # 9) 平台报错：字符串型 error 也要能读出来
    fake = FakeMV()
    fake.synth_status = 401
    eng = build(fake, api_key="sk-bad")
    try:
        run(eng.synthesize_segment(
            SegmentRequest(text="鉴权失败", voice=voice, emotion_label=None, speed=1.0)))
        check(False, "非 200 应抛 RuntimeError")
    except RuntimeError as e:
        check("invalid api key" in str(e), "错误体是字符串时也能读出原因")
    except EngineInputError:
        check(False, "鉴权失败不该被归因为输入侧错误")

    # 10) 200 但塞着 JSON 错误体 / 空音频 —— 都不能当成音频返回
    fake = FakeMV()
    fake.synth_body = b'{"error": "quota exceeded"}'
    eng = build(fake)
    try:
        run(eng.synthesize_segment(
            SegmentRequest(text="配额", voice=voice, emotion_label=None, speed=1.0)))
        check(False, "200 + JSON 错误体应抛 RuntimeError")
    except RuntimeError as e:
        check("quota exceeded" in str(e), "200 + JSON 错误体 → RuntimeError（含原因）")

    fake = FakeMV()
    fake.synth_body = b""
    eng = build(fake)
    try:
        run(eng.synthesize_segment(
            SegmentRequest(text="空音频", voice=voice, emotion_label=None, speed=1.0)))
        check(False, "空音频应抛 RuntimeError")
    except RuntimeError as e:
        check("空音频" in str(e), "空音频 → RuntimeError")

    # 11) 连接级重试：前两次 ConnectError（请求没到服务器）→ 第三次成功，只算一次提交
    fake = FakeMV()
    fake.connect_fails = 2
    eng = build(fake)
    audio = run(eng.synthesize_segment(
        SegmentRequest(text="重试", voice=voice, emotion_label=None, speed=1.0)))
    check(audio[:4] == b"RIFF", "连接中断重试后成功")
    check(fake.synth_calls == 1, "连接失败的尝试不计入服务端提交次数")

    # 12) 连接持续失败 → RuntimeError（含「重试」字样，不静默）
    fake = FakeMV()
    fake.connect_fails = 99
    eng = build(fake)
    try:
        run(eng.synthesize_segment(
            SegmentRequest(text="持续失败", voice=voice, emotion_label=None, speed=1.0)))
        check(False, "持续连接失败应抛 RuntimeError")
    except RuntimeError as e:
        check("重试" in str(e) or "连接失败" in str(e), "持续连接失败 → RuntimeError")

    # 13) 语速越界不在本地夹（会被上层钳，本地悄悄改值等于吞掉用户设置）
    fake = FakeMV()
    eng = build(fake)
    run(eng.synthesize_segment(
        SegmentRequest(text="越界语速", voice=voice, emotion_label=None, speed=0.25)))
    check(payload_of(fake).get("speed") == 0.25, "speed=0.25 原样透传（不在本地夹）")

    # 14) 工厂接线：YAML 里写 provider: modelverse 要能被派发，能力要进池
    #     （provider 分支写错、或忘了注册凭据变量名，只会在运行时才炸）
    import os
    from app.engines import factory as factory_mod

    os.environ["TTS_RESOURCES"] = (
        "- id: mv\n"
        "  provider: modelverse\n"
        "  api_key_env: MODELVERSE_API_KEY\n"
        "  max_concurrency: 3\n"
    )
    os.environ["MODELVERSE_API_KEY"] = "sk-test"
    factory_mod.reset_registry()
    pool = factory_mod.build_registry(force=True).engines[0]
    cfg, mv_engine = pool.resources[0]
    check(isinstance(mv_engine, ModelverseEngine), "provider: modelverse 派发出 ModelverseEngine")
    check(cfg.id == "mv" and cfg.tier == "cloud", "资源 id/tier 正确（cloud）")
    check(pool.capabilities.max_input_chars == 600, "池向 runner 声明的上限 = 600")
    check(pool.capabilities.supports_speed is True, "池声明支持原生语速（不再补 atempo）")
    check(pool.capabilities.speed_guaranteed is True, "speed_guaranteed=True（上层不得再变速）")
    summary = factory_mod.engine_summary()[0]
    check(summary["resources"][0]["provider"] == "modelverse", "engine_summary 透出 provider")
    check(summary["max_input_chars"] == 600, "engine_summary 透出 max_input_chars")

    # 缺凭据时点名到变量名（而不是含糊的「凭据不完整」）
    os.environ["MODELVERSE_API_KEY"] = ""
    factory_mod.reset_registry()
    try:
        factory_mod.build_registry(force=True)
        check(False, "缺凭据应报错")
    except ValueError as e:
        check("MODELVERSE_API_KEY" in str(e), "缺凭据时报错点名 MODELVERSE_API_KEY")

    print(f"\n===== {PASS}/{PASS + FAIL} passed =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
