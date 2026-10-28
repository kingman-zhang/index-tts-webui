#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不发音符号归一化（app/symbol_norm.py）自测。

用法（在 webui-backend 下）：
    python tests/test_symbol_norm.py

五部分：
  1. 该改的改：括号→逗号、书名号/引号→删除、破折号→逗号、分号冒号→逗号、
     删除后的接缝清理（连续逗号/顿号、句末标点前的逗号、行首行尾逗号）
  2. 不该动的一律不动：`%` `$` `¥` `/` `+` `=` `#`（有 TN 读法）、半角 `'`
     （英文缩写）、数字区间用的破折号、`[pause:N]` 与 `<#>`、纯中文
  3. **与引擎前端的契约**：产出里不许再出现「会被 char_rep_map 折成单引号」
     的字符 —— 这是本次修复的机器化断言，不是靠人眼复查
  4. 幂等 + 不变性（不改入参、非 dict 行透传、ENABLED=0 时整体关闭）
  5. 纯符号行：清完是空 ⇒ 原样返回（不能造出空段落）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import symbol_norm  # noqa: E402
from app.symbol_norm import apply_symbol_rules, clean_symbols  # noqa: E402

PASS = FAIL = 0


def check(name: str, actual, expected, quiet: bool = False) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        if not quiet:
            print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     期望: {expected!r}\n     实际: {actual!r}")


def section(title: str) -> None:
    print(f"\n{title}")


# ── 1. 该改的改 ────────────────────────────────────────────────
section("1. 该改的改")

check("全角括号 → 逗号（插入语保留停顿）",
      clean_symbols("余华（作家）说"), "余华，作家，说")
check("半角括号 → 逗号",
      clean_symbols("苹果(Apple)公司"), "苹果，Apple，公司")
check("方括号 → 逗号（用户报的 `[ ]`）",
      clean_symbols("这是[测试]内容"), "这是，测试，内容")
check("分号 → 逗号",
      clean_symbols("甲；乙"), "甲，乙")
check("全角冒号 → 逗号",
      clean_symbols("注意：别动"), "注意，别动")
check("书名号 → 删除（连读）",
      clean_symbols("《盗梦空间》是由"), "盗梦空间是由")
check("中文引号 → 删除",
      clean_symbols("他说“你好”"), "他说你好")
check("破折号 → 逗号",
      clean_symbols("他——来了"), "他，来了")
check("用户原句（多符号混排）",
      clean_symbols("他说（笑）你好；这是[测试]内容。"),
      "他说，笑，你好，这是，测试，内容。")
check("书名号 + 引号 + 括号混排（真实产品文案）",
      clean_symbols("余华（作家）在《活着》里写道：“人是为了活着本身而活着”。"),
      "余华，作家，在活着里写道，人是为了活着本身而活着。")

section("1b. 删除后的接缝清理")
check("连续逗号折叠",
      clean_symbols("甲（一）（二）乙"), "甲，一，二，乙")
check("逗号 + 顿号折叠（[1] 引用标记那种接缝）",
      clean_symbols("名单：[1]张三、[2]李四。"), "名单，1，张三，2，李四。")
check("句末标点前的逗号删掉",
      clean_symbols("“你好”，他说。"), "你好，他说。")
check("行首逗号删掉", clean_symbols("，开头"), "开头")
check("行尾逗号删掉", clean_symbols("结尾，"), "结尾")


# ── 2. 不该动的一律不动 ────────────────────────────────────────
section("2. 不该动的一律不动")

check("百分号/货币/除号/加号等有 TN 读法，一律不碰",
      clean_symbols("50%的人花$12.5看A/B，1+1=2，#3"), "50%的人花$12.5看A/B，1+1=2，#3")
check("半角单引号（英文缩写）不碰",
      clean_symbols("it's a good day"), "it's a good day")
check("数字区间的破折号留给 TN（range_norm 刻意不收）",
      clean_symbols("2010—2015年的变化"), "2010—2015年的变化")
check("数字区间的半角连字符本来就不碰",
      clean_symbols("3-5天"), "3-5天")
check("纯中文原样", clean_symbols("今天天气不错。"), "今天天气不错。")
check("正常逗号句号不碰", clean_symbols("甲，乙。丙！丁？"), "甲，乙。丙！丁？")
check("省略号不碰", clean_symbols("这酒……有毒"), "这酒……有毒")

section("2c. 英文行用半角逗号（不往英文里插全角 `，`）")
check("英文括号 → `, `",
      clean_symbols("Apple (Cupertino) said; it's fine."), "Apple, Cupertino, said, it's fine.")
check("英文引号删除 + 破折号 → `, `",
      clean_symbols('He said "hello" — then left.'), "He said hello, then left.")
check("英文货币/数字不动",
      clean_symbols("The price is $12.5 (about 90 yuan)."),
      "The price is $12.5, about 90 yuan.")
check("混排（含汉字）按中文处理",
      clean_symbols("苹果(Apple)公司"), "苹果，Apple，公司")

section("2b. 停顿标记必须原样穿过")
check("[pause:N] 保留",
      clean_symbols("他说[pause:0.5]你好"), "他说[pause:0.5]你好")
check("<#> 保留", clean_symbols("他说<#>你好"), "他说<#>你好")
check("停顿标记与括号混排（方括号不能被当成括注吃掉）",
      clean_symbols("（笑）[pause:1]（停顿）"), "笑，[pause:1]，停顿")
check("多个停顿标记各自保留",
      clean_symbols("甲[pause:0.3]乙[pause:2]丙<#>丁"),
      "甲[pause:0.3]乙[pause:2]丙<#>丁")


# ── 3. 与引擎前端的契约 ────────────────────────────────────────
section("3. 与引擎前端的契约：产出里不许再有「会变成单引号」的字符")

# 这张表是从 indextts/utils/front.py:16-52 的 char_rep_map 里**值为 `'` 的那些键**
# 抄过来的。抄而不是 import 的理由：index-tts-main 是嵌套 git 仓库，不参与本进程的
# 依赖；用一份快照断言，规则漂移时这条测试会失败，正好提醒复查（2026-10-28）。
_TO_SINGLE_QUOTE = "（）()《》【】[]「」\"“”‘’"

bad = [ch for ch in _TO_SINGLE_QUOTE
       if ch in clean_symbols("甲乙" + ch + "丙丁")]
check("清洗后不残留任何「会折成单引号」的字符", bad, [])

check("真实句子的产出里也没有这类字符",
      [ch for ch in _TO_SINGLE_QUOTE
       if ch in clean_symbols("他说（笑）：「你好」，这是[测试]《书名》。")],
      [])

check("缩略语里的 `'` 仍会留下（这是刻意保留的，不算残留）",
      "'" in clean_symbols("it's fine"), True)


# ── 4. 幂等 + 不变性 ─────────────────────────────────────────
section("4. 幂等 + 不变性")

CASES = [
    "他说（笑）你好；这是[测试]内容。",
    "余华（作家）在《活着》里写道：“人是为了活着本身而活着”。",
    "名单：[1]张三、[2]李四。",
    "他说[pause:0.5]你好<#>再见。",
    "2010—2015年的变化",
    "50%的人$12.5元 it's fine",
]
for text in CASES:
    once = clean_symbols(text)
    check(f"幂等: {text[:16]}…", clean_symbols(once), once, quiet=True)

check("空串", clean_symbols(""), "")
check("None 透传", clean_symbols(None), None)

lines = [{"text": "甲（乙）"}, {"speaker": "A"}, "不是 dict", {"text": ""}]
out = apply_symbol_rules(lines)
check("逐行处理", out[0]["text"], "甲，乙")
check("无 text 的行透传", out[1], {"speaker": "A"})
check("非 dict 透传", out[2], "不是 dict")
check("空 text 不炸", out[3]["text"], "")
check("不修改入参", lines[0]["text"], "甲（乙）")
check("返回新对象", out[0] is lines[0], False)
check("ENABLED 缺省为开", symbol_norm.ENABLED, True)


# ── 5. 纯符号行 ──────────────────────────────────────────────
section("5. 纯符号行：清完是空 ⇒ 原样返回（不造空段落）")

check("只有一对括号", clean_symbols("（）"), "（）")
check("只有引号", clean_symbols("“”"), "“”")
check("只有破折号", clean_symbols("——"), "——")
check("空白行原样", clean_symbols("   "), "   ")


print(f"\n{'=' * 46}\n通过 {PASS}，失败 {FAIL}\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
