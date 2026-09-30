#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间读法归一化（app/time_norm.py）自测。

用法（在 webui-backend 下）：
    python tests/test_time_norm.py

五部分：
  1. 文本层：该改的改（含带空格写法）、不该动的一律不动
  2. 幂等：已是汉字的结果再跑一次不变
  3. 不变性：不改入参、非 dict 行透传、ENABLED=0 时整体关闭
  4. 与 TN 的互操作（装了 wetext 才跑）：**这一部分才是本模块存在的理由** ——
     实测 TN 对 `12 : 30` / `0:30` 会读成「十二比三十 / 零比三十」（比例），
     本层修复后必须读成时间；同时 `12:30:45` 本层刻意不碰，要验证 TN 自己读得对。
  5. 自描述：MAX_PARTS 与正则实际接受的范围一致
     （`/api/version` 与部署自检靠 `time_norm_max_parts` 分辨「是哪一版」）。

本表的用例与 `tts-server/tests/test_time_rule.py` 逐条对应：两条链路（正式合成走
backend 前处理，音色试听直连 tts-server）必须给出同一套读法，改动必须同步。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import time_norm  # noqa: E402
from app.time_norm import (  # noqa: E402
    ENABLED,
    MAX_PARTS,
    apply_time_rules,
    chinese_under_100,
    normalize_times,
)

PASS = FAIL = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         expect: {expected!r}\n         actual: {actual!r}")


# (原文, 期望输出, 说明)
CASES: list[tuple[str, str, str]] = [
    # —— 该改的 ——
    ("12:30", "十二点三十分", "最简形态"),
    ("会议定在12:30开始", "会议定在十二点三十分开始", "★ 句中"),
    ("12 : 30", "十二点三十分", "★ 冒号两侧带空格（TN 实测退化成「十二比三十」）"),
    ("0:30", "零点三十分", "★ 小时为 0（TN 实测退化成「零比三十」）"),
    ("0:00", "零点整", "小时为 0 且整点"),
    ("9:05", "九点零五分", "分钟 < 10 必须补零"),
    ("8:00", "八点整", "整点"),
    ("23:59", "二十三点五十九分", "上界"),
    ("15:45", "十五点四十五分", "十五（不是「一十五」）"),
    ("20:20", "二十点二十分", "二十（不是「两十」）"),
    ("3:16", "三点十六分", "TN 对「3:16」也读时间 ⇒ 不设章节否决（已知取舍，两边一致）"),
    # —— 不该动的 ——
    ("16:9", "16:9", "比例（分钟一位，不命中）"),
    ("3:2", "3:2", "比分（分钟一位，不命中）"),
    ("1:2", "1:2", "版本号（分钟一位，不命中）"),
    ("12:30:45", "12:30:45", "三段式：整串不碰，交回 TN 读秒"),
    ("1:12:30", "1:12:30", "三段式变体：整串不碰"),
    ("99:99", "99:99", "时/分越界"),
    ("24:00", "24:00", "「二十四点」合法，交回 TN"),
    ("123:45", "123:45", "更长的数字串"),
    ("12:345", "12:345", "分钟三位"),
    ("第3章16节", "第3章16节", "章节引用（分钟一位，不命中）"),
]


def part1_text() -> None:
    print("── 1/5 文本层 ──")
    for src, want, why in CASES:
        check(f"{why}｜{src!r}", normalize_times(src), want)
    check("chinese_under_100(0)", chinese_under_100(0), "零")
    check("chinese_under_100(10)", chinese_under_100(10), "十")
    check("chinese_under_100(15)", chinese_under_100(15), "十五")
    check("chinese_under_100(20)", chinese_under_100(20), "二十")
    check("chinese_under_100(30)", chinese_under_100(30), "三十")
    check("chinese_under_100(59)", chinese_under_100(59), "五十九")
    check("空串不炸", normalize_times(""), "")


def part2_idempotent() -> None:
    print("── 2/5 幂等：已是汉字的结果再跑一次不变 ──")
    for src, want, _why in CASES:
        check(f"幂等｜{want!r}", normalize_times(want), want)


def part3_invariance() -> None:
    print("── 3/5 不变性 ──")
    lines = [{"text": "12:30 开会", "speaker": "A"},
             "裸字符串行",
             {"text": "", "speaker": "B"},
             {"no_text": 1}]
    out = apply_time_rules(lines)
    check("不改入参 text", lines[0]["text"], "12:30 开会")
    check("输出已改", out[0]["text"], "十二点三十分 开会")
    check("其它字段保留", out[0]["speaker"], "A")
    check("非 dict 行透传", out[1], "裸字符串行")
    check("空 text 不炸", out[2]["text"], "")
    check("无 text 键不炸", out[3], {"no_text": 1})
    check("返回新列表", out is not lines, True)

    old = time_norm.ENABLED
    try:
        time_norm.ENABLED = False
        check("ENABLED=0 时整体关闭", apply_time_rules([{"text": "12:30"}])[0]["text"], "12:30")
    finally:
        time_norm.ENABLED = old
    check("测试后 ENABLED 复原", time_norm.ENABLED, ENABLED)


def part4_with_tn() -> None:
    print("── 4/5 与 TN 互操作（本模块存在的理由）──")
    try:
        import wetext
    except ImportError:
        print("  [skip] 未装 wetext（pip install wetext 后可跑）")
        return
    tn = wetext.Normalizer(remove_erhua=False, lang="zh", operator="tn")

    # ① 实测缺口：TN 把这两类读成「比」（比例），本层必须修成时间
    check("基线：TN 读错「12 : 30」（带空格）", tn.normalize("12 : 30"), "十二比三十")
    check("修复后 TN 读对", tn.normalize(normalize_times("12 : 30")), "十二点三十分")
    check("基线：TN 读错「0:30」（小时为 0）", "零比三十" in tn.normalize("0:30"), True)
    check("修复后 TN 不再是「比」", "比" in tn.normalize(normalize_times("0:30")), False)

    # ② 本层刻意不碰的三段式：TN 自己就能读秒 ⇒ 不碰是对的
    check("基线：TN 会读「12:30:45」的秒", tn.normalize("12:30:45"), "十二点三十分四十五秒")
    check("本层不碰三段式 ⇒ 仍交给 TN 读秒",
          tn.normalize(normalize_times("12:30:45")), "十二点三十分四十五秒")

    # ③ 汉字透明性：本层的纯汉字产出进 TN 必须原样出来（否则等于没修）
    #    跳过含 ASCII 数字的产出（那些本来就该交给 TN）与首尾带空白的样本
    #    （TN 会 trim 首尾空白，与本模块无关）。
    n = 0
    for _src, want, _why in CASES:
        if not want or any(ch.isdigit() for ch in want) or want != want.strip():
            continue
        check(f"TN 透明｜{want!r}", tn.normalize(want), want)
        n += 1
    print(f"     （共核对 {n} 条纯汉字产出）")

    # ④ 整句：TN 单独跑会把带空格的时间读错，本层接上后不再出现「比」
    sent = "会议定在 12 : 30 开始，下午 0:30 再确认。"
    check("基线：整句 TN 出现「比」（带空格的时间）", "比" in tn.normalize(sent), True)
    check("修复后整句无「比」", "比" in tn.normalize(normalize_times(sent)), False)


def part5_descriptors() -> None:
    print("── 5/5 自描述：常量与正则必须一致 ──")
    # 这是 /api/version 的 time_norm_max_parts 与部署自检的判据：
    # 看到 2 = 只做「时:分」；若将来做「时:分:秒」应变成 3。
    check("MAX_PARTS", MAX_PARTS, 2)
    # 行为核对（比读 pattern 字符串稳）：符合/不符合的各来几条
    # `99:99` / `24:00` 属**命中但范围否决**（正则认形态、replace 时越界原样返回），
    # 所以放 should_match 一侧；纯形态不合法的才在 should_not。
    should_match = ["12:30", "0:30", "9:05", "12 : 30", "23:59", "99:99", "24:00"]
    should_not = ["12:30:45", "1:12:30", "16:9", "3:2", "123:45", "12:345"]
    for s in should_match:
        check(f"应命中｜{s!r}", bool(time_norm._TIME_PATTERN.search(s)), True)
    for s in should_not:
        check(f"不应命中｜{s!r}", bool(time_norm._TIME_PATTERN.search(s)), False)
    # 越界形态会被正则命中但 replace 时原样返回（范围否决），两者都要对
    check("越界命中但原样返回｜99:99", normalize_times("99:99"), "99:99")
    check("越界命中但原样返回｜24:00", normalize_times("24:00"), "24:00")


def main() -> int:
    print(f"app/time_norm.py  TIME_NORMALIZE 生效 = {ENABLED}\n")
    part1_text()
    part2_idempotent()
    part3_invariance()
    part4_with_tn()
    part5_descriptors()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
