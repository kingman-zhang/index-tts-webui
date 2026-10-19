#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""区间读法归一化（app/range_norm.py）自测。

用法（在 webui-backend 下）：
    python tests/test_range_norm.py

五部分：
  1. 文本层：该改的改（含带空格写法、全角波浪号、百分号、量级后缀）、不该动的一律不动
  2. 幂等：产出再跑一次不变
  3. 不变性：不改入参、非 dict 行透传、ENABLED=0 时整体关闭
  4. **与 year_norm 的顺序耦合**：年份规则的全部语料在本层之后必须**一个字节都不变**
     —— 这是「绝不能影响年份逻辑」这条要求的机器化断言，不是靠人眼复查。
  5. 与 TN 的互操作（装了 wetext 才跑）：本层产出进 TN 必须原样输出，
     且用户报障句经过完整链路后不许再出现「一五」这种逐位读法。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import range_norm  # noqa: E402
from app.range_norm import (  # noqa: E402
    ENABLED,
    RANGE_SEPARATOR,
    SEP_CHARS,
    apply_range_rules,
    normalize_ranges,
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
    # —— 核心：用户报障的两句（2026-10-19）——
    (
        "对 1000 名 10 ~15 岁的儿童调查显示",
        "对 1000 名 十到十五岁的儿童调查显示",
        "★ 用户原句：左侧有空格 ⇒ TN 把 15 读成「一五」",
    ),
    (
        "2 ~ 6 个月后他们参与打架的次数就越多",
        "两到六个月后他们参与打架的次数就越多",
        "★ 用户原句：顺手吃掉两端空格",
    ),
    ("对1000名10~15岁的儿童调查显示", "对1000名十到十五岁的儿童调查显示", "无空格的紧凑写法"),
    # —— 基础形态 ——
    ("10~15岁", "十到十五岁", "最简"),
    ("2~6个月", "两到六个月", "单个 2 读「两」"),
    ("0~3岁", "零到三岁", "从 0 起"),
    ("1000~2000元", "一千到两千元", "四位数 + 单位 ⇒ 数值读法"),
    ("1.5~2.5岁", "一点五到二点五岁", "小数两端"),
    ("1,000~2,000元", "一千到两千元", "千分位"),
    ("20~30分钟", "二十到三十分钟", "常规区间"),
    ("第2~6章", "第二到六章", "序数语境 ⇒ 第二个 2 读「二」"),
    ("10~15年", "十到十五年", "★ 两位 + 年 = 时长，不是年份"),
    # —— 波浪号家族 ——
    ("10～15岁", "十到十五岁", "全角波浪号"),
    ("10〜15岁", "十到十五岁", "U+301C"),
    ("10∼15岁", "十到十五岁", "U+223C"),
    ("10˜15", "十到十五", "U+02DC"),
    # —— 量级后缀 ——
    ("1~2万", "一万到两万", "★ 量级要乘到两端，不能读「一到两万」"),
    ("1万~2万", "一万到两万", "两端都写量级"),
    ("2~3百万", "两百万到三百万", "复合量级"),
    ("10~15万元", "十万到十五万元", "量级 + 量词，量词留在外面"),
    # —— 百分号 ——
    ("10~15%", "百分之十到百分之十五", "★ 左端也要「百分之」"),
    ("5%~10%", "百分之五到百分之十", "两端都带 %"),
    ("5 % ~ 10 %", "百分之五到百分之十", "带空格"),
    ("5%~10", "百分之五到百分之十", "只在左端写 % 也按百分数读"),
    # —— 刻意不动：年份地盘（本层之前由 year_norm 处理）——
    ("1550～1850年间", "1550～1850年间", "★ 年份区间：正常顺序下 year_norm 已换成汉字"),
    ("2020年~2024年", "2020年~2024年", "两端带「年」"),
    ("850~950年", "850~950年", "双三位：year_norm 刻意留给 TN"),
    ("100~200年", "100~200年", "三位 + 年 = 时长区间，同上"),
    # —— 刻意不动：方向不唯一 ——
    ("2020~2024", "2020~2024", "★ 裸四位对：可能是年份也可能是钱数，不猜"),
    ("1500~1800", "1500~1800", "同上"),
    ("007~008", "007~008", "前导 0 ⇒ 编号"),
    ("A1~2", "A1~2", "字母在前 ⇒ 编号"),
    ("-5~3", "-5~3", "带符号 ⇒ 不是区间"),
    ("1~2~3", "1~2~3", "三段及以上，不是「从 A 到 B」"),
    ("10~", "10~", "单边"),
    ("约~10", "约~10", "单边"),
    # —— 刻意不动：连字符（另一个已知缺陷，见模块 docstring）——
    ("3-5天", "3-5天", "连字符不归本层"),
    ("400-123-4567", "400-123-4567", "分段号码"),
    # —— 裸千/百 不当量级（与今天 TN 的结果一致，不是回归）——
    ("3~5千米", "三到五千米", "★ 千米 = km，不是 3000~5000"),
    ("3~5千克", "三到五千克", "千克 = kg"),
    ("3~5千字", "三到五千字", "裸「千」不猜"),
    # —— 兜底：两端已是汉字数字 ——
    ("九点三十分~十点三十分", "九点三十分到十点三十分", "★ time_norm 之后留下的残局"),
    ("十五~二十岁", "十五到二十岁", "本来就是汉字"),
    # —— 恒等 ——
    ("没有数字的普通句子", "没有数字的普通句子", "恒等"),
    ("", "", "空串"),
]


def _load_year_cases() -> list[tuple[str, str, str]]:
    """借 `test_year_norm.CASES` 当「年份规则全语料」，不复制一份（复制必然漂移）。"""
    path = Path(__file__).with_name("test_year_norm.py")
    spec = importlib.util.spec_from_file_location("_year_norm_cases", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # 有 __main__ 守卫，不会执行 main()
    return list(mod.CASES)


def part1_text() -> None:
    print("── 1/5 文本层 ──")
    for src, want, why in CASES:
        check(f"{why}｜{src!r}", normalize_ranges(src), want)


def part2_idempotent() -> None:
    print("── 2/5 幂等：产出再跑一次不变 ──")
    for _src, want, _why in CASES:
        check(f"幂等｜{want!r}", normalize_ranges(want), want)


def part3_invariance() -> None:
    print("── 3/5 不变性 ──")
    lines = [{"text": "10~15岁", "speaker": "A"},
             "裸字符串行",
             {"text": "", "speaker": "B"},
             {"no_text": 1}]
    out = apply_range_rules(lines)
    check("不改入参 text", lines[0]["text"], "10~15岁")
    check("输出已改", out[0]["text"], "十到十五岁")
    check("其它字段保留", out[0]["speaker"], "A")
    check("非 dict 行透传", out[1], "裸字符串行")
    check("空 text 不炸", out[2]["text"], "")
    check("无 text 键不炸", out[3], {"no_text": 1})
    check("返回新列表", out is not lines, True)

    old = range_norm.ENABLED
    try:
        range_norm.ENABLED = False
        check("ENABLED=0 时整体关闭", apply_range_rules([{"text": "10~15岁"}])[0]["text"],
              "10~15岁")
    finally:
        range_norm.ENABLED = old
    check("测试后 ENABLED 复原", range_norm.ENABLED, ENABLED)


def part4_year_untouched() -> None:
    print("── 4/5 与 year_norm 的顺序耦合：年份输出必须一个字节都不变 ──")
    from app.year_norm import normalize_years

    year_cases = _load_year_cases()
    changed = []
    for src, want, why in year_cases:
        got = normalize_ranges(want)  # 顺序：year_norm → range_norm
        if got != want:
            changed.append((why, src, want, got))
        check(f"年份不被改写｜{why}", got, want)

    # 再加一层：原文直接过两层，结果必须等于 year_norm 的单独产出
    for src, want, why in year_cases:
        check(f"整链｜{why}", normalize_ranges(normalize_years(src)), want)

    if changed:
        print(f"     !! 有 {len(changed)} 条年份输出被区间层改写了 —— 必须修")
    print(f"     （共核对 {len(year_cases)} 条年份语料）")


def part5_with_tn() -> None:
    print("── 5/5 与 TN 互操作 ──")
    try:
        import wetext
    except ImportError:
        print("  [skip] 未装 wetext（pip install wetext 后可跑）")
        return
    tn = wetext.Normalizer(remove_erhua=False, lang="zh", operator="tn")

    # ① 纯汉字产出进 TN 必须原样出来
    n = 0
    for _src, want, _why in CASES:
        if not want or any(ch.isdigit() for ch in want) or want != want.strip():
            continue
        check(f"TN 透明｜{want!r}", tn.normalize(want), want)
        n += 1
    print(f"     （共核对 {n} 条纯汉字产出）")

    # ② 用户报障句：修之前 TN 读「十到一五岁」，修之后必须全对
    sent = "对 1000 名 10 ~15 岁的儿童调查显示"
    check("修复前 TN 读错（基线）", "一五岁" in tn.normalize(sent), True)
    fixed = normalize_ranges(sent)
    out = tn.normalize(fixed)
    check("修复后不再出现「一五岁」", "一五岁" in out, False)
    check("修复后读到「十到十五岁」", "十到十五岁" in out, True)

    sent2 = "2 ~ 6 个月后他们参与打架的次数就越多"
    check("第二句修复后", "两到六个月" in tn.normalize(normalize_ranges(sent2)), True)


def part6_descriptors() -> None:
    print("── 自描述：常量与正则必须一致 ──")
    # `/api/version` 的 range_norm_separator 直接取这个常量：取值域才可判版本
    check("RANGE_SEPARATOR", RANGE_SEPARATOR, "到")
    for ch in "~～〜∼˜⁓":
        check(f"SEP_CHARS 收 {ch!r}", ch in SEP_CHARS, True)
    for ch in "-–—－":
        check(f"SEP_CHARS 不收 {ch!r}（连字符另有误判风险）", ch in SEP_CHARS, False)
    # 量级：万/亿/百万/千万 认；裸 千/百 不认
    import re
    mult = re.compile(range_norm._MULT_PATTERN)
    for text, want in [("万", True), ("亿", True), ("百万", True), ("千万", True),
                       ("千", False), ("百", False), ("米", False)]:
        check(f"量级 {text!r}", bool(mult.fullmatch(text)), want)


def main() -> int:
    print(f"app/range_norm.py  RANGE_NORMALIZE 生效 = {ENABLED}\n")
    part1_text()
    part2_idempotent()
    part3_invariance()
    part4_year_untouched()
    part5_with_tn()
    part6_descriptors()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
