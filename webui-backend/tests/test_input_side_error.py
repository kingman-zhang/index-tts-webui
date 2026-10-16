#!/usr/bin/env python3
"""输入侧错误（参考音频缺失 / 音色补齐不了）不得被当成「资源故障」。

## 为什么单独测（2026-10-16）

引擎池门面 `engines/base.py::EnginePoolFacade.synthesize_segment` 原来只有
一个 `except Exception`：它把**一切**引擎异常包成
`NonRetryableSynthesisError("资源 xxx 合成失败，提交状态可能未知")`，并顺手把
该资源冷却 `cooldown_sec`。于是一份坏音色会造成两件事：

1. **报错指错方向** —— 用户看到「资源 art 合成失败」，而 `queue_worker.py` 里
   本来就有 `except FileNotFoundError` 的专用分支（文案是「参考音频文件不存在，
   请检查任务音色配置」），只是在池路径下**永远走不到** —— 类型被包装掉了。
2. **误伤** —— 整台服务器 / 平台被隔离 120s，之后所有正常任务一起失败。

而 `mono_runner` / `podcast_runner` 末尾的 `raise first` 注释写着
「保留原始异常类型，queue_worker 据此归类 INTERRUPTED/FAILED」——
**设计意图本来就是让原始类型穿透**，是池门面把它破坏了。

## 修法与本文覆盖的判据

`engines/base.py` 新增 `EngineInputError`（`voice_sync.VoiceUnavailable` 改为继承它），
并定义 `ENGINE_INPUT_ERRORS = (EngineInputError, FileNotFoundError)`：

- 池门面对它 **不冷却、不包装、原样抛**（类型保真）；
- 它并进 `NON_RETRYABLE_ERRORS`，被 runner 的重试排除名单复用 —— **这一条是必需的**：
  池门面不再包装之后，若不排除，永久性输入错误反而会变成「可重试」
  （runner 的重试判据是「除名单外一律重试」），刷「重试 N 个失败段」且必然再失败。

用法：cd webui-backend && python3 tests/test_input_side_error.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须在 import app.* 之前钉住：隔离数据目录，并把四个存储开关按 file 钉死。
# 只设 DATA_DIR 是不够的 —— DATA_DIR 对 mongo/redis 后端完全无效，
# 本机若配了 MEMBER_STORE=mongo，裸跑本文件就会写进真实库。
_TMP = tempfile.mkdtemp(prefix="wb-input-error-test-")
os.environ["DATA_DIR"] = _TMP
os.environ.pop("TTS_ENGINE_PREFERRED", None)
for _k in ("MEMBER_STORE", "SESSION_STORE", "CONTENT_STORE", "QUEUE_STORE"):
    os.environ[_k] = "file"

# 屏蔽 .env：不让本机真实配置（尤其是 storage 后端与 TTS 资源池）参与。
_original_exists = Path.exists
with patch.object(Path, "exists", lambda p: False if p.name == ".env" else _original_exists(p)):
    from app.engines.base import (  # noqa: E402
        ENGINE_INPUT_ERRORS,
        NON_RETRYABLE_ERRORS,
        EngineCapabilities,
        EngineInputError,
        EnginePoolFacade,
        NonRetryableSynthesisError,
        ResourceConfig,
        SegmentRequest,
        VoiceRef,
    )
    from app.engines.voice_sync import VoiceUnavailable  # noqa: E402
    from app import mono_runner, podcast_runner, queue_state as qs, queue_worker  # noqa: E402

PASS = 0
FAIL = 0


def check(desc: str, actual, expected) -> bool:
    global PASS, FAIL
    ok = actual == expected
    if ok:
        PASS += 1
    else:
        FAIL += 1
    mark = "✓" if ok else "✗"
    print(f"  {mark} {desc}" + ("" if ok else f"\n      期望 {expected!r}，实际 {actual!r}"))
    return ok


def wav(rate=24000, ms=10) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        w.writeframes(b"\0" * (rate * ms // 1000 * 2))
    return out.getvalue()


class Fake:
    """假引擎：把预设异常在合成时抛出，并记录调用次数。"""

    capabilities = EngineCapabilities("fake", max_input_chars=2000)

    def __init__(self):
        self.calls = 0
        self.error: BaseException | None = None
        self.audio = wav()

    async def health(self):
        return True

    async def synthesize_segment(self, req):
        self.calls += 1
        if self.error:
            raise self.error
        return self.audio


REQ = SegmentRequest("测试", VoiceRef())


def test_pool_does_not_treat_input_error_as_resource_failure() -> bool:
    """输入侧错误：类型原样穿透 + 不冷却 + 资源仍然可用。

    三条断言缺一不可 —— 少「类型保真」则 queue_worker 拿不到专用分支；
    少「不冷却」则一份坏音色连累之后所有正常任务。
    """
    print("=== 池门面：输入侧错误不隔离资源 ===")
    ok = True
    cases = [
        ("参考音频文件不存在（内置 FileNotFoundError）",
         FileNotFoundError("参考音频不存在: /x/y.wav")),
        ("音色补齐不了（VoiceUnavailable）",
         VoiceUnavailable("参考音频 'x.wav' 在 tts-server 上不存在")),
    ]
    for label, exc in cases:
        e = Fake()
        e.error = exc
        p = EnginePoolFacade([(ResourceConfig("art", "art"), e)])

        # 异常必须原样抛出（同一个对象），不能被包成 NonRetryableSynthesisError
        raised = None
        try:
            asyncio.run(p.synthesize_segment(REQ))
        except BaseException as err:  # noqa: BLE001
            raised = err

        ok &= check(f"{label}：原样抛出同一个异常对象", raised is exc, True)
        ok &= check(f"{label}：类型未被包装成 NonRetryableSynthesisError",
                    isinstance(raised, NonRetryableSynthesisError), False)
        ok &= check(f"{label}：不冷却该资源", p.cooldown_until, {})
        ok &= check(f"{label}：租约已归还", p._inflight["art"], 0)

        # 资源没被隔离 ⇒ 修好音色后同一资源立刻能用（旧行为这里会因冷却失败）
        e.error = None
        asyncio.run(p.synthesize_segment(REQ))
        ok &= check(f"{label}：之后同一资源照样可用", (e.calls, p.cooldown_until), (2, {}))
    return ok


def test_real_resource_failure_still_wrapped_and_cooled() -> bool:
    """对照：真正的资源故障（非输入侧）行为**不变** —— 仍包装 + 仍隔离。

    这条是防「修过头」的护栏：引擎超时/500 这类不能因为本次改动被放过。
    """
    print("=== 对照：真资源故障仍包装并隔离 ===")
    ok = True
    e = Fake()
    e.error = RuntimeError("上游 502")
    p = EnginePoolFacade([(ResourceConfig("art", "art"), e)])

    raised = None
    try:
        asyncio.run(p.synthesize_segment(REQ))
    except BaseException as err:  # noqa: BLE001
        raised = err

    ok &= check("包装成 NonRetryableSynthesisError", isinstance(raised, NonRetryableSynthesisError), True)
    ok &= check("文案指向资源", "资源 art 合成失败" in str(raised), True)
    ok &= check("保留原始异常为 __cause__", isinstance(raised.__cause__, RuntimeError), True)
    ok &= check("资源被隔离", list(p.cooldown_until), ["art"])
    return ok


def test_input_errors_are_in_the_no_retry_list() -> bool:
    """输入侧错误必须落在 runner 的重试排除名单里。

    ⚠️ 这条是本次修法的**必需配套**，不是锦上添花：runner 的重试判据是
    「`isinstance(r, Exception)` 且**不在**名单里就重试」，所以池门面一旦不再包装，
    输入侧错误就会天然变成「可重试」。判据不同源走样过一次，这里锁死同源。
    """
    print("=== 重试排除名单 ===")
    ok = True
    for exc in (FileNotFoundError("x"), VoiceUnavailable("y")):
        ok &= check(f"{type(exc).__name__} 在 ENGINE_INPUT_ERRORS 里",
                    isinstance(exc, ENGINE_INPUT_ERRORS), True)
        ok &= check(f"{type(exc).__name__} 在 NON_RETRYABLE_ERRORS 里",
                    isinstance(exc, NON_RETRYABLE_ERRORS), True)
        ok &= check(f"{type(exc).__name__} 在 runner 的 _NO_RETRY_ERRORS 里",
                    isinstance(exc, mono_runner._NO_RETRY_ERRORS), True)
    # 普通异常（可重试）不能被误列入，否则会失去「失败段重试一次」的兜底能力
    ok &= check("普通 RuntimeError 不在排除名单里",
                isinstance(RuntimeError("boom"), mono_runner._NO_RETRY_ERRORS), False)
    ok &= check("两个 runner 共用同一份名单（同源）",
                podcast_runner._NO_RETRY_ERRORS is mono_runner._NO_RETRY_ERRORS, True)
    ok &= check("VoiceUnavailable 仍是 RuntimeError 子类（不破坏既有 except）",
                issubclass(VoiceUnavailable, RuntimeError), True)
    return ok


def test_queue_worker_reports_reference_audio_missing() -> bool:
    """端到端：池门面抛 FileNotFoundError ⇒ 任务卡文案点明「参考音频文件不存在」。

    这正是 2026-10-16 之前拿不到的结果：包装之后 queue_worker 只能落到
    兜底的 `except Exception`，文案是「任务执行失败」+「资源 art 合成失败」。
    """
    print("=== 端到端：任务失败文案 ===")
    ok = True

    async def _run(exc: BaseException) -> dict:
        tid = "t-input-error"
        task = {
            "id": tid, "kind": "mono",
            "lines": [{"speaker": "A", "text": "你好", "emotion": None}],
            "voices": {}, "params": {}, "silence": {},
        }
        qs.queue_tasks[tid] = task

        async def _boom(_task, lines=None):
            raise exc

        orig_pq, orig_run = queue_worker.process_queue, mono_runner.run_mono_task
        queue_worker.process_queue = lambda: asyncio.sleep(0)
        mono_runner.run_mono_task = _boom
        try:
            await queue_worker._execute_task(tid)
            return dict(task)
        finally:
            queue_worker.process_queue = orig_pq
            mono_runner.run_mono_task = orig_run
            qs.queue_tasks.pop(tid, None)

    task = asyncio.run(_run(FileNotFoundError("参考音频不存在: /x/y.wav")))
    status = task["status"].value if hasattr(task["status"], "value") else task["status"]
    ok &= check("任务判为 failed", status, "failed")
    ok &= check("message 点明「参考音频文件不存在」", task.get("message"), "参考音频文件不存在，请检查任务音色配置")
    ok &= check("error 带原始路径便于定位", "参考音频不存在: /x/y.wav" in (task.get("error") or ""), True)

    # 对照：普通异常仍走兜底分支，文案不变（防「所有失败都报参考音频问题」）
    task2 = asyncio.run(_run(RuntimeError("上游 502")))
    status2 = task2["status"].value if hasattr(task2["status"], "value") else task2["status"]
    ok &= check("对照：普通异常判为 failed", status2, "failed")
    ok &= check("对照：普通异常仍是兜底文案", task2.get("message"), "任务执行失败")
    return ok


def main() -> int:
    print(f"DATA_DIR = {_TMP}\n")
    results = [
        test_pool_does_not_treat_input_error_as_resource_failure(),
        test_real_resource_failure_still_wrapped_and_cooled(),
        test_input_errors_are_in_the_no_retry_list(),
        test_queue_worker_reports_reference_audio_missing(),
    ]
    print(f"\n===== {PASS}/{PASS + FAIL} passed =====")
    return 0 if all(results) and FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
