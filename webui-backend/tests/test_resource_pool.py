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

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='pool-test-')
_original_exists = Path.exists
with patch.object(Path, 'exists', lambda p: False if p.name == '.env' else _original_exists(p)):
    from app.engines.base import (EngineCapabilities, EnginePoolFacade, ResourceConfig,
                                  SegmentRequest, SynthesisCancelled, VoiceRef,
                                  NonRetryableSynthesisError)
    from app.engines import base as engine_base
    from app.engines import factory
    from app.engines.indextts_local import IndexttsLocalEngine
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

    async def test_snapshot_explains_why_local_was_skipped(self):
        """「所有合成都走 art」时，/api/version 必须说清 local 为什么被跳过。

        「连不上」与「连上了、但模型没加载好」要查的地方完全不同（网络/端口/隧道
        vs GPU 机器上的模型加载日志），而健康位只有一个 bool —— 2026-09-30 用户就是
        卡在这里：全部走了云端，端点却只回一个 `unavailable`。
        """
        local, cloud = Fake(), Fake()
        local.online = False
        local.last_health_note = '模型未加载（status=no_model）'
        p = EnginePoolFacade([(ResourceConfig('gpu', 'local'), local),
                              (ResourceConfig('art', 'cloud'), cloud)])
        await p.synthesize_segment(REQ)                       # local 被跳过 ⇒ 全落云端
        self.assertEqual((local.calls, cloud.calls), (0, 1))
        snap = {r['id']: r for r in p.resource_snapshot()}
        self.assertEqual(snap['gpu']['health'], 'unavailable')
        self.assertIn('no_model', snap['gpu']['health_note'])
        self.assertIsNone(snap['art']['health_note'])         # 没失败就不该有原因

        local.online = True
        local.last_health_note = None
        p.cooldown_until['gpu'] = 0
        await p.synthesize_segment(REQ)
        self.assertEqual(local.calls, 1)
        # 恢复后必须清掉，否则会长期挂着一条已经过期的解释
        self.assertIsNone({r['id']: r for r in p.resource_snapshot()}['gpu']['health_note'])

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
            fallback = podcast_runner._apply_speed(converted, 1.0)
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
        original = podcast_runner._apply_speed

        def spy(data, speed=1.0):
            seen.append(speed)
            return original(data, speed)

        # 语速由池保证时，本层一次都不该动音频；只有裸引擎（无保障）才补一次。
        for guaranteed, expected in [(True, []), (False, [2.0])]:
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
                 patch.object(podcast_runner, '_apply_speed', spy), \
                 patch.object(podcast_runner.qs, 'persist_task'), \
                 patch.object(podcast_runner, 'mark_engine_failed'):
                await podcast_runner.run_podcast_task(task)
            self.assertEqual(seen, expected)            # 本层要补的语速：无，或 2.0
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
            path = Path(directory) / 'resources.yaml'
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
            missing = str(Path(directory) / 'nope.yaml')
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': missing}, clear=True):
                with self.assertRaises(ValueError) as ctx:
                    factory.build_registry(force=True)
                msg = str(ctx.exception)
                self.assertIn('不存在', msg)
                # 迁移期最容易犯的错就是「改了 .env 忘了改名」（或反之）—— 报错里直接把路指出来
                self.assertIn('.yaml', msg)

    def test_config_file_hot_reload(self):
        """改 TTS_RESOURCES_FILE 即生效、不用重启 —— 「加减一台 tts-server」的主路径。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.yaml'
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
            path = Path(directory) / 'resources.yaml'
            spec = [dict(id='c1', provider='302ai', api_key_env='A')]
            path.write_text(json.dumps(spec))
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': str(path), 'A': 'fake'}, clear=True):
                good = factory.build_registry(force=True).engines[0]
                path.write_text('{ 这不是 YAML')
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
            path = Path(directory) / 'resources.yaml'
            path.write_text(json.dumps(specs))
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': str(path)}, clear=True):
                source = factory.engine_summary()[0]['config_source']
                self.assertIn('TTS_RESOURCES_FILE', source)
                self.assertIn('热加载', source)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn('旧式', factory.engine_summary()[0]['config_source'])

    # ─── 配置是 YAML（2026-10-08 由 JSON 切）：注释成了特性，报错仍要点名真凶 ───

    def test_hash_comments_are_allowed_and_can_disable_a_resource(self):
        """`#` 注释是 YAML 的**特性**，也是这次从 JSON 切过来的唯一理由。

        用户原话：「我切换配置时，使用注释可能更方便一些」—— 想停用某台服务器时
        整条注释掉，比「删掉、用的时候再凭记忆写回来」安全得多。
        """
        text = ('# 备用机先停用\n'
                '- {id: l1, provider: local, base_url: "http://a:8000"}\n'
                '# - {id: l2, provider: local, base_url: "http://b:8000"}\n')
        with patch.dict(os.environ, {'TTS_RESOURCES': text}, clear=True):
            pool = factory.build_registry(force=True).engines[0]
            self.assertEqual([cfg.id for cfg, _ in pool.resources], ['l1'])

    def test_c_style_comment_is_rejected_with_line_hint(self):
        """`//` 与 `/* */` 都不是 YAML 注释符 —— 报错必须**点名到行**。

        `yaml.safe_load` 原生只说 `expected <block end>, but found '?'`，对
        「这里写了 C 风格注释」毫无指向性。
        """
        ok = '- {id: l1, provider: local, base_url: "http://a:8000"}'
        for text in (f'{ok}\n// 这一行是 C 风格注释\n', f'{ok}\n/* 停用一条 */\n'):
            with patch.dict(os.environ, {'TTS_RESOURCES': text}, clear=True):
                with self.assertRaises(ValueError) as ctx:
                    factory.build_registry(force=True)
            msg = str(ctx.exception)
            self.assertIn('第 2 行', msg)
            self.assertIn('C 风格注释', msg)
            self.assertIn('`#`', msg)

    def test_tab_indent_is_rejected_with_line_hint(self):
        """YAML 只允许**空格**缩进；原生报错 `found character '\\t'...` 看不出是缩进问题。"""
        text = '- id: l1\n\tprovider: local\n'
        with patch.dict(os.environ, {'TTS_RESOURCES': text}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
        msg = str(ctx.exception)
        self.assertIn('第 2 行', msg)
        self.assertIn('Tab', msg)
        self.assertIn('空格', msg)

    def test_all_commented_out_fails_loudly(self):
        """全部注释掉 = 没有引擎可用 ⇒ 直接失败，**不给一个空池**。

        空池的故障会推迟到第一次合成才爆（而且是「没有可用资源」这种绕的报错），
        不如在配置解析时就拦住。
        """
        with patch.dict(os.environ, {'TTS_RESOURCES': '# - {id: l1, provider: local}\n'}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
        self.assertIn('空的', str(ctx.exception))

    def test_top_level_mapping_gets_a_hint(self):
        """`resources:` 包装键是很自然的误写 —— 报错要点名那个键、并说清怎么改。"""
        with patch.dict(os.environ,
                        {'TTS_RESOURCES': 'resources:\n  - {id: l1, provider: local}\n'}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
        msg = str(ctx.exception)
        self.assertIn('resources', msg)
        self.assertIn('列表', msg)
        self.assertIn('顶格', msg)

    def test_flow_style_trailing_comma_is_accepted(self):
        """YAML 的**流式**写法允许尾随逗号（JSON 不允许）—— 记录规范行为，别当成 bug 去修。

        切 YAML 之后 `[...,]` 不再报错，这正是「规范允许的就不能算我们的放宽」
        的实例；但块式（`- {...},`）仍然不合法，见下一条。
        """
        text = '[{"id":"l1","provider":"local","base_url":"http://a:8000"},]'
        with patch.dict(os.environ, {'TTS_RESOURCES': text}, clear=True):
            pool = factory.build_registry(force=True).engines[0]
            self.assertEqual([cfg.id for cfg, _ in pool.resources], ['l1'])

    def test_block_style_trailing_comma_is_still_rejected(self):
        """块式列表项后面的逗号是 YAML 语法错 —— 别把上面的结论推广到这里。"""
        text = '- {id: l1, provider: local, base_url: "http://a:8000"},\n'
        with patch.dict(os.environ, {'TTS_RESOURCES': text}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
        self.assertIn('不是合法 YAML', str(ctx.exception))

    def test_extension_must_be_yaml(self):
        """`.json` 要被**拒绝**，且报错要说清「改名即可、内容不用动」。

        为什么不干脆让 YAML 解析器照读 `.json` —— 因为 YAML 是 JSON 的超集，
        那样一个叫 `.json` 的文件里写 `#` 注释也会被接受；可它已经不是 JSON 了，
        VS Code 会标红、`jq` 会报错，等于又造出一种「只有本项目认得」的方言
        （2026-09-30 明确否决过这条路）。改名是零成本的，所以要求改名。
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tts-resources.json'
            path.write_text('- {id: l1, provider: local, base_url: "http://a:8000"}')
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': str(path)}, clear=True):
                with self.assertRaises(ValueError) as ctx:
                    factory.build_registry(force=True)
        msg = str(ctx.exception)
        self.assertIn('只认 YAML', msg)
        self.assertIn('.yaml', msg)
        self.assertIn('不用动', msg)

    def test_bom_is_tolerated(self):
        """BOM 是 YAML 规范允许的开头（JSON 不允许）—— 切换后这条限制自然消失。

        2026-09-30 曾专门为「文件带 BOM」写了一条诊断；现在它不再是错误，
        所以要有一条测试**锁住「不再报错」**，否则以后有人会把合法配置改回失败。
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resources.yaml'
            body = '- {id: l1, provider: local, base_url: "http://a:8000"}\n'
            path.write_bytes(b'\xef\xbb\xbf' + body.encode('utf-8'))
            with patch.dict(os.environ, {'TTS_RESOURCES_FILE': str(path)}, clear=True):
                pool = factory.build_registry(force=True).engines[0]
                self.assertEqual([cfg.id for cfg, _ in pool.resources], ['l1'])

    def test_url_in_config_is_not_mistaken_for_a_comment(self):
        """⚠️ 诊断不能把 URL 里的 `//` 当注释 —— 合法配置必须照常通过。

        （这正是当初想「用正则剥注释」会踩的坑：会静默截断 base_url。）
        """
        specs = [dict(id='l1', provider='local', base_url='https://h.example.com:3391')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(specs)}, clear=True):
            pool = factory.build_registry(force=True).engines[0]
            self.assertEqual(pool.resources[0][1].tts_url, 'https://h.example.com:3391')

    def test_credential_hint_is_actionable(self):
        """填了密钥本身时，报错要说清「这里要填变量名」，并给出该 provider 的惯用名。

        原措辞「缺少 api_key_env（<填进去的密钥>）指定的凭据」会被读成
        「这个密钥不对」，而真正的问题是**位置错了**。
        """
        secret = 'j7cy' + 'X' * 39          # 形态：长随机串（非全大写）
        specs = [dict(id='art', provider='art', api_key_env=secret)]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(specs)}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
        msg = str(ctx.exception)
        self.assertIn('密钥本身', msg)
        self.assertIn('AUTODL_API_TOKEN', msg)
        self.assertNotIn(secret, msg)       # 绝不把密钥回显进日志

    def test_all_missing_credentials_reported_at_once(self):
        """配了三家云只报第一家 = 让人来回重启三次。"""
        specs = [dict(id='art', provider='art', api_key_env='AUTODL_API_TOKEN'),
                 dict(id='ai302', provider='302ai', api_key_env='INDEXTTS302_API_KEY'),
                 dict(id='sf', provider='siliconflow', api_key_env='SILICONFLOW_API_KEY')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(specs)}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
        msg = str(ctx.exception)
        for name in ('art', 'ai302', 'sf'):
            self.assertIn(name, msg)

    def test_hint_points_at_existing_conventional_variable(self):
        """变量名写错但惯用名就在环境里时，直接点名 —— 改一个字就能跑。"""
        specs = [dict(id='art', provider='art', api_key_env='AUTODL_TOKEN')]
        with patch.dict(os.environ, {'TTS_RESOURCES': json.dumps(specs),
                                     'AUTODL_API_TOKEN': 'fake'}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                factory.build_registry(force=True)
        msg = str(ctx.exception)
        self.assertIn('AUTODL_TOKEN', msg)
        self.assertIn('已存在 AUTODL_API_TOKEN', msg)

    def test_example_config_stays_parsable(self):
        """仓库里的示例配置必须始终可解析 —— 文档一腐烂，用户照抄就报错。

        用标准 `yaml.safe_load`（不是项目里的任何解析入口）：这条同时守住
        「示例文件就叫 .yaml」与「示例里的字段名是 `_make_spec` 认得的」。
        """
        path = Path(__file__).resolve().parents[1] / 'tts-resources.example.yaml'
        items = yaml.safe_load(path.read_text(encoding='utf-8'))
        self.assertEqual([i['id'] for i in items],
                         ['gpu-a', 'gpu-b', 'art', 'ai302', 'siliconflow'])
        self.assertTrue(all(i.get('provider') for i in items))


class LocalHealthNoteTests(unittest.IsolatedAsyncioTestCase):
    """探活失败的**原因**必须能分辨：连不上 vs 连上了但模型没加载（2026-09-30）。

    两者的排查方向不同（网络/端口 vs GPU 机器上的模型加载日志），健康位只有一个
    bool 表达不了 —— 所以原因单独带出来，透出到 `/api/version` 与探活日志。
    """

    @staticmethod
    def _engine(handler):
        import httpx
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return IndexttsLocalEngine('http://gpu:8000', client), client

    async def test_reason_categories(self):
        import httpx
        cases = [
            (lambda req: httpx.Response(200, json={'status': 'ok', 'model_loaded': True}), None),
            (lambda req: httpx.Response(200, json={'status': 'no_model', 'model_loaded': False}),
             '模型未加载'),
            (lambda req: httpx.Response(503), 'HTTP 503'),
            (lambda req: httpx.Response(200, text='<html>别的服务</html>'), '不是合法 JSON'),
        ]
        for handler, expect in cases:
            engine, client = self._engine(handler)
            try:
                ok = await engine.health()
            finally:
                await client.aclose()
            self.assertEqual(ok, expect is None)
            if expect is None:
                self.assertIsNone(engine.last_health_note)
            else:
                self.assertIn(expect, engine.last_health_note)

    async def test_connection_error_points_at_network(self):
        """连不上时原因里要出现异常类型 —— 指向网络/端口/隧道，而不是模型。"""
        import httpx

        def boom(request):
            raise httpx.ConnectError('Connection refused')

        engine, client = self._engine(boom)
        try:
            self.assertFalse(await engine.health())
        finally:
            await client.aclose()
        self.assertIn('连接失败', engine.last_health_note)
        self.assertIn('ConnectError', engine.last_health_note)


if __name__ == '__main__':
    unittest.main()
