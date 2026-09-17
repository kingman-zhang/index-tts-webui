#!/usr/bin/env python3
"""mono_runner.split_by_pauses 纯函数单测（无外部依赖，直接 python3 运行）。

用法：cd webui-backend && python3 tests/test_mono_pauses.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.mono_runner import MAX_GAP_MS, MIN_GAP_MS, split_by_pauses  # noqa: E402


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expected: {expected}")
        print(f"      actual:   {actual}")
    return ok


def main() -> int:
    all_ok = True

    # 基本切分
    all_ok &= check(
        "基本切分",
        split_by_pauses("你好[pause:0.5]世界"),
        [("你好", 500), ("世界", 0)],
    )
    # 两位小数
    all_ok &= check(
        "两位小数 0.25s",
        split_by_pauses("甲[pause:0.25]乙"),
        [("甲", 250), ("乙", 0)],
    )
    # <#> 固定 0.5s
    all_ok &= check(
        "<#> 固定 0.5s",
        split_by_pauses("甲<#>乙"),
        [("甲", 500), ("乙", 0)],
    )
    # 相邻停顿叠加
    all_ok &= check(
        "相邻停顿叠加",
        split_by_pauses("a[pause:0.5][pause:1]b"),
        [("a", 1500), ("b", 0)],
    )
    # 停顿带空格变体
    all_ok &= check(
        "标记内空格",
        split_by_pauses("a[pause: 1.5 ]b"),
        [("a", 1500), ("b", 0)],
    )
    # 上钳制 10s
    all_ok &= check(
        "上钳制 10s",
        split_by_pauses("a[pause:99]b"),
        [("a", MAX_GAP_MS), ("b", 0)],
    )
    # 下钳制 50ms
    all_ok &= check(
        "下钳制 50ms",
        split_by_pauses("a[pause:0.01]b"),
        [("a", MIN_GAP_MS), ("b", 0)],
    )
    # 段首停顿 → 并入下一子段静音
    all_ok &= check(
        "段首停顿",
        split_by_pauses("[pause:1]你好"),
        [("你好", 1000)],
    )
    # 段尾停顿 → 并入最后子段静音
    all_ok &= check(
        "段尾停顿",
        split_by_pauses("你好[pause:1]"),
        [("你好", 1000)],
    )
    # 无停顿
    all_ok &= check("无停顿", split_by_pauses("你好世界"), [("你好世界", 0)])
    # 纯停顿段（无文字）→ 空列表
    all_ok &= check("纯停顿段", split_by_pauses("[pause:1][pause:0.5]"), [])
    # 未知标记不匹配、保留原文
    all_ok &= check(
        "未知标记保留",
        split_by_pauses("a[pause:abc]b[pausex]c"),
        [("a[pause:abc]b[pausex]c", 0)],
    )
    # 混合中文长句（模拟前端提交）
    all_ok &= check(
        "多子段+尾停顿",
        split_by_pauses("开场白[pause:0.25]第一段内容[pause:1]第二段内容[pause:0.5]"),
        [("开场白", 250), ("第一段内容", 1000), ("第二段内容", 500)],
    )

    print()
    print("ALL PASS" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
