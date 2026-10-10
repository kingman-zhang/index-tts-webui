#!/usr/bin/env python3
"""分段缓存 seg_cache 单测（无外部依赖，直接 python3 运行）。

覆盖：合成指纹的稳定性与敏感性、prepare 的三态（新建 / 命中 / 失配重建）、
读写往返与覆盖写、原子落盘无 .tmp 残留、缺失段返回 None、clear、prune、
meta 损坏时的降级。
用法：cd webui-backend && python3 tests/test_seg_cache.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import seg_cache  # noqa: E402


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected: {expected}")
        print(f"      actual:   {actual}")
    return ok


def _entries(texts=("甲", "乙"), emotions=(None, "happy"), gaps=(0, 500)):
    return [{"text": t, "emotion": e, "gap_ms": g} for t, e, g in zip(texts, emotions, gaps)]


def _fp(**over):
    kw = dict(engine="art", max_input_chars=2048, speed=1.0, voice="v.mp3", entries=_entries())
    kw.update(over)
    return seg_cache.fingerprint(**kw)


def test_fingerprint():
    """指纹是「缓存能不能复用」的唯一判据，必须覆盖所有会影响音频的因素。"""
    ok = True
    ok &= check("同输入 ⇒ 稳定", _fp(), _fp())
    ok &= check("换引擎 ⇒ 变", _fp() != _fp(engine="302ai"), True)
    ok &= check("换单次上限 ⇒ 变", _fp() != _fp(max_input_chars=2000), True)
    ok &= check("上限 None(不限) 与数字不同", _fp(max_input_chars=None) != _fp(), True)
    ok &= check("换语速 ⇒ 变", _fp() != _fp(speed=1.2), True)
    ok &= check("换音色 ⇒ 变", _fp() != _fp(voice="other.mp3"), True)
    ok &= check("改文本 ⇒ 变", _fp() != _fp(entries=_entries(texts=("甲", "丙"))), True)
    ok &= check("改情绪 ⇒ 变", _fp() != _fp(entries=_entries(emotions=(None, None))), True)
    ok &= check("改段后静音 ⇒ 变", _fp() != _fp(entries=_entries(gaps=(0, 0))), True)
    ok &= check("段序颠倒 ⇒ 变", _fp() != _fp(entries=_entries(texts=("乙", "甲"))), True)
    return ok


def test_prepare_and_roundtrip():
    ok = True
    root = Path(tempfile.mkdtemp()) / "seg_cache"
    tid = "q_abc"
    fp = _fp()

    ok &= check("首次 prepare ⇒ False（无可复用缓存）", seg_cache.prepare(root, tid, fp, 2), False)
    ok &= check("meta 已落地", seg_cache.load_meta(root, tid) is not None, True)
    ok &= check("prepare 之后尚无任何段", seg_cache.has_segment(root, tid, 0), False)

    seg_cache.write_segment(root, tid, 0, b"WAV0")
    seg_cache.write_segment(root, tid, 1, b"WAV1")
    ok &= check("段 0 可读回", seg_cache.read_segment(root, tid, 0), b"WAV0")
    ok &= check("段 1 可读回", seg_cache.read_segment(root, tid, 1), b"WAV1")
    ok &= check("has_segment 命中", seg_cache.has_segment(root, tid, 1), True)
    ok &= check("越界段 ⇒ read 返回 None", seg_cache.read_segment(root, tid, 9), None)
    ok &= check("越界段 ⇒ has 返回 False", seg_cache.has_segment(root, tid, 9), False)

    seg_cache.write_segment(root, tid, 1, b"NEW1")
    ok &= check("覆盖写生效", seg_cache.read_segment(root, tid, 1), b"NEW1")

    leftovers = [p.name for p in seg_cache.cache_dir(root, tid).iterdir() if p.name.endswith(".tmp")]
    ok &= check("落盘是原子的：无 .tmp 残留", leftovers, [])

    ok &= check("同指纹再 prepare ⇒ True（命中）", seg_cache.prepare(root, tid, fp, 2), True)
    ok &= check("命中时旧段仍在", seg_cache.read_segment(root, tid, 1), b"NEW1")

    ok &= check("异指纹 ⇒ False（失配重建）", seg_cache.prepare(root, tid, _fp(engine="302ai"), 2), False)
    ok &= check("失配后旧段被清空", seg_cache.read_segment(root, tid, 1), None)

    seg_cache.write_segment(root, tid, 0, b"X")
    ok &= check("段总数变化 ⇒ 失配", seg_cache.prepare(root, tid, fp, 3), False)
    ok &= check("段总数变化后旧段被清空", seg_cache.has_segment(root, tid, 0), False)

    seg_cache.write_segment(root, tid, 0, b"Y")
    seg_cache.clear(root, tid)
    ok &= check("clear 后目录消失", seg_cache.cache_dir(root, tid).exists(), False)
    ok &= check("clear 幂等（不存在也不抛）", seg_cache.clear(root, tid) is None, True)
    return ok


def test_prune():
    ok = True
    root = Path(tempfile.mkdtemp()) / "seg_cache"
    ok &= check("根目录不存在 ⇒ 0", seg_cache.prune(root, {"q_a"}), 0)

    for tid in ("q_a", "q_b", "q_gone"):
        seg_cache.prepare(root, tid, "fp", 1)
    ok &= check("已建 3 个缓存目录", sorted(p.name for p in root.iterdir()), ["q_a", "q_b", "q_gone"])

    ok &= check("删除 1 个孤儿", seg_cache.prune(root, {"q_a", "q_b"}), 1)
    ok &= check("现存任务的缓存被保留", sorted(p.name for p in root.iterdir()), ["q_a", "q_b"])
    return ok


def test_meta_corruption():
    """meta 损坏不能让任务崩：按「无缓存」处理，于是 prepare 重建目录。"""
    ok = True
    root = Path(tempfile.mkdtemp()) / "seg_cache"
    tid = "q_bad"
    seg_cache.prepare(root, tid, "fp", 1)
    (seg_cache.cache_dir(root, tid) / "meta.json").write_text("{oops", encoding="utf-8")
    ok &= check("损坏 ⇒ load_meta 返回 None", seg_cache.load_meta(root, tid), None)
    ok &= check("损坏 ⇒ prepare 判为失配", seg_cache.prepare(root, tid, "fp", 1), False)
    return ok


def test_task_id_sanitized():
    """task_id 里的路径分隔符不能逃出缓存根（与 queue_state.queue_file 同口径）。"""
    root = Path(tempfile.mkdtemp()) / "seg_cache"
    tid = "a/b\\c"
    d = seg_cache.cache_dir(root, tid)
    return check("路径分隔符被替换掉", d.parent, root)


def main() -> int:
    all_ok = True
    all_ok &= test_fingerprint()
    all_ok &= test_prepare_and_roundtrip()
    all_ok &= test_prune()
    all_ok &= test_meta_corruption()
    all_ok &= test_task_id_sanitized()
    print()
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
