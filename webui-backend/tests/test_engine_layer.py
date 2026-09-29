#!/usr/bin/env python3
"""引擎层的单测：能力声明、注册表单例、熔断、选择、未知 kind 的处理。

背景（2026-09-29）：此前「走哪个引擎」这件事完全不可见，能力差异靠
`engine_name == "indextts_art"` 这类硬编码泄漏到上层，故障切换是死代码，
art 的探活恒为真（余额耗尽照样当选）。这一层改动没有任何测试覆盖过，
所以补上 —— 它正是那些「改了不生效 / 悄悄降级到错误引擎」事故的所在地。

用法：cd webui-backend && python3 tests/test_engine_layer.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须在 import app.* 之前钉住：隔离数据目录，避免读到真实 data/。
_TMP = tempfile.mkdtemp(prefix="wb-engine-test-")
os.environ["DATA_DIR"] = _TMP
os.environ.pop("TTS_ENGINE_PREFERRED", None)

from app.engines import (  # noqa: E402
    EngineCapabilities,
    EngineRegistry,
    Indextts302aiEngine,
    IndexttsArtEngine,
    IndexttsLocalEngine,
    IndexttsSiliconflowEngine,
    build_registry,
    effective_concurrency,
    engine_summary,
    reset_registry,
)
from app.engines.chunker import ART_MAX_CHARS  # noqa: E402

PASS = 0
FAIL = 0


def check(desc: str, actual, expected) -> bool:
    global PASS, FAIL
    ok = actual == expected
    PASS += 1 if ok else 0
    FAIL += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    if not ok:
        print(f"        expected: {expected!r}")
        print(f"        actual:   {actual!r}")
    return ok


# ---------- 假引擎（不发网络请求） ----------

class StubEngine:
    def __init__(self, name: str, healthy: bool = True, caps: EngineCapabilities | None = None):
        self.name = name
        self.capabilities = caps or EngineCapabilities(display_name=name)
        self._healthy = healthy
        self.health_calls = 0

    async def health(self) -> bool:
        self.health_calls += 1
        return self._healthy

    async def synthesize_segment(self, req) -> bytes:
        return b"wav"


def test_capabilities_declared():
    """每个引擎都必须声明能力，且值与实现一致 —— 声明错了上层就会被误导。"""
    print("=== 能力声明 ===")
    ok = True

    local = IndexttsLocalEngine("http://localhost:8000", client=None).capabilities
    # 自建：不限输入（交给模型侧分段）、GPU 串行、原生语速、8 维情绪
    ok &= check("自建 max_input_chars 不限", local.max_input_chars, None)
    ok &= check("自建 max_concurrency = 1（GPU 串行）", local.max_concurrency, 1)
    ok &= check("自建 supports_speed", local.supports_speed, True)

    ai302 = Indextts302aiEngine(api_key="fake").capabilities
    ok &= check("302.ai 单次上限 2000", ai302.max_input_chars, 2000)
    # 平台请求体没有 speed 参数，传了也无效 —— 必须声明 False，否则上层以为变速生效
    ok &= check("302.ai 不支持语速", ai302.supports_speed, False)

    art = IndexttsArtEngine(token="fake").capabilities
    ok &= check("art 单次上限 = chunker 常量", art.max_input_chars, ART_MAX_CHARS)
    ok &= check("art 不支持语速（请求体无 speed 字段）", art.supports_speed, False)

    sf = IndexttsSiliconflowEngine(api_key="fake").capabilities
    ok &= check("SiliconFlow 单次上限 2048", sf.max_input_chars, 2048)
    ok &= check("SiliconFlow 支持语速（0.25-4.0）", sf.supports_speed, True)
    return ok


def test_capabilities_drive_concurrency():
    """并发度只看能力声明。自建 1、第三方读 env —— 这正是原 _concurrency 的行为，
    但它当时是靠 `engine_name == "indextts_local"` 判断的。"""
    print("=== 能力驱动并发 ===")
    ok = True
    old = os.environ.get("TTS_CONCURRENCY")
    os.environ["TTS_CONCURRENCY"] = "5"
    try:
        ok &= check("自建声明的 1 压过 env", effective_concurrency(
            EngineCapabilities(display_name="x", max_concurrency=1)), 1)
        ok &= check("未声明则读 env", effective_concurrency(
            EngineCapabilities(display_name="x")), 5)
    finally:
        if old is None:
            os.environ.pop("TTS_CONCURRENCY", None)
        else:
            os.environ["TTS_CONCURRENCY"] = old
    return ok


def test_resolve_skips_unhealthy():
    """选择阶段的降级：探活失败的引擎被跳过（这是唯一的降级点）。"""
    print("=== 选择阶段降级 ===")
    ok = True
    dead = StubEngine("dead", healthy=False)
    alive = StubEngine("alive", healthy=True)

    reg = EngineRegistry()
    reg.register(dead)
    reg.register(alive)
    picked = asyncio.run(reg.resolve())
    ok &= check("跳过不健康的引擎", picked.name, "alive")
    ok &= check("按顺序只探活到可用为止", dead.health_calls, 1)

    only_dead = EngineRegistry()
    only_dead.register(StubEngine("d1", healthy=False))
    try:
        asyncio.run(only_dead.resolve())
        ok &= check("全部不可用应抛错", "no-raise", "RuntimeError")
    except RuntimeError:
        ok &= check("全部不可用抛 RuntimeError", "RuntimeError", "RuntimeError")
    return ok


def test_cooldown_skip_and_half_open():
    """熔断：合成阶段暴露的引擎级故障要能反馈到选择阶段。

    这就是 autodl.art 的形态 —— health() 只能验证 Token 存在，余额耗尽
    （HTTP 403）只在真正合成时才暴露，于是每个新任务都先撞一次墙、全段失败。
    """
    print("=== 熔断 ===")
    ok = True
    a = StubEngine("eng_a")
    b = StubEngine("eng_b")
    reg = EngineRegistry()
    reg.register(a)
    reg.register(b)

    ok &= check("初始不在冷却", reg.in_cooldown("eng_a"), False)
    reg.mark_failed("eng_a")
    ok &= check("mark_failed 后进入冷却", reg.in_cooldown("eng_a"), True)
    picked = asyncio.run(reg.resolve())
    ok &= check("冷却中的引擎被跳过", picked.name, "eng_b")

    # 半开：全部冷却时不能拒绝接单，否则一个引擎故障就让整个服务停摆
    reg.mark_failed("eng_b")
    picked2 = asyncio.run(reg.resolve())
    ok &= check("全部冷却时忽略冷却重试优先级最高的", picked2.name, "eng_a")

    # 冷却到期自动解除（把到期时刻调到过去）
    reg.cooldown_until["eng_a"] = 0.0
    ok &= check("冷却到期后自动解除", reg.in_cooldown("eng_a"), False)
    return ok


def test_registry_is_singleton():
    """注册表必须是进程级单例：熔断状态要跨任务存活，探活/音色缓存不能每任务作废。"""
    print("=== 注册表单例 ===")
    ok = True
    reset_registry()
    r1 = build_registry()
    r2 = build_registry()
    ok &= check("两次取到同一实例", r1 is r2, True)

    r1.engines[0].cooldown_until["indextts_local"] = float("inf")
    ok &= check("资源熔断状态跨调用可见", build_registry().engines[0].in_cooldown("indextts_local"), True)

    reset_registry()
    r3 = build_registry()
    ok &= check("reset 后是新实例", r3 is r1, False)
    ok &= check("reset 后冷却状态清空", r3.in_cooldown("indextts_art"), False)
    return ok


def test_engine_summary_shape():
    """摘要必须包含「为什么走了这个引擎」所需的字段。"""
    print("=== 引擎摘要 ===")
    ok = True
    reset_registry()
    summary = engine_summary()
    ok &= check("至少注册了引擎", len(summary) >= 1, True)
    fields = {"name", "display_name", "max_input_chars", "max_concurrency",
              "supports_speed", "supports_emotion", "in_cooldown"}
    ok &= check("字段齐全", fields <= set(summary[0].keys()), True)
    ok &= check("摘要实例与注册表一致", summary[0]["name"], build_registry().engines[0].name)
    reset_registry()
    return ok


def test_podcast_flatten_uses_capabilities():
    """播客路径的切片同样走能力声明 —— 此前是同一个引擎名硬编码。

    `_flatten_podcast_segments` 的签名已由 (lines, engine_name, silence) 改为
    (lines, capabilities, silence)。这条路径此前没有任何测试覆盖。
    """
    ok = True
    from app import podcast_runner

    long_text = "字" * 5000  # 计费口径 10000（1 汉字 = 2）
    lines = [{"speaker": "A", "text": long_text}]

    es0 = podcast_runner._flatten_podcast_segments(
        lines, EngineCapabilities(display_name="不限"), {})
    ok &= check("播客：上限不限 ⇒ 不切片", len(es0), 1)

    es = podcast_runner._flatten_podcast_segments(
        lines, EngineCapabilities(display_name="上限 2000", max_input_chars=2000), {})
    ok &= check("播客：上限 2000 ⇒ 切成 5 片", len(es), 5)
    ok &= check("播客：切片内容无损", "".join(e["text"] for e in es), long_text)
    ok &= check("播客：speaker 已带上（多音色路由需要）", es[0].get("speaker"), "A")
    return ok


def test_unknown_kind_rejected():
    """未知 kind 必须明确报错。

    2026-09-29 之前，kind 不是 mono/podcast 时会走一条兜底分支：
    POST {TTS_URL}/api/podcast，把任务交给本地 tts-server —— 那是「云端引擎 /
    本地服务器」两套执行路径并存的根源，云端部署下它必然失败，且绕过了能力
    声明、探活与熔断。已删除，改为明确报错。
    """
    print("=== 未知 kind ===")
    ok = True
    from app import queue_worker, queue_state as qs

    async def _run(kind):
        tid = "t-unknown"
        task = {
            "id": tid, "kind": kind,
            "lines": [{"speaker": "A", "text": "你好", "emotion": None}],
            "voices": {}, "params": {}, "silence": {},
        }
        qs.queue_tasks[tid] = task
        # 收尾的 finally 会 create_task(process_queue())，测试里换成空操作，
        # 避免事件循环关闭时的 pending task 噪音
        orig_pq = queue_worker.process_queue
        queue_worker.process_queue = lambda: asyncio.sleep(0)
        try:
            await queue_worker._execute_task(tid)
            return task
        finally:
            queue_worker.process_queue = orig_pq
            qs.queue_tasks.pop(tid, None)

    task = asyncio.run(_run("legacy"))
    status = task["status"].value if hasattr(task["status"], "value") else task["status"]
    ok &= check("未知 kind 的任务判为 failed", status, "failed")
    ok &= check("错误信息点明 kind", "legacy" in (task.get("error") or ""), True)
    return ok


def main() -> int:
    print(f"DATA_DIR = {_TMP}\n")
    results = [
        test_capabilities_declared(),
        test_capabilities_drive_concurrency(),
        test_resolve_skips_unhealthy(),
        test_cooldown_skip_and_half_open(),
        test_registry_is_singleton(),
        test_engine_summary_shape(),
        test_podcast_flatten_uses_capabilities(),
        test_unknown_kind_rejected(),
    ]
    print(f"\n===== {PASS}/{PASS + FAIL} passed =====")
    return 0 if all(results) and FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
