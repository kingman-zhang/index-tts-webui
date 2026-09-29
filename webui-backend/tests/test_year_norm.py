#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""年份读法归一化（app/year_norm.py）自测。

用法（在 webui-backend 下）：
    python tests/test_year_norm.py

四部分：
  1. 文本层：该改的改（含带空格写法）、不该动的一律不动
  2. 区间：两端都逐位读，分隔符统一「到」
  3. 不变性：不改入参、非 dict 行透传、ENABLED=0 时整体关闭
  4. 与 TN 的互操作（装了 wetext 才跑）：转换后的汉字进 TN 必须原样输出，
     且**用户原始报障句**经过「本模块 → TN」后年份必须是逐位读法。
     —— 这一部分才是本模块存在的理由：光看字符串相等不够。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import year_norm  # noqa: E402
from app.year_norm import (  # noqa: E402
    ENABLED,
    apply_year_rules,
    normalize_years,
    year_digits,
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
    # —— 核心：用户报障的形态 ——
    ("2011年", "二零一一年", "最简形态"),
    ("2011 年", "二零一一年", "★ 数字与「年」之间有空格（用户原文就是这个）"),
    (" 2011年", " 二零一一年", "前导空格在年份之外，保留（吞掉的只是数字与「年」之间那个）"),
    ("日本 2011 年的地震", "日本 二零一一年的地震", "★ 句中带空格"),
    (
        "在 2011 年遭受特大地震和海啸后，日本获得了难以计数的帮助。",
        "在 二零一一年遭受特大地震和海啸后，日本获得了难以计数的帮助。",
        "★ 用户原句：只吞「数字↔年」之间的空格，其余空格属上下文，不动",
    ),
    # —— 其他独立年份 ——
    ("1998年", "一九九八年", "含 9 与 8"),
    ("2024年", "二零二四年", "2000 年代"),
    ("公元1550年", "公元一五五零年", "带「公元」前缀，四位"),
    ("2011年3月11日", "二零一一年3月11日", "月日不归本层（TN 会读成三月十一日）"),
    # —— 区间：两端都逐位读，分隔符统一「到」——
    ("1550～1850年间", "一五五零到一八五零年间", "★ 全角波浪：TN 会把左端读成数值"),
    ("1550～1850年", "一五五零到一八五零年", "无「间」"),
    ("1550 ～ 1850 年间", "一五五零到一八五零年间", "★ 两侧带空格"),
    ("1550年至1850年", "一五五零年到一八五零年", "「至」"),
    ("1550到1850年", "一五五零到一八五零年", "「到」"),
    ("1550—1850年", "一五五零到一八五零年", "破折号"),
    ("1550-1850年", "一五五零到一八五零年", "★ 半角连字符也收敛成「到」，"
                                          "避免汉字之间留一个 - 没人管"),
    ("2020-2024年", "二零二零到二零二四年", "与 TN 自身结果一致"),
    ("从1550年开始，到1850年结束", "从一五五零年开始，到一八五零年结束",
     "两侧「年」都单独出现，不是区间形态"),
    # —— 刻意不动 ——
    ("2020-2024", "2020-2024", "右端无「年」→ 可能是减法/范围，交回 TN"),
    ("12345年", "12345年", "5 位不是年份"),
    ("201年", "201年", "3 位不处理（`公元850年` 同此，见 docstring 待定项）"),
    ("公元850年", "公元850年", "3 位年份交 TN（它读「八百五十年」）"),
    ("30 年房贷", "30 年房贷", "两位数字：`\\d{4}` 下限挡住"),
    ("20 年 前", "20 年 前", "两位数字"),
    ("数千年", "数千年", "已是汉字"),
    ("三千年的历史", "三千年的历史", "已是汉字"),
    ("9:30", "9:30", "时间不归本层（TN 读「九点三十分」）"),
    ("2011年度报告", "二零一一年度报告", "「年度」不是反例：TN 本身就读「二零一一年度报告」"),
    ("没有数字的普通句子", "没有数字的普通句子", "恒等"),
    ("", "", "空串"),
]


def part1_text() -> None:
    print("── 1/4 文本层 ──")
    for src, want, why in CASES:
        check(f"{why}｜{src!r}", normalize_years(src), want)
    check("year_digits(2011)", year_digits("2011"), "二零一一")
    check("year_digits 用「一」不用「幺」", year_digits("1"), "一")


def part2_idempotent() -> None:
    print("── 2/4 幂等：已是汉字的结果再跑一次不变 ──")
    for src, want, _why in CASES:
        check(f"幂等｜{want!r}", normalize_years(want), want)


def part3_invariance() -> None:
    print("── 3/4 不变性 ──")
    lines = [{"text": "2011 年", "speaker": "A"},
             "裸字符串行",
             {"text": "", "speaker": "B"},
             {"no_text": 1}]
    out = apply_year_rules(lines)
    check("不改入参 text", lines[0]["text"], "2011 年")
    check("输出已改", out[0]["text"], "二零一一年")
    check("其它字段保留", out[0]["speaker"], "A")
    check("非 dict 行透传", out[1], "裸字符串行")
    check("空 text 不炸", out[2]["text"], "")
    check("无 text 键不炸", out[3], {"no_text": 1})
    check("返回新列表", out is not lines, True)

    old = year_norm.ENABLED
    try:
        year_norm.ENABLED = False
        check("ENABLED=0 时整体关闭", apply_year_rules([{"text": "2011年"}])[0]["text"], "2011年")
    finally:
        year_norm.ENABLED = old
    check("测试后 ENABLED 复原", year_norm.ENABLED, ENABLED)


def part4_with_tn() -> None:
    print("── 4/4 与 TN 互操作（汉字必须对 TN 透明）──")
    try:
        import wetext
    except ImportError:
        print("  [skip] 未装 wetext（pip install wetext 后可跑）")
        return
    tn = wetext.Normalizer(remove_erhua=False, lang="zh", operator="tn")

    # ① 本层产出里「纯汉字」的那些，进 TN 必须原样出来（否则等于没修）
    #    跳过首尾带空白的样本：TN 会 trim 首尾空白（正常行为），
    #    那与本模块的「汉字透明性」无关。
    n = 0
    for _src, want, _why in CASES:
        if not want or any(ch.isdigit() for ch in want) or want != want.strip():
            continue
        check(f"TN 透明｜{want!r}", tn.normalize(want), want)
        n += 1
    print(f"     （共核对 {n} 条纯汉字产出；含 ASCII 数字的产出不在此列，"
          f"它们本来就要交给 TN）")

    # ② 用户原句：修之前 TN 读「两千零一十一 年」，修之后必须逐位
    sent = "在 2011 年遭受特大地震和海啸后，日本获得了难以计数的帮助。"
    check("修复前 TN 确实读错（基线）", "两千零一十一" in tn.normalize(sent), True)
    fixed = normalize_years(sent)
    out = tn.normalize(fixed)
    check("修复后 TN 输出含「二零一一年」", "二零一一年" in out, True)
    check("修复后不再出现数值读法", "两千零一十一" in out, False)
    print(f"     送进引擎的文本: {fixed}")

    # ③ 区间：TN 单独处理会把左端读成数值，本层修复后两端都是逐位
    rng = "1550～1850年间"
    check("修复前区间左端读错（基线）", "一千五百五十" in tn.normalize(rng), True)
    check("修复后区间正确", tn.normalize(normalize_years(rng)), "一五五零到一八五零年间")


def main() -> int:
    print(f"app/year_norm.py  YEAR_NORMALIZE 生效 = {ENABLED}\n")
    part1_text()
    part2_idempotent()
    part3_invariance()
    part4_with_tn()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
