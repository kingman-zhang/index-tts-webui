#!/usr/bin/env python3
"""mono 流式落盘 + 断点续传的端到端单测（无外部依赖，直接 python3 运行）。

覆盖：
  - 流式合流的帧数 / 采样率 / 时长 / 段后静音（gap）正确性；
  - 段缓存缺失时合流报错，且**不留半成品**；
  - 每段都真的落盘（这是「音频不再攒在内存里」的直接证据）；
  - 同 task_id 重跑时只补缺失段（断点续传，不重复调用平台）；
  - 合成指纹变化（换引擎 ⇒ 段划分变）时整份重来，绝不复用旧段；
  - 成功后段缓存被清理。
用法：cd webui-backend && python3 tests/test_mono_stream.py
"""

import asyncio
import io
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["TTS_CONCURRENCY"] = "3"

from app import mono_runner, seg_cache  # noqa: E402
from app.engines import EngineCapabilities  # noqa: E402
from app.engines.base import SegmentRequest  # noqa: E402

FRAMERATE = 8000


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected: {expected}")
        print(f"      actual:   {actual}")
    return ok


# ---------- 工具 ----------

def _wav(frames: int, value: int = 1, framerate: int = FRAMERATE) -> bytes:
    """单声道 16bit、指定帧数、采样值恒为 value 的合法 wav。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(struct.pack("<h", value) * frames)
    return buf.getvalue()


def _frame_count(path: Path) -> int:
    with wave.open(str(path), "rb") as r:
        return r.getnframes()


def _wav_format(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as r:
        return r.getframerate(), r.getnchannels(), r.getsampwidth()


class WavEngine:
    """返回合法 wav 的假引擎。frames_for(text) 决定每段帧数。"""

    def __init__(self, name="fake_api", max_input_chars=None, frames_for=None, fail=()):
        self.name = name
        self.capabilities = EngineCapabilities(
            display_name="假引擎（合法 wav）", max_input_chars=max_input_chars
        )
        self.calls: list[str] = []
        self.frames_for = frames_for or (lambda text: 100)
        self.fail = set(fail)

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        self.calls.append(req.text)
        await asyncio.sleep(0)
        if req.text in self.fail:
            raise RuntimeError(f"模拟失败: {req.text}")
        return _wav(self.frames_for(req.text))


def _install(engine, tmp: Path) -> None:
    async def _select():
        return engine

    mono_runner.select_engine = _select
    mono_runner.mark_engine_failed = lambda name: None
    mono_runner.OUTPUTS_DIR = tmp


def _workdir() -> Path:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "voice.mp3").write_bytes(b"fake")
    return tmp


def _task(tmp: Path, texts, task_id: str) -> dict:
    return {
        "id": task_id,
        "kind": "mono",
        "lines": [{"speaker": "A", "text": t} for t in texts],
        "voices": {"A": str(tmp / "voice.mp3")},
        "params": {},
    }


# ---------- 用例 ----------

def test_assemble_frames_and_gaps():
    """合流后的帧数 = 各段帧数之和 + 各段后静音帧数；采样率保持。"""
    ok = True
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "seg_cache"
    tid = "q_asm"
    seg_cache.prepare(root, tid, "fp", 3)
    seg_cache.write_segment(root, tid, 0, _wav(100))
    seg_cache.write_segment(root, tid, 1, _wav(200))
    seg_cache.write_segment(root, tid, 2, _wav(300))

    gaps = [100, 0, 50]  # ms
    out = tmp / "out.wav"
    duration = mono_runner._assemble_streaming_sync(root, tid, gaps, out)

    expected = 600 + int(FRAMERATE * 0.1) + int(FRAMERATE * 0.05)
    ok &= check("帧数 = 各段 + 段后静音", _frame_count(out), expected)
    ok &= check("采样率保持不变", _wav_format(out), (FRAMERATE, 1, 2))
    ok &= check("返回时长 = 帧数 / 采样率", abs(duration - expected / FRAMERATE) < 1e-9, True)
    ok &= check("分段文件用完仍留在磁盘（由任务收尾决定去留）",
                seg_cache.has_segment(root, tid, 2), True)
    return ok


def test_assemble_missing_segment():
    """缺段必须报错，且不能留下半成品 wav 冒充成品。"""
    ok = True
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "seg_cache"
    tid = "q_missing"
    seg_cache.prepare(root, tid, "fp", 2)
    seg_cache.write_segment(root, tid, 0, _wav(10))  # 故意不写第 1 段
    out = tmp / "missing.wav"
    msg = ""
    try:
        mono_runner._assemble_streaming_sync(root, tid, [0, 0], out)
    except ValueError as e:
        msg = str(e)
    ok &= check("缺段 ⇒ ValueError 且指出是哪一段", "第 2/2 段" in msg, True)
    ok &= check("失败不留半成品", out.exists(), False)
    return ok


def test_every_segment_persisted():
    """每段都必须落过盘 —— 这就是「音频不再攒在内存里」的直接证据。"""
    written: list[int] = []
    original = seg_cache.write_segment

    def spy(root, tid, index, data):
        written.append(index)
        return original(root, tid, index, data)

    async def run():
        tmp = _workdir()
        _install(WavEngine(), tmp)
        task = _task(tmp, [f"第{i}段" for i in range(5)], "t_persist")
        await mono_runner.run_mono_task(task)
        return task

    seg_cache.write_segment = spy
    try:
        task = asyncio.run(run())
    finally:
        seg_cache.write_segment = original

    ok = check("五段全部落过盘", sorted(written), [0, 1, 2, 3, 4])
    ok &= check("任务成功",
                task["status"].value if hasattr(task["status"], "value") else task["status"],
                "success")
    ok &= check("成品音频已生成", Path(task["output_path"]).is_file(), True)
    ok &= check("成功后段缓存被清理",
                not (mono_runner._seg_cache_root() / "t_persist").exists(), True)
    return ok


def test_resume_only_missing_segments():
    """第一遍部分段失败 ⇒ 第二遍同 task_id 只补缺失段（断点续传的核心收益）。"""
    async def first():
        tmp = _workdir()
        engine = WavEngine(fail={"第2段"})  # 第 2 段（含重试）始终失败
        _install(engine, tmp)
        task = _task(tmp, [f"第{i}段" for i in range(4)], "t_resume")
        err = None
        try:
            await mono_runner.run_mono_task(task)
        except Exception as e:  # noqa: BLE001
            err = e
        return tmp, engine, err

    tmp, eng1, err = asyncio.run(first())
    ok = check("第一遍：第 2 段失败 ⇒ 任务抛错", isinstance(err, RuntimeError), True)
    ok &= check("第一遍：成功的 3 段各调用 1 次，第 2 段（含重试）2 次",
                sorted(eng1.calls), sorted(["第0段", "第1段", "第2段", "第2段", "第3段"]))
    ok &= check("第一遍失败后缓存**保留**（供续传）",
                (mono_runner._seg_cache_root() / "t_resume").exists(), True)

    async def second():
        engine = WavEngine()  # 引擎已恢复
        _install(engine, tmp)
        task = _task(tmp, [f"第{i}段" for i in range(4)], "t_resume")
        await mono_runner.run_mono_task(task)
        return engine, task

    eng2, task2 = asyncio.run(second())
    ok &= check("第二遍：只补第 2 段（其余命中缓存，不重复付费）", eng2.calls, ["第2段"])
    ok &= check("第二遍：任务成功",
                task2["status"].value if hasattr(task2["status"], "value") else task2["status"],
                "success")
    ok &= check("第二遍：成品已生成", Path(task2["output_path"]).is_file(), True)
    ok &= check("第二遍：成功后缓存被清理",
                not (mono_runner._seg_cache_root() / "t_resume").exists(), True)
    return ok


def test_fingerprint_change_redoes_all():
    """换引擎（单次上限变了 ⇒ 段划分变了）必须整份重来：复用旧段会拼出错位音频。"""
    async def first():
        tmp = _workdir()
        _install(WavEngine(max_input_chars=None, fail={"第2段"}), tmp)
        task = _task(tmp, [f"第{i}段" for i in range(4)], "t_fp")
        try:
            await mono_runner.run_mono_task(task)
        except Exception:  # noqa: BLE001
            pass
        return tmp

    tmp = asyncio.run(first())

    async def second():
        # 同一个 task_id、同样的文本，但引擎单次上限不同 ⇒ 指纹失配
        engine = WavEngine(max_input_chars=100)
        _install(engine, tmp)
        task = _task(tmp, [f"第{i}段" for i in range(4)], "t_fp")
        await mono_runner.run_mono_task(task)
        return engine

    eng = asyncio.run(second())
    ok = check("指纹失配 ⇒ 4 段全部重新合成（含上一遍成功的那 3 段）",
               sorted(eng.calls), sorted(["第0段", "第1段", "第2段", "第3段"]))
    return ok


def main() -> int:
    all_ok = True
    all_ok &= test_assemble_frames_and_gaps()
    all_ok &= test_assemble_missing_segment()
    all_ok &= test_every_segment_persisted()
    all_ok &= test_resume_only_missing_segments()
    all_ok &= test_fingerprint_change_redoes_all()
    print()
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
