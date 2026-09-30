"""完全离线：禁止读取 .env，所有请求均使用假引擎。"""
import asyncio
import io
import json
import math
import os
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='pool-test-')
_original_exists = Path.exists
with patch.object(Path, 'exists', lambda p: False if p.name == '.env' else _original_exists(p)):
    from app.engines.base import (EngineCapabilities, EnginePoolFacade, ResourceConfig,
                                  SegmentRequest, SynthesisCancelled, VoiceRef,
                                  NonRetryableSynthesisError)
    from app.engines import base as engine_base
    from app.engines import factory
    from app import mono_runner, podcast_runner


def wav(rate=24000, channels=1, width=2, ms=10):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        w.writeframes(b'\0' * (rate * ms // 1000 * channels * width))
    return out.getvalue()


def tone(ms=400, rate=24000, freq=220.0):
    """正弦音（非静音）：响度可测 ⇒ 走固定增益路径，时长严格保持，便于断言变速次数。"""
    frames = b''.join(struct.pack('<h', int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / rate)))
                      for i in range(rate * ms // 1000))
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        w.writeframes(frames)
    return out.getvalue()


def duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as r:
        return r.getnframes() / r.getframerate()


class Fake:
    capabilities = EngineCapabilities('fake', max_input_chars=2000)
    def __init__(self):
        self.calls = self.probes = self.active = self.peak = 0
        self.online = True
        self.error = None
        self.gate = None
        self.audio = wav()
        self.last_speed = None
    async def health(self):
        self.probes += 1
        await asyncio.sleep(0)
        return self.online
    async def synthesize_segment(self, req):
        self.calls += 1
        self.active += 1
        self.last_speed = req.speed
        self.peak = max(self.peak, self.active)
        try:
            if self.gate:
                await self.gate.wait()
            if self.error:
                raise self.error
            return self.audio
        finally:
            self.active -= 1


REQ = SegmentRequest('测试', VoiceRef())


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_local_probe_ttl_and_recovery(self):
        local, cloud = Fake(), Fake()
        local.online = False
        p = EnginePoolFacade([(ResourceConfig('l', 'local', max_concurrency=9), local),
                              (ResourceConfig('c', 'cloud'), cloud)])
        await asyncio.gather(*(p.synthesize_segment(REQ) for _ in range(5)))
        self.assertEqual(local.calls, 0)
        self.assertEqual(local.probes, 1)
        self.assertEqual(cloud.probes, 1)
        local.online = True
        p.cooldown_until['l'] = 0
        await p.synthesize_segment(REQ)
        self.assertEqual(local.calls, 1)
        self.assertEqual(p.resources[0][0].max_concurrency, 1)

    async def test_weighted_sequential_fairness(self):
        a, b = Fake(), Fake()
        p = EnginePoolFacade([(ResourceConfig('a', 'cloud'), a),
                              (ResourceConfig('b', 'cloud', weight=2), b)])
        for _ in range(12):
            await p.synthesize_segment(REQ)
        self.assertEqual((a.calls, b.calls), (4, 8))

    async def test_global_capacity_local_use_and_busy_health(self):
        engines = [Fake(), Fake(), Fake()]
        gate = asyncio.Event()
        for e in engines:
            e.gate = gate
        p = EnginePoolFacade([(ResourceConfig('l', 'local'), engines[0]),
                              (ResourceConfig('a', 'cloud', max_concurrency=2), engines[1]),
                              (ResourceConfig('b', 'cloud', max_concurrency=3), engines[2])])
        tasks = [asyncio.create_task(p.synthesize_segment(REQ)) for _ in range(20)]
        for _ in range(40):
            await asyncio.sleep(0)
        self.assertEqual(list(p._inflight.values()), [1, 2, 3])
        self.assertTrue(await p.health())
        self.assertFalse(p.cooldown_until)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual([e.peak for e in engines], [1, 2, 3])
        self.assertEqual(sum(p._inflight.values()), 0)

    async def test_cancel_active_and_waiting(self):
        e = Fake()
        e.gate = asyncio.Event()
        p = EnginePoolFacade([(ResourceConfig('l', 'local'), e)])
        active = asyncio.create_task(p.synthesize_segment(REQ))
        for _ in range(20):
            await asyncio.sleep(0)
        waiter = asyncio.create_task(p.synthesize_segment(REQ))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(p._inflight['l'], 1)
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        self.assertEqual(p._inflight['l'], 0)

    async def test_failure_never_resubmits_or_trips_pool(self):
        a, b = Fake(), Fake()
        a.error = TimeoutError('提交状态未知')
        p = EnginePoolFacade([(ResourceConfig('a', 'cloud'), a), (ResourceConfig('b', 'cloud'), b)])
        with self.assertRaises(NonRetryableSynthesisError):
            await p.synthesize_segment(REQ)
        self.assertEqual((a.calls, b.calls), (1, 0))
        from app.engines.base import EngineRegistry
        reg = EngineRegistry([p])
        reg.mark_failed('pool')
        self.assertFalse(reg.in_cooldown('pool'))
        await p.synthesize_segment(REQ)
        self.assertEqual(b.calls, 1)

    async def test_art_unverified_and_snapshot(self):
        e = Fake()
        e.has_free_probe = False
        e.token = 'offline-secret'
        p = EnginePoolFacade([(ResourceConfig('art', 'art'), e)])
        await p.synthesize_segment(REQ)
        self.assertEqual(e.probes, 0)
        self.assertEqual(p.resource_snapshot()[0]['health'], 'unverified')
        self.assertNotIn('offline-secret', json.dumps(p.resource_snapshot()))

    async def test_mixed_pcm(self):
        e = Fake()
        e.audio = wav(22050, 2, 1)
        p = EnginePoolFacade([(ResourceConfig('a', 'cloud'), e)])
        converted = await p.synthesize_segment(REQ)
        result, duration = mono_runner._concat_wavs([converted, wav()], [100, 0])
        with wave.open(io.BytesIO(result)) as r:
            self.assertEqual((r.getframerate(), r.getnchannels(), r.getsampwidth()), (24000, 1, 2))
        self.assertAlmostEqual(duration, .12, places=2)
        with patch.object(podcast_runner, '_ffmpeg_bin', return_value=None):
            fallback = podcast_runner._normalize_segment(converted, 1.0)
        mono_runner._concat_wavs([fallback, wav()], [0, 0])
        self.assertEqual(fallback, converted)

    async def test_runners_do_not_retry_nonretryable(self):
        for module, runner in [(mono_runner, mono_runner.run_mono_task),
                               (podcast_runner, podcast_runner.run_podcast_task)]:
            engine = Fake()
            engine.name = 'pool'
            engine.error = NonRetryableSynthesisError('提交未知')
            async def select():
                return engine
            task = {'id': 'offline', 'voices': {'A': '/fake.wav'},
                    'lines': [{'speaker': 'A', 'text': '测试'}]}
            with patch.object(module, 'select_engine', select), patch.object(module.qs, 'persist_task'), patch.object(module, 'mark_engine_failed'):
                with self.assertRaises(NonRetryableSynthesisError):
                    await runner(task)
            self.assertEqual(engine.calls, 1)

    async def test_cancel_hook_stops_waiting_segment(self):
        """排队中的段发现用户取消后，必须在提交前退出，且不隔离资源。"""
        e = Fake()
        e.gate = asyncio.Event()
        p = EnginePoolFacade([(ResourceConfig('l', 'local'), e)])
        active = asyncio.create_task(p.synthesize_segment(REQ))
        for _ in range(20):
            await asyncio.sleep(0)
        flag = {'cancel': False}
        waiting = asyncio.create_task(p.synthesize_segment(
            SegmentRequest('排队段', VoiceRef(), should_cancel=lambda: flag['cancel'])))
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(e.calls, 1)
        flag['cancel'] = True
        with self.assertRaises(SynthesisCancelled):
            await asyncio.wait_for(waiting, timeout=3)
        self.assertEqual(e.calls, 1)              # 排队段从未提交给平台
        self.assertEqual(p._inflight['l'], 1)     # 只有活跃那一段占租约
        e.gate.set()
        await active
        self.assertEqual(p._inflight['l'], 0)

    async def test_cancel_hook_before_submit_does_not_cooldown(self):
        """已取消的段既不发请求，也不该被当成故障隔离资源。"""
        e = Fake()
        p = EnginePoolFacade([(ResourceConfig('a', 'cloud'), e)])
        with self.assertRaises(SynthesisCancelled):
            await p.synthesize_segment(SegmentRequest('x', VoiceRef(), should_cancel=lambda: True))
        self.assertEqual((e.calls, e.probes), (0, 0))
        self.assertEqual(p._inflight['a'], 0)
        self.assertFalse(p.cooldown_until)
        await p.synthesize_segment(REQ)           # 资源仍然可用
        self.assertEqual(e.calls, 1)

    async def test_normalize_pcm_identity_then_ffmpeg_then_fallback(self):
        target = wav()
        self.assertIs(engine_base.normalize_pcm(target), target)   # 已是目标格式 → 零转换
        source = wav(22050, 2, 1)
        with patch.object(engine_base, 'find_ffmpeg', return_value='/opt/homebrew/bin/ffmpeg'):
            via_ffmpeg = engine_base.normalize_pcm(source)
        with patch.object(engine_base, 'find_ffmpeg', return_value=None):
            via_stdlib = engine_base.normalize_pcm(source)
        for data in (via_ffmpeg, via_stdlib):
            with wave.open(io.BytesIO(data)) as r:
                self.assertEqual((r.getframerate(), r.getnchannels(), r.getsampwidth()), (24000, 1, 2))
        with patch.object(engine_base, 'find_ffmpeg', return_value=None), \
             patch.object(engine_base, '_parse_wav', return_value=None):
            with self.assertRaises(NonRetryableSynthesisError):
                engine_base.normalize_pcm(b'not-a-wav')

    async def test_runners_pass_cancel_hook_to_pool(self):
        seen = []
        for module, runner in [(mono_runner, mono_runner.run_mono_task),
                               (podcast_runner, podcast_runner.run_podcast_task)]:
            engine = Fake()
            engine.name = 'pool'
            original = engine.synthesize_segment
            async def record(req, _o=original):
                seen.append(req.should_cancel)
                return await _o(req)
            engine.synthesize_segment = record
            async def select():
                return engine
            task = {'id': 'hook', 'voices': {'A': '/fake.wav'},
                    'lines': [{'speaker': 'A', 'text': '测试'}]}
            with patch.object(module, 'select_engine', select), \
                 patch.object(module.qs, 'persist_task'), \
                 patch.object(module, 'mark_engine_failed'):
                await runner(task)
        self.assertTrue(seen)
        for hook in seen:
            self.assertTrue(callable(hook))
            self.assertFalse(hook())


class SpeedTests(unittest.IsolatedAsyncioTestCase):
    """语速只允许应用一次（2026-09-29 真 bug：播客链路对原生变速引擎又叠了一层 atempo）。"""

    def setUp(self):
        if not engine_base.find_ffmpeg():
            self.skipTest('需要 ffmpeg 才能验证 atempo 次数')

    async def test_pool_applies_speed_exactly_once_per_resource(self):
        native, cloud = Fake(), Fake()
        native.capabilities = EngineCapabilities('native', max_input_chars=2000, supports_speed=True)
        cloud.capabilities = EngineCapabilities('cloud', max_input_chars=2000, supports_speed=False)
        native.audio = cloud.audio = tone(400)
        source = SegmentRequest('测试', VoiceRef(), speed=2.0)

        p_native = EnginePoolFacade([(ResourceConfig('n', 'cloud'), native)])
        p_cloud = EnginePoolFacade([(ResourceConfig('c', 'cloud'), cloud)])
        out_native = await p_native.synthesize_segment(source)
        out_cloud = await p_cloud.synthesize_segment(source)

        # speed 参数照样传下去（原生支持的资源由它自己生效，池不能替它改请求）
        self.assertEqual((native.last_speed, cloud.last_speed), (2.0, 2.0))
        self.assertAlmostEqual(duration(out_native), 0.4, delta=0.05)   # 原生：不叠加
        self.assertAlmostEqual(duration(out_cloud), 0.2, delta=0.05)    # 不支持：池补一次
        # 能力声明必须说真话：混池整体不宣称原生，但两类资源都由池保证语速
        self.assertTrue(p_native.capabilities.speed_guaranteed)
        self.assertTrue(p_cloud.capabilities.speed_guaranteed)
        self.assertFalse(p_cloud.capabilities.supports_speed)
        mixed = EnginePoolFacade([(ResourceConfig('n', 'cloud'), native),
                                  (ResourceConfig('c', 'cloud'), cloud)])
        self.assertFalse(mixed.capabilities.supports_speed)
        self.assertTrue(mixed.capabilities.speed_guaranteed)

    async def test_podcast_defers_speed_to_resource_layer(self):
        """池保证语速时播客层不许再变速；只有裸引擎（无保障）才由播客层补。"""
        seen = []
        original = podcast_runner._normalize_segment

        def spy(data, speed=1.0):
            seen.append(speed)
            return original(data, speed)

        for guaranteed, expected in [(True, 1.0), (False, 2.0)]:
            seen.clear()
            engine = Fake()
            engine.name = 'pool'
            engine.capabilities = EngineCapabilities('pool', max_input_chars=2000,
                                                     supports_speed=False, speed_guaranteed=guaranteed)
            engine.audio = tone(400)
            async def select(e=engine):
                return e
            task = {'id': f'speed-{guaranteed}', 'voices': {'A': '/fake.wav'},
                    'params': {'speed': 2.0, 'speaker_speeds': {}},
                    'lines': [{'speaker': 'A', 'text': '测试'}]}
            with patch.object(podcast_runner, 'select_engine', select), \
                 patch.object(podcast_runner, '_normalize_segment', spy), \
                 patch.object(podcast_runner.qs, 'persist_task'), \
                 patch.object(podcast_runner, 'mark_engine_failed'):
                await podcast_runner.run_podcast_task(task)
            self.assertEqual(seen, [expected])          # 本层要补的语速：1.0 或 2.0
            self.assertEqual(engine.last_speed, 2.0)    # 请求里的用户语速始终是 2.0
            self.assertAlmostEqual(task['duration_sec'], 0.4 if guaranteed else 0.2, delta=0.05)

    async def test_atempo_chain_covers_out_of_range_speed(self):
        self.assertEqual(engine_base.atempo_filters(1.0), [])
        self.assertEqual(engine_base.atempo_filters(1.5), ['atempo=1.5'])
        self.assertEqual(engine_base.atempo_filters(4.0), ['atempo=2.0', 'atempo=2'])
        self.assertEqual(engine_base.atempo_filters(0.25), ['atempo=0.5', 'atempo=0.5'])
        self.assertEqual(engine_base.atempo_filters(None), [])
        data = engine_base.normalize_pcm(tone(400), 2.0)
        self.assertAlmostEqual(duration(data), 0.2, delta=0.05)


class ConfigTests(unittest.TestCase):
    def tearDown(self):
        factory.reset_registry()

    def test_empty_and_invalid(self):
        for value in ['[]', '', '{}', '[{}]']:
            with patch.dict(os.environ, {'TTS_RESOURCES': value}, clear=True):
                with self.assertRaises(ValueError):
                    factory.build_registry(force=True)
        with self.assertRaises(RuntimeError):
            EnginePoolFacade([])

    def test_legacy_pool_and_cache_isolation(self):
        with patch.dict(os.environ, {'INDEXTTS302_API_KEY': 'fake'}, clear=True):
            p = factory.build_registry(force=True).engines[0]
            self.assertIsInstance(p, EnginePoolFacade)
            self.assertEqual([s.provider for s, _ in p.resources], ['local', '302ai'])
        specs = [dict(id='a', provider='302ai', api_key_env='A'),
                 dict(id='b', provider='302ai', api_key_env='B')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(specs), 'A': 'fake-a', 'B': 'fake-b'}, clear=True):
            p = factory.build_registry(force=True).engines[0]
            a, b = [e for _, e in p.resources]
            self.assertNotEqual(a.cache_path, b.cache_path)
            self.assertIsNot(a._cache, b._cache)
            self.assertEqual(a.api_key, 'fake-a')
            self.assertEqual(b.api_key, 'fake-b')
            summary = json.dumps(factory.engine_summary())
            self.assertNotIn('fake-a', summary)
            self.assertNotIn('https://', summary)

    def test_file_config_and_default_302_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.json'
            path.write_text(json.dumps([dict(id='a', provider='302ai', api_key_env='A')]))
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': str(path), 'A': 'fake'}, clear=True):
                pool = factory.build_registry(force=True).engines[0]
                self.assertEqual(pool.resources[0][1].base_url, 'https://api.302ai.com')
                summary = factory.engine_summary()[0]
                # v3：语速只在资源侧应用一次（池对不支持原生变速的资源用 atempo 补齐）
                self.assertEqual(summary['pool_schema_version'], 3)
                self.assertTrue(summary['speed_guaranteed'])
                self.assertFalse(summary['supports_speed'])

    def test_multi_local_needs_no_declaration(self):
        """多台 local 不再需要声明 shared_voice_paths（2026-09-30 删除该字段）。

        某台服务器缺哪个音色，由 voice_sync 在提交前按需补传 —— 共享挂载只是
        「省掉每台首次上传」的优化手段，不是必填配置。
        """
        specs = [dict(id='l1', provider='local', base_url='http://a:8000'),
                 dict(id='l2', provider='local', base_url='http://b:8000')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(specs)}, clear=True):
            pool = factory.build_registry(force=True).engines[0]
            self.assertEqual([cfg.id for cfg, _ in pool.resources], ['l1', 'l2'])
            self.assertTrue(all(cfg.tier == 'local' and cfg.max_concurrency == 1
                                for cfg, _ in pool.resources))

    def test_removed_and_unknown_fields_fail_with_hint(self):
        """报错必须直接指向要改的那一行，而不是一句「资源列表无效」。"""
        legacy = [dict(id='l1', provider='local', shared_voice_paths=True)]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(legacy)}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
            self.assertIn('shared_voice_paths', str(ctx.exception))
            self.assertIn('voice_sync', str(ctx.exception))
        typo = [dict(id='c1', provider='302ai', api_key_env='A', base_ur='http://x')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(typo), 'A': 'fake'}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
            self.assertIn('base_ur', str(ctx.exception))

    def test_missing_resource_file_fails_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / 'nope.json')
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': missing}, clear=True):
                with self.assertRaises(ValueError) as ctx:
                    factory.build_registry(force=True)
                self.assertIn('不存在', str(ctx.exception))

    def test_config_file_hot_reload(self):
        """改 TTS_RESOURCES_FILE 即生效、不用重启 —— 「加减一台 tts-server」的主路径。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.json'
            one = [dict(id='c1', provider='302ai', api_key_env='A')]
            path.write_text(json.dumps(one))
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': str(path), 'A': 'fake'}, clear=True):
                first = factory.build_registry(force=True).engines[0]
                self.assertEqual([cfg.id for cfg, _ in first.resources], ['c1'])
                # 配置没动时必须复用同一个池实例（不能每个请求都重建）
                self.assertIs(factory.build_registry().engines[0], first)
                # 加一条资源 → 下一次取池自动重建
                path.write_text(json.dumps(one + [dict(id='c2', provider='302ai', api_key_env='A')]))
                second = factory.build_registry().engines[0]
                self.assertIsNot(second, first)
                self.assertEqual([cfg.id for cfg, _ in second.resources], ['c1', 'c2'])

    def test_broken_config_file_keeps_previous_pool(self):
        """写坏一个字符不该让合成链路停摆：沿用旧池并打 ERROR，改好自动恢复。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.json'
            spec = [dict(id='c1', provider='302ai', api_key_env='A')]
            path.write_text(json.dumps(spec))
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': str(path), 'A': 'fake'}, clear=True):
                good = factory.build_registry(force=True).engines[0]
                path.write_text('{ 这不是 JSON')
                self.assertIs(factory.build_registry().engines[0], good)
                path.write_text(json.dumps(spec + [dict(id='c2', provider='302ai', api_key_env='A')]))
                self.assertEqual(
                    [cfg.id for cfg, _ in factory.build_registry().engines[0].resources], ['c1', 'c2'])

    def test_tts_url_falls_back_to_pool_local(self):
        """TTS_URL 不再要求手填：未显式配置时取池里第一个 local 的地址。"""
        from app import config
        specs = [dict(id='c1', provider='302ai', api_key_env='A'),
                 dict(id='gpu', provider='local', base_url='http://gpu-host:8000')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(specs)}, clear=True):
            self.assertEqual(config._pool_first_local_url(), 'http://gpu-host:8000')
        cloud_only = [dict(id='c1', provider='302ai', api_key_env='A')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(cloud_only)}, clear=True):
            self.assertIsNone(config._pool_first_local_url())
        with patch.dict(os.environ, {'TTS_RESOURCES': '{ 坏 JSON'}, clear=True):
            self.assertIsNone(config._pool_first_local_url())

    def test_summary_reports_config_source(self):
        """排障要能一眼看出「池是按哪份配置起的」。"""
        specs = [dict(id='gpu', provider='local', base_url='http://a:8000')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(specs)}, clear=True):
            self.assertIn('TTS_RESOURCES', factory.engine_summary()[0]['config_source'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.json'
            path.write_text(json.dumps(specs))
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': str(path)}, clear=True):
                source = factory.engine_summary()[0]['config_source']
                self.assertIn('TTS_RESOURCES_FILE', source)
                self.assertIn('热加载', source)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn('旧式', factory.engine_summary()[0]['config_source'])


if __name__ == '__main__':
    unittest.main()
