"""同步接口长尾超时不得熔断整单 —— 2026-10-23 生产事故回归。

事故（任务 q_ee0bcd7bf0，双人播客 50 段 / concurrency=6 / 池 = art + modelverse）：

  48 段在 3~10s 内正常返回，**2 段卡满 300s 读超时**（`modelverse.REQUEST_TIMEOUT`）。
  旧行为把「这一次没返回」当「这台资源坏了」：

      modelverse._post_infer  ReadTimeout（不重试，怕重复计费）
        → base.py except Exception  冷却 120s + NonRetryableSynthesisError
        → podcast_runner._NO_RETRY_ERRORS 命中 ⇒ 不重试
        → raise errors[0] ⇒ 整单 FAILED（48 段已成功计费的音频一起丢弃）

修复后应有的行为（本文件逐条锁死）：

  1. ReadTimeout/连接中断 ⇒ `RetryableResourceError`（不在 NON_RETRYABLE_ERRORS 里）；
  2. 池命中该异常 ⇒ **不 pop health_state**、只短冷却 `retryable_cooldown_sec`；
  3. 紧接着的重试 ⇒ lease 到另一台资源并成功（事故里就是落到 art）；
  4. 真正的资源故障仍然是 NonRetryable，且 message 里**带上原因**（旧行为只有
     「资源 x 合成失败」，任务卡上看不出是超时还是 403）。
"""
import asyncio
import io
import os
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='retryable-test-')

_original_exists = Path.exists
with patch.object(Path, 'exists', lambda p: False if p.name == '.env' else _original_exists(p)):
    from app.engines.base import (
        EngineCapabilities,
        EnginePoolFacade,
        NonRetryableSynthesisError,
        ResourceConfig,
        RetryableResourceError,
        SegmentRequest,
        VoiceRef,
    )
    from app.engines import base as engine_base
    from app.engines.modelverse import ModelverseEngine


def wav_bytes(rate=24000, ms=30):
    """24k/单声道/PCM16 —— 与池的 NORM_RATE 一致，normalize_pcm 直接直通。"""
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        w.writeframes(b'\x10\x00' * (rate * ms // 1000))
    return out.getvalue()


def make_req():
    return SegmentRequest(
        text='这是一段测试文本',
        voice=VoiceRef(tts_path='v.wav', local_path=None, display_name='v'),
        emotion_label=None,
        speed=1.0,
    )


class FakeEngine:
    """假引擎：要么返回合法 wav，要么抛出构造时给定的异常。"""
    has_free_probe = True
    capabilities = EngineCapabilities(
        display_name='假资源',
        max_input_chars=600, max_concurrency=3,
        supports_speed=True, supports_emotion=True, normalizes_loudness=False,
    )

    def __init__(self, name, exc=None):
        self.name = name
        self.exc = exc
        self.calls = 0

    async def health(self):
        return True

    async def synthesize_segment(self, req):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return wav_bytes()


def quiet_norm():
    """屏蔽 ffmpeg / 响度归一：本文件只关心异常归因，不关心音频处理。"""
    return (
        patch.object(engine_base, 'normalize_pcm', lambda b, s=1.0: b),
        patch.object(engine_base.audio_norm, 'apply_loudness', lambda b: b),
    )


class TestModelverseReadTimeoutIsRetryable(unittest.TestCase):
    def test_read_timeout_raises_retryable_not_fatal(self):
        """ReadTimeout 必须归为可重试，且文案里留着异常类型（排障要看这个）。"""
        def handler(request):
            raise httpx.ReadTimeout('read timed out', request=request)

        async def run():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            engine = ModelverseEngine(api_key='sk-test', client=client)
            try:
                await engine._post_infer(
                    data={'model': 'IndexTeam/IndexTTS-2'},
                    files={'spk_audio_file': ('ref.wav', b'RIFF0000', 'application/octet-stream')},
                )
            finally:
                await client.aclose()

        with self.assertRaises(RetryableResourceError) as ctx:
            asyncio.run(run())
        self.assertNotIsInstance(ctx.exception, NonRetryableSynthesisError)
        self.assertIn('ReadTimeout', str(ctx.exception))

    def test_connect_error_still_retried_locally(self):
        """连接没建立（请求确定没到平台）仍就地重试，不该抛给池 —— 否则白浪费一次重试机会。"""
        seen = {'n': 0}

        def handler(request):
            seen['n'] += 1
            raise httpx.ConnectError('refused', request=request)

        async def run():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            engine = ModelverseEngine(api_key='sk-test', client=client)
            try:
                await engine._post_infer(
                    data={'model': 'm'},
                    files={'spk_audio_file': ('ref.wav', b'x', 'application/octet-stream')},
                )
            finally:
                await client.aclose()

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(run())
        self.assertNotIsInstance(ctx.exception, RetryableResourceError)
        self.assertEqual(seen['n'], 3, 'ConnectError 应该重试 CONNECT_RETRIES 次')


class TestPoolKeepsResourceAlive(unittest.TestCase):
    def _pool(self, fake_a, fake_b):
        return EnginePoolFacade([
            (ResourceConfig(id='a', provider='art', max_concurrency=3), fake_a),
            (ResourceConfig(id='b', provider='art', max_concurrency=3), fake_b),
        ])

    def test_short_cooldown_and_health_kept(self):
        """不判死资源：health_state 还在，冷却只有 retryable_cooldown_sec（30s）。"""
        fake_a = FakeEngine('a', exc=RetryableResourceError('本次未拿到音频（ReadTimeout）'))
        fake_b = FakeEngine('b')

        async def run():
            pool = self._pool(fake_a, fake_b)
            p1, p2 = quiet_norm()
            with p1, p2:
                try:
                    await pool.synthesize_segment(make_req())
                except RetryableResourceError:
                    pass
                return pool

        pool = asyncio.run(run())
        self.assertIn('a', pool.health_state, '短冷却不该 pop health_state（资源没坏）')
        self.assertTrue(pool.health_state['a'][0])
        remain = pool.cooldown_until['a'] - time.monotonic()
        self.assertGreater(remain, 0)
        self.assertLessEqual(remain, pool.retryable_cooldown_sec + 1)
        self.assertEqual(pool.retryable_cooldown_sec, 30.0)
        self.assertEqual(fake_a.calls, 1)

    def test_retry_lands_on_other_resource(self):
        """runner 重试该段时，池应把它让给另一台资源并成功（事故里 = 落到 art）。"""
        fake_a = FakeEngine('a', exc=RetryableResourceError('本次未拿到音频（ReadTimeout）'))
        fake_b = FakeEngine('b')

        async def run():
            pool = self._pool(fake_a, fake_b)
            p1, p2 = quiet_norm()
            with p1, p2:
                try:
                    await pool.synthesize_segment(make_req())
                except RetryableResourceError:
                    pass
                # 模拟 podcast_runner 的「失败段重试一次」：紧接着再调一次
                return await pool.synthesize_segment(make_req())

        audio = asyncio.run(run())
        self.assertTrue(audio.startswith(b'RIFF'))
        self.assertEqual(fake_a.calls, 1, '重试不该再打长尾的那台')
        self.assertEqual(fake_b.calls, 1, '重试应落到另一台')

    def test_real_failure_still_fatal_and_message_has_cause(self):
        """真资源故障仍是 NonRetryable，但 message 必须带原因（旧版只有包装文案）。"""
        fake_a = FakeEngine('a', exc=ValueError('平台 401 invalid api key'))
        fake_b = FakeEngine('b')
        pool = None

        async def run():
            nonlocal pool
            pool = self._pool(fake_a, fake_b)
            p1, p2 = quiet_norm()
            with p1, p2:
                await pool.synthesize_segment(make_req())

        with self.assertRaises(NonRetryableSynthesisError) as ctx:
            asyncio.run(run())
        self.assertIn('资源 a 合成失败', str(ctx.exception))
        self.assertIn('401 invalid api key', str(ctx.exception), '原因必须进 message')
        self.assertEqual(fake_b.calls, 0, '不可重试的失败不该被池挪到别的资源')

    def test_input_error_unchanged(self):
        """输入侧错误（参考音频缺失）语义不变：不冷却、不重试、原样抛。"""
        from app.engines.base import EngineInputError
        fake_a = FakeEngine('a', exc=EngineInputError('参考音频不存在'))
        fake_b = FakeEngine('b')

        async def run():
            pool = self._pool(fake_a, fake_b)
            p1, p2 = quiet_norm()
            with p1, p2:
                await pool.synthesize_segment(make_req())

        with self.assertRaises(EngineInputError):
            asyncio.run(run())

    def test_retryable_is_not_in_no_retry_list(self):
        """护栏：这个异常一旦被误加进 NON_RETRYABLE_ERRORS，整个修复就等于没做。"""
        from app import podcast_runner
        from app.engines.base import NON_RETRYABLE_ERRORS

        self.assertFalse(
            issubclass(RetryableResourceError, NON_RETRYABLE_ERRORS),
            'RetryableResourceError 必须在「会重试」的一侧',
        )
        self.assertFalse(issubclass(RetryableResourceError, podcast_runner._NO_RETRY_ERRORS))
        # 反向保护：NonRetryable 仍在名单里（别为了让本用例通过而放宽名单）
        self.assertTrue(issubclass(NonRetryableSynthesisError, NON_RETRYABLE_ERRORS))


if __name__ == '__main__':
    unittest.main(verbosity=2)
