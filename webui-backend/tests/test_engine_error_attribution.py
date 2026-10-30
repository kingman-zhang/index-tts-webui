"""异常归因：302.ai 的失败要落到正确的类，池的处置才正确（全 mock，不打真机）。

背景（2026-10-30）：302.ai 适配器此前把**所有**失败都抛成裸 RuntimeError /
ValueError，于是池一律按「资源故障 + 可能已计费」处理 —— 冷却整台平台 120s、
该段禁止重试、整单失败。而 2026-10-23 modelverse 的同类问题早已用
`RetryableResourceError` 修过（短冷却 30s + 换资源重试该段），302.ai 没跟上。

本文件锁三件事：
  1. 输入侧错误（参考音频不可读 / 超长）→ EngineInputError：池**不冷却**资源。
  2. 「这一次没拿到结果」（连接耗尽 / 读超时 / 轮询超时 / 轮询连续断连）
     → RetryableResourceError：池只短冷却 retryable_cooldown_sec，健康位保留。
  3. 平台明确失败 / 轮询连续非 200 → 仍判死（NonRetryableSynthesisError，冷却 120s）。

运行：webui-backend 目录下
  /Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python3 \
      tests/test_engine_error_attribution.py
"""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines import indextts_302ai as m302
from app.engines.base import (
    EngineCapabilities,
    EngineInputError,
    EnginePoolFacade,
    NonRetryableSynthesisError,
    ResourceConfig,
    RetryableResourceError,
    SegmentRequest,
    VoiceRef,
)
from app.engines.indextts_302ai import Indextts302aiEngine

VOICE_MAP = {"v1": "https://file.302.ai/mapped/ref.mp3"}
AUDIO = b"RIFF____WAVEfmt "  # 适配器不解析音频，原样返回


class Fake302:
    """按脚本回放的 302 假平台。"""

    def __init__(self, poll_script=None, submit_exc=None, upload_exc=None, upload_status=None):
        # poll_script 元素：("json", {..}) / ("status", 500) / ("exc", Exception 实例)
        self.poll_script = poll_script or [("json", {"state": "SUCCESS", "audio_url": "https://file.302.ai/a.wav"})]
        self.submit_exc = submit_exc
        self.upload_exc = upload_exc
        self.upload_status = upload_status
        self.submit_calls = 0
        self.poll_calls = 0
        self.upload_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "/302/upload-file" in str(request.url):
            self.upload_calls += 1
            if self.upload_exc is not None:
                raise self.upload_exc
            if self.upload_status:
                return httpx.Response(self.upload_status, text="upload boom")
            return httpx.Response(200, json={"data": "https://file.302.ai/uploaded/ref.mp3"})
        if request.method == "POST":
            self.submit_calls += 1
            if self.submit_exc is not None:
                raise self.submit_exc
            return httpx.Response(200, json={"task_id": "t-1"})
        self.poll_calls += 1
        kind, payload = self.poll_script[min(self.poll_calls - 1, len(self.poll_script) - 1)]
        if kind == "exc":
            raise payload
        if kind == "status":
            return httpx.Response(payload, text="gateway boom")
        return httpx.Response(200, json=payload)

    # 下载域：音频直出
    def audio_handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=AUDIO)


def make_engine(fake: Fake302, **kwargs) -> Indextts302aiEngine:
    def route(request: httpx.Request) -> httpx.Response:
        if "file.302.ai" in str(request.url):
            return fake.audio_handler(request)
        return fake.handler(request)

    return Indextts302aiEngine(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(route)),
        voice_map=VOICE_MAP,
        **kwargs,
    )


REQ = SegmentRequest(text="测试一句话", voice=VoiceRef(display_name="v1"))


class Test302aiAttribution(unittest.IsolatedAsyncioTestCase):
    """适配器层：抛出的类型必须能被池正确识别。"""

    async def test_read_timeout_is_retryable(self):
        """提交读超时（请求已发出、结果没拿到）→ RetryableResourceError。"""
        fake = Fake302(submit_exc=httpx.ReadTimeout("read timed out"))
        eng = make_engine(fake)
        with self.assertRaises(RetryableResourceError) as ctx:
            await eng.synthesize_segment(REQ)
        self.assertIn("未拿到音频", str(ctx.exception))

    async def test_connect_exhausted_is_retryable(self):
        """连接重试耗尽（请求确定没到平台）→ RetryableResourceError，而不是判死。"""
        fake = Fake302(submit_exc=httpx.ConnectError("refused"))
        eng = make_engine(fake)
        with patch.object(m302, "_CONNECT_RETRY_BASE_S", 0):
            with self.assertRaises(RetryableResourceError) as ctx:
                await eng.synthesize_segment(REQ)
        self.assertEqual(fake.submit_calls, m302._CONNECT_RETRIES, "连接类错误仍应就地重试")
        self.assertIn("未到达平台", str(ctx.exception))

    async def test_poll_timeout_is_retryable(self):
        """轮询超时（提交了、平台还在跑）→ RetryableResourceError。"""
        fake = Fake302(poll_script=[("json", {"state": "PENDING"})])
        eng = make_engine(fake)
        eng.poll_timeout = 0.05
        with patch.object(m302, "POLL_INTERVAL", 0.01):
            with self.assertRaises(RetryableResourceError) as ctx:
                await eng.synthesize_segment(REQ)
        self.assertIn("轮询超时", str(ctx.exception))

    async def test_poll_network_flaky_is_retryable(self):
        """轮询连续断连 5 次 → RetryableResourceError。"""
        fake = Fake302(poll_script=[("exc", httpx.ConnectError("blip"))])
        eng = make_engine(fake)
        eng.poll_timeout = 5.0
        with patch.object(m302, "POLL_INTERVAL", 0.01):
            with self.assertRaises(RetryableResourceError) as ctx:
                await eng.synthesize_segment(REQ)
        self.assertIn("连续断连", str(ctx.exception))

    async def test_poll_http_5xx_once_recovers(self):
        """轮询偶发 500 一次不该判死（网关抖动），后续 SUCCESS 照常返回。"""
        fake = Fake302(poll_script=[
            ("status", 500),
            ("json", {"state": "SUCCESS", "audio_url": "https://file.302.ai/a.wav"}),
        ])
        eng = make_engine(fake)
        with patch.object(m302, "POLL_INTERVAL", 0.01):
            audio = await eng.synthesize_segment(REQ)
        self.assertEqual(audio, AUDIO)
        self.assertEqual(fake.poll_calls, 2, "第一次 500 后应继续轮询")

    async def test_poll_http_5xx_persistent_is_fatal(self):
        """轮询连续 3 次非 200 → 判死（不是 Retryable，避免重复计费）。"""
        fake = Fake302(poll_script=[("status", 502)])
        eng = make_engine(fake)
        with patch.object(m302, "POLL_INTERVAL", 0.01):
            with self.assertRaises(RuntimeError) as ctx:
                await eng.synthesize_segment(REQ)
        self.assertNotIsInstance(ctx.exception, RetryableResourceError)
        self.assertEqual(fake.poll_calls, m302._POLL_HTTP_RETRIES)

    async def test_platform_failure_state_is_fatal(self):
        """平台明确 state=FAILURE → 判死（不重试）。"""
        fake = Fake302(poll_script=[("json", {"state": "FAILURE", "message": "bad text"})])
        eng = make_engine(fake)
        with patch.object(m302, "POLL_INTERVAL", 0.01):
            with self.assertRaises(RuntimeError) as ctx:
                await eng.synthesize_segment(REQ)
        self.assertNotIsInstance(ctx.exception, RetryableResourceError)
        self.assertIn("state=FAILURE", str(ctx.exception))

    async def test_unreadable_voice_is_input_error(self):
        """参考音频不可读且无映射 → EngineInputError（不是 ValueError）。"""
        eng = Indextts302aiEngine(
            api_key="sk-test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(Fake302().handler)),
        )
        bad = SegmentRequest(text="x", voice=VoiceRef(local_path="/nonexistent/a.mp3", display_name="ghost"))
        with self.assertRaises(EngineInputError):
            await eng.synthesize_segment(bad)

    async def test_too_long_is_input_error(self):
        """超长输入 → EngineInputError。"""
        eng = make_engine(Fake302())
        long_req = SegmentRequest(text="字" * (m302.MAX_INPUT_CHARS + 1),
                                  voice=VoiceRef(display_name="v1"))
        with self.assertRaises(EngineInputError):
            await eng.synthesize_segment(long_req)

    # ---- 参考音频上传阶段（2026-10-30 q_d2003547d7：这里漏了归因）----
    def _upload_engine(self, tmpdir: str, fake: Fake302):
        return Indextts302aiEngine(
            api_key="sk-test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
            cache_path=Path(tmpdir) / "cache.json",
        )

    def _upload_req(self, tmpdir: str) -> SegmentRequest:
        ref = Path(tmpdir) / "ref.wav"
        ref.write_bytes(AUDIO)
        # 名字不在 voice_map 里 ⇒ 必然走「自动上传」分支
        return SegmentRequest(text="x", voice=VoiceRef(local_path=str(ref), display_name="未映射音色"))

    async def test_upload_read_timeout_is_retryable(self):
        """上传读超时 → RetryableResourceError。上传阶段还没提交任何任务，0 计费风险。"""
        with tempfile.TemporaryDirectory() as td:
            fake = Fake302(upload_exc=httpx.ReadTimeout(""))
            eng = self._upload_engine(td, fake)
            with self.assertRaises(RetryableResourceError) as ctx:
                await eng.synthesize_segment(self._upload_req(td))
        self.assertEqual(fake.submit_calls, 0, "上传失败就不该继续提交任务")
        self.assertIn("未创建合成任务", str(ctx.exception))

    async def test_upload_5xx_is_retryable(self):
        """上传端点 5xx（平台抽风）→ RetryableResourceError，而不是整单判死。"""
        with tempfile.TemporaryDirectory() as td:
            fake = Fake302(upload_status=502)
            eng = self._upload_engine(td, fake)
            with self.assertRaises(RetryableResourceError):
                await eng.synthesize_segment(self._upload_req(td))
        self.assertEqual(fake.submit_calls, 0)

    async def test_upload_4xx_stays_fatal(self):
        """上传 4xx（凭据/参数问题）仍判死：换台资源也没用，要让人看见。"""
        with tempfile.TemporaryDirectory() as td:
            fake = Fake302(upload_status=401)
            eng = self._upload_engine(td, fake)
            with self.assertRaises(RuntimeError) as ctx:
                await eng.synthesize_segment(self._upload_req(td))
        self.assertNotIsInstance(ctx.exception, RetryableResourceError)


class _ScriptedEngine:
    """池层测试用：按需抛出指定异常。"""

    capabilities = EngineCapabilities("scripted", max_input_chars=2000)

    def __init__(self, exc: BaseException):
        self.exc = exc

    async def health(self) -> bool:
        return True

    async def synthesize_segment(self, req):
        raise self.exc


class TestPoolDisposition(unittest.IsolatedAsyncioTestCase):
    """池层：同一份异常在两个层面必须一致（不冷却 ⇔ 不重试）。"""

    def _pool(self, exc):
        cfg = ResourceConfig("r1", "302ai", max_concurrency=2)
        pool = EnginePoolFacade([(cfg, _ScriptedEngine(exc))],
                                cooldown_sec=120.0, health_ttl=15.0, retryable_cooldown_sec=30.0)
        return pool

    def _remaining(self, pool) -> float:
        return pool.cooldown_until["r1"] - time.monotonic()

    async def test_input_error_does_not_isolate_resource(self):
        """EngineInputError：不冷却、健康位保留、异常原样抛（文案才能指对方向）。"""
        pool = self._pool(EngineInputError("参考音频不可读"))
        with self.assertRaises(EngineInputError):
            await pool.synthesize_segment(REQ)
        self.assertFalse(pool.in_cooldown("r1"), "输入侧错误不得冷却整台资源")
        self.assertIn("r1", pool.health_state, "健康位不该被清空")
        self.assertIs(pool.health_state["r1"][0], True)

    async def test_retryable_error_short_cooldown_keeps_health(self):
        """RetryableResourceError：短冷却 30s、健康位保留（资源没坏，只是这轮让给别台）。"""
        pool = self._pool(RetryableResourceError("这次没返回"))
        with self.assertRaises(RetryableResourceError):
            await pool.synthesize_segment(REQ)
        self.assertTrue(pool.in_cooldown("r1"))
        self.assertAlmostEqual(self._remaining(pool), 30.0, delta=3.0)
        self.assertIs(pool.health_state["r1"][0], True, "不得把资源判死")

    async def test_unknown_error_is_fatal_and_long_cooldown(self):
        """其它异常：冷却 120s + 清空健康位 + 包成 NonRetryableSynthesisError。"""
        pool = self._pool(RuntimeError("提交失败 HTTP 400"))
        with self.assertRaises(NonRetryableSynthesisError) as ctx:
            await pool.synthesize_segment(REQ)
        self.assertTrue(pool.in_cooldown("r1"))
        self.assertAlmostEqual(self._remaining(pool), 120.0, delta=3.0)
        self.assertNotIn("r1", pool.health_state)
        self.assertIn("禁止自动重试", str(ctx.exception))
        self.assertIn("提交失败 HTTP 400", str(ctx.exception), "原始原因必须进 message（任务卡只显示 str(e)）")

    async def test_empty_exception_message_falls_back_to_type_name(self):
        """`str(httpx.ReadTimeout(""))` 是空串 ⇒ message 里要退回类型名，不能只剩「原因：」。"""
        pool = self._pool(httpx.ReadTimeout(""))
        with self.assertRaises(NonRetryableSynthesisError) as ctx:
            await pool.synthesize_segment(REQ)
        self.assertIn("ReadTimeout", str(ctx.exception), "空原因时必须写异常类型名")


if __name__ == "__main__":
    unittest.main(verbosity=2)
