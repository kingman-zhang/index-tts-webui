#!/usr/bin/env python3
"""mono_runner._flatten_segments 的**行间静音**单测（纯函数，无外部依赖）。

覆盖 2026-10-20 变更：单人路径换行后按 MONO_SILENCE_BETWEEN_MS（默认 200ms）
插入行间静音，并要求与行内 [pause:N] 叠加、末行不加、行级显式值优先。

用法：cd webui-backend && python3 tests/test_mono_line_gap.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines import EngineCapabilities  # noqa: E402
from app.mono_runner import MAX_GAP_MS, _flatten_segments  # noqa: E402

# 不声明 max_input_chars ⇒ 不触发超长切分，一行只按 [pause] 切
CAPS = EngineCapabilities(display_name="test")


def gaps(lines, between_ms=200):
    """取每段 gap_ms 序列，便于断言。"""
    return [e["gap_ms"] for e in _flatten_segments(lines, CAPS, between_ms)]


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected: {expected}")
        print(f"      actual:   {actual}")
    return ok


def main() -> int:
    all_ok = True

    # 换行 = 行间静音；末行不加
    all_ok &= check(
        "三行：前两行各 200，末行 0",
        gaps([{"text": "第一行"}, {"text": "第二行"}, {"text": "第三行"}]),
        [200, 200, 0],
    )
    # 与行内 [pause:N] 叠加（pause 500 + between 200）
    all_ok &= check(
        "行内 pause + 行间静音叠加",
        gaps([{"text": "甲[pause:0.5]乙"}, {"text": "丙"}]),
        [500, 200, 0],
    )
    # 行末 pause 与行间静音叠加：落在同一段上相加
    all_ok &= check(
        "行末 pause + 行间静音",
        gaps([{"text": "甲[pause:1]"}, {"text": "乙"}]),
        [1200, 0],
    )
    # 行级显式 silence_after_ms 优先于 between_ms（且与 pause 叠加）
    all_ok &= check(
        "显式 silence_after_ms=800（无 pause）",
        gaps([{"text": "甲", "silence_after_ms": 800}, {"text": "乙"}]),
        [800, 0],
    )
    # 显式 0 是「这行后面不要静音」，不是缺省 ⇒ 不能回落成 200
    all_ok &= check(
        "显式 0 不被默认值顶掉",
        gaps([{"text": "甲", "silence_after_ms": 0}, {"text": "乙"}]),
        [0, 0],
    )
    # 稿子末尾的空行不该让倒数第二行多吃静音
    all_ok &= check(
        "末尾空行不算末行",
        gaps([{"text": "甲"}, {"text": "乙"}, {"text": "   "}]),
        [200, 0],
    )
    # 中间空行跳过，不影响前后行的静音归属
    all_ok &= check(
        "中间空行",
        gaps([{"text": "甲"}, {"text": ""}, {"text": "乙"}]),
        [200, 0],
    )
    # between_ms=0 ⇒ 回到「硬拼接」的老行为（便于一键回滚）
    all_ok &= check(
        "between=0 时与旧行为一致",
        gaps([{"text": "甲"}, {"text": "乙"}], 0),
        [0, 0],
    )
    # 叠加后仍受 MAX_GAP_MS 钳制
    all_ok &= check(
        "叠加后钳到 10s",
        gaps([{"text": "甲[pause:9]", "silence_after_ms": 9000}, {"text": "乙"}]),
        [MAX_GAP_MS, 0],
    )
    # 全空/纯空行 ⇒ 无段（上层报「所有文本段均为空」）
    all_ok &= check("全空行", gaps([{"text": ""}, {"text": "  "}]), [])

    print()
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
