#!/usr/bin/env python3
"""mono_runner 并发执行单测（无外部依赖，直接 python3 运行）。

覆盖：_flatten_segments 语义（含按能力切片）、effective_concurrency 解析、
并发保序、失败段重试一次、全部失败熔断、取消行为。
用法：cd webui-backend && python3 tests/test_mono_concurrency.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 本测试断言「未配置时默认 3」，需先断开 .env（可能设了 TTS_CONCURRENCY=5）
# 的干扰。真实环境变量优先于 .env，故在 import app.* 之前钉住即可。
os.environ["TTS_CONCURRENCY"] = "3"

from app import mono_runner  # noqa: E402
from app.engines import EngineCapabilities, effective_concurrency  # noqa: E402
from app.engines.base import SegmentRequest, VoiceRef  # noqa: E402


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected: {expected}")
        print(f"      actual:   {actual}")
    return ok


# ---------- 假引擎 ----------

class FakeEngine:
    name = "fake_api"
    # 能力声明：单次不限、并发读 TTS_CONCURRENCY（与第三方 API 引擎一致）
    capabilities = EngineCapabilities(display_name="假引擎（第三方 API 型）")

    def __init__(self, fail_first: set[str] | None = None, delay: float = 0.02):
        self.calls: list[str] = []
        self.max_inflight = 0
        self._inflight = 0
        self.fail_first = fail_first or set()
        self.delay = delay

    async def health(self) -> bool:
        return True

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            self.calls.append(req.text)
            await asyncio.sleep(self.delay)
            if req.text in self.fail_first:
                self.fail_first.discard(req.text)  # 只失败一次，重试成功
                raise RuntimeError(f"模拟瞬时失败: {req.text}")
            return f"wav:{req.text}".encode()
        finally:
            self._inflight -= 1


class FakeLocalEngine(FakeEngine):
    name = "indextts_local"
    # 自建 GPU 实例串行 —— 现在由引擎自己声明，而非上层判断引擎名
    capabilities = EngineCapabilities(display_name="假引擎（自建型）", max_concurrency=1)


def _make_task(tmpdir: Path, lines: list[dict], **kw) -> dict:
    task = {
        "id": "test_conc",
        "kind": "mono",
        "lines": lines,
        "voices": {"A": str(tmpdir / "voice.mp3")},
        "params": {},
    }
    task.update(kw)
    return task


def _make_voice_file(tmpdir: Path) -> None:
    (tmpdir / "voice.mp3").write_bytes(b"fake")


def _patch_engine(engine, tmpdir: Path, concat_sink: list | None = None):
    """把 mono_runner 的选引擎入口替换为固定假引擎。

    2026-09-29 起 mono_runner 不再自己 `build_registry()`，而是调
    `engines.selector.select_engine()`（注册表已是进程级单例，因为熔断状态
    必须跨任务存活），所以 patch 点从 `mono_runner.build_registry`
    改成 `mono_runner.select_engine` —— 名字是从 `.engines` import 进本模块的，
    patch 本模块属性即可生效。
    """
    async def _select():
        return engine

    mono_runner.select_engine = _select
    # 失败路径会调 mark_engine_failed 写真实注册表的冷却表；测试里换成空操作，
    # 避免假引擎名污染跨用例的熔断状态（熔断本身另有 tests/test_engine_layer.py 覆盖）。
    mono_runner.mark_engine_failed = lambda name: None
    mono_runner.OUTPUTS_DIR = tmpdir  # 避免写入真实 data 目录
    if concat_sink is not None:
        def _fake_concat(chunks, gaps):
            concat_sink.append([c.decode() for c in chunks])
            return b"", 0.0

        mono_runner._concat_wavs = _fake_concat


def test_flatten():
    all_ok = True
    lines = [
        {"speaker": "A", "text": "你好[pause:0.5]世界", "emotion": {"label": "happy"}, "silence_after_ms": 800},
        {"speaker": "A", "text": "  "},  # 空行跳过
        {"speaker": "A", "text": "第二行"},
    ]
    es = mono_runner._flatten_segments(lines, FakeEngine.capabilities)
    all_ok &= check("扁平化段数", len(es), 3)
    all_ok &= check("扁平化顺序", [e["text"] for e in es], ["你好", "世界", "第二行"])
    all_ok &= check("情绪逐段继承", [e["emotion"] for e in es], ["happy", "happy", None])
    all_ok &= check("停顿 gap", [e["gap_ms"] for e in es], [500, 800, 0])  # 行尾静音并入末段
    all_ok &= check("line_idx", [e["line_idx"] for e in es], [0, 0, 2])
    return all_ok


def test_split_by_capability():
    """切片依据必须是引擎声明的单次上限，而不是引擎名。

    这是 2026-09-29 的核心修复。旧写法
        `split_for_art(text) if engine_name == "indextts_art" else [text]`
    让 302.ai（上限 2000）与 SiliconFlow（上限 2048）的超长输入直接抛
    「请走 chunker」，而上层从来不切 ⇒ 同一段长文本 art 成功、302.ai 必失败。
    """
    ok = True
    # 5000 个汉字 = 10000 计费字符（chunker 口径：1 汉字 = 2）
    long_text = "字" * 5000
    lines = [{"speaker": "A", "text": long_text}]

    no_limit = EngineCapabilities(display_name="不限", max_input_chars=None)
    ok &= check("上限不限 ⇒ 不切片（自建引擎行为不变）", len(mono_runner._flatten_segments(lines, no_limit)), 1)

    limited = EngineCapabilities(display_name="上限 2000", max_input_chars=2000)
    es = mono_runner._flatten_segments(lines, limited)
    ok &= check("上限 2000 ⇒ 切成 5 片", len(es), 5)
    ok &= check("每片计费字符数不超上限", all(len(e["text"]) <= 1000 for e in es), True)
    ok &= check("切片内容无损（拼接 == 原文）", "".join(e["text"] for e in es), long_text)

    # art 的上限与旧行为一致 ⇒ 历史结果不漂移
    art = EngineCapabilities(display_name="art", max_input_chars=2048)
    ok &= check("art 上限仍是 2048 ⇒ 与旧行为一致", len(mono_runner._flatten_segments(lines, art)), 5)
    return ok


def test_concurrency_env():
    """并发度来自能力声明：显式声明优先，None 才读 TTS_CONCURRENCY。"""
    ok = True
    api = EngineCapabilities(display_name="第三方 API 型")           # max_concurrency=None
    local = EngineCapabilities(display_name="自建型", max_concurrency=1)

    ok &= check("未声明 ⇒ 默认 3", effective_concurrency(api), 3)
    ok &= check("声明 1 的引擎恒为 1（自建串行）", effective_concurrency(local), 1)

    import os
    old = os.environ.get("TTS_CONCURRENCY")
    os.environ["TTS_CONCURRENCY"] = "99"
    ok &= check("上钳 8", effective_concurrency(api), 8)
    os.environ["TTS_CONCURRENCY"] = "0"
    ok &= check("下钳 1", effective_concurrency(api), 1)
    os.environ["TTS_CONCURRENCY"] = "abc"
    ok &= check("非法值回退 3", effective_concurrency(api), 3)
    os.environ["TTS_CONCURRENCY"] = "99"
    ok &= check("显式声明不受 env 影响", effective_concurrency(local), 1)
    if old is None:
        os.environ.pop("TTS_CONCURRENCY")
    else:
        os.environ["TTS_CONCURRENCY"] = old
    return ok


def test_concurrent_order_and_speed():
    async def run():
        tmp = Path(tempfile.mkdtemp())
        _make_voice_file(tmp)
        engine = FakeEngine(delay=0.06)
        sink: list = []
        _patch_engine(engine, tmp, sink)
        lines = [{"speaker": "A", "text": f"第{i}段"} for i in range(6)]
        task = _make_task(tmp, lines, id="conc_order")
        t0 = time.monotonic()
        await mono_runner.run_mono_task(task)
        elapsed = time.monotonic() - t0
        return engine, task, elapsed, sink

    engine, task, elapsed, sink = asyncio.run(run())
    ok = check("并发保序（拼接顺序=文本顺序）", sink[0], [f"wav:第{i}段" for i in range(6)])
    ok &= check("任务成功", task["status"].value if hasattr(task["status"], "value") else task["status"], "success")
    ok &= check("调用次数", len(engine.calls), 6)  # 无失败无重试
    ok &= check("实际并发度 3", engine.max_inflight, 3)
    ok &= check(f"耗时提速（{elapsed:.2f}s < 0.28s，串行需 0.36s）", elapsed < 0.28, True)
    ok &= check("duration_sec 已统计", isinstance(task.get("duration_sec"), float), True)
    return ok


def test_retry_failed_segment():
    async def run():
        tmp = Path(tempfile.mkdtemp())
        _make_voice_file(tmp)
        engine = FakeEngine(fail_first={"第2段"}, delay=0.01)
        _patch_engine(engine, tmp)
        lines = [{"speaker": "A", "text": f"第{i}段"} for i in range(4)]
        task = _make_task(tmp, lines, id="conc_retry")
        await mono_runner.run_mono_task(task)
        return engine, task

    engine, task = asyncio.run(run())
    ok = check("失败段重试后任务成功",
               task["status"].value if hasattr(task["status"], "value") else task["status"], "success")
    ok &= check("重试只针对失败段（调用 5 次）", len(engine.calls), 5)
    ok &= check("第2段被调用两次", engine.calls.count("第2段"), 2)
    return ok


def test_all_failed_raises():
    class AlwaysFail(FakeEngine):
        async def synthesize_segment(self, req: SegmentRequest) -> bytes:
            self.calls.append(req.text)
            raise RuntimeError("平台持续 500")

    async def run():
        tmp = Path(tempfile.mkdtemp())
        _make_voice_file(tmp)
        engine = AlwaysFail(delay=0.01)
        _patch_engine(engine, tmp)
        lines = [{"speaker": "A", "text": f"第{i}段"} for i in range(3)]
        task = _make_task(tmp, lines, id="conc_fail")
        try:
            await mono_runner.run_mono_task(task)
            return engine, task, None
        except Exception as e:
            return engine, task, e

    engine, task, exc = asyncio.run(run())
    ok = check("全部失败抛异常", isinstance(exc, RuntimeError), True)
    ok &= check("异常信息保留原始错误", "平台持续 500" in str(exc), True)
    ok &= check("重试共调用 6 次（3 段 × 2）", len(engine.calls), 6)
    return ok


def test_cancel():
    async def run():
        tmp = Path(tempfile.mkdtemp())
        _make_voice_file(tmp)

        class SlowEngine(FakeEngine):
            async def synthesize_segment(self, req: SegmentRequest) -> bytes:
                task_ref["cancel_requested"] = True  # 第一段开始后立刻请求取消
                return await super().synthesize_segment(req)

        task_ref = _make_task(tmp, [{"speaker": "A", "text": f"第{i}段"} for i in range(6)], id="conc_cancel")
        engine = SlowEngine(delay=0.05)
        _patch_engine(engine, tmp)
        await mono_runner.run_mono_task(task_ref)
        return task_ref

    task = asyncio.run(run())
    status = task["status"].value if hasattr(task["status"], "value") else task["status"]
    return check("取消后状态 cancelled", status, "cancelled")


def main() -> int:
    all_ok = True
    all_ok &= test_flatten()
    all_ok &= test_concurrency_env()
    all_ok &= test_concurrent_order_and_speed()
    all_ok &= test_retry_failed_segment()
    all_ok &= test_all_failed_raises()
    all_ok &= test_cancel()
    print()
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
