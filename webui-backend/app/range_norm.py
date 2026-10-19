"""区间读法归一化：`10~15岁` → `十到十五岁`（默认开启，RANGE_NORMALIZE=0 关闭）。

问题（2026-10-19 用户报障）
==========================
从网页/文档粘贴的文本里，区间几乎都写成波浪号：

    对 1000 名 10 ~15 岁的儿童调查显示
    2 ~ 6 个月后他们参与打架的次数就越多

合成出来的音频里那个 `~` **完全不出声**（读成「十 十五岁」），听起来像漏字。

为什么本层必须存在（不能指望 TN）
==================================
`num_value_norm` 有一条「两侧都是数字的分隔符 ⇒ 否决」的规则（`_RUN_SEPS`
含 `~`），本意是把日期 / 时间 / 区间 / 号码整段交回 TN。问题是 TN 对波浪号
**只在数字紧邻时才做对**，一旦带上粘贴文本里的空格就不稳 —— 实测（wetext 替身，
`lang='zh', operator='tn'`，2026-10-19）：

    10~15岁的儿童     -> 十到十五岁的儿童          ✓
    10 ~15 岁         -> 十到一五岁                ✗ 左空格 + 右无空格 ⇒ 右端逐位读
    2 ~ 6 个月        -> 两到六个月                ✓（两侧都有空格反而对）
    1000 名 10 ~15 岁 -> 一千名 十到一五岁          ✗ 用户报障句
    1~2万             -> 一到两万                  ✗ 该读「一万到两万」
    2~3百万           -> 两到三百万                ✗ 该读「两百万到三百万」
    10~15%            -> 十到百分之十五            ✗ 左端漏了「百分之」
    10 - 15 岁        -> 十减一五岁                ✗ 连字符被读成「减」

更要紧的是：线上是**云引擎**，TN 是服务端实现、我们控制不了，版本一变行为就变。
按 `year_norm` 同一条思路 —— **在本层直接输出汉字**，汉字对 TN 透明，结果与跑
哪个引擎无关、可预期、可测试。

规则（高精度，宁可少改）
========================
两侧都是阿拉伯数字、中间是波浪号家族之一（`~ ～ 〜 ∼ ˜ ⁓`）⇒ 两端按**数值**读，
分隔符统一成「到」。可选的百分号 / 数量级后缀一起吃掉：

| 形态 | 输出 |
|---|---|
| `10~15岁` | `十到十五岁` |
| `10 ~15 岁` / `2 ~ 6 个月` | `十到十五岁` / `两到六个月`（顺手吃掉两端空格） |
| `1000~2000元` | `一千到两千元` |
| `1.5~2.5岁` | `一点五到二点五岁` |
| `1~2万` / `1万~2万` / `2~3百万` | `一万到两万` / `一万到两万` / `两百万到三百万` |
| `5%~10%` / `10~15%` | `百分之五到百分之十` / `百分之十到百分之十五` |
| `第2~6章` | `第二到六章`（「二/两」的语境沿用数值层规则） |

**数量级只认 `万` / `亿` / `百万` / `千万`**，且要乘到两端：`1~2万` 若读成
「一到两万」，口语里本身就是病句（到底是「1 到 2 万」还是「1 万到 2 万」）。
**刻意不收裸 `千` / `百`**：`3~5千米` 是「三到五千米」（千米 = km）而不是
「三千到五千米」，`3~5千克` / `3~5百年` 同理 —— 千/百 既可能是数量级又可能是
量词的首字，方向不唯一，收益为负。这类仍交回 TN（与今天一致，不是回归）。
**亿级放大后超过 8 位就整体放弃**（`5~10亿`）：`read_value` 只按「万」分段，
9 位以上会静默丢高位；TN 对这种形态实测读得对，交给它更安全。

另有一道**兜底**（`_RESIDUAL_TILDE`）：两端已经是**汉字数字**的波浪号也收敛成
「到」—— 专门收前一道工序留下的残局。`9:30~10:30` 先被 `time_norm` 换成
「九点三十分~十点三十分」，主规则（两侧必须是阿拉伯数字）就再也看不到它了，
而汉字之间的 `~` 没有任何一层会管。这一道**只认汉字数字 ↔ 汉字数字**：
主规则否决过的形态（年份 / 编号 / 分段号码）不会被它"救"回来。

不碰（都是刻意）
================
- **年份区间**：`1550～1850年` / `2011 年 ~ 2015 年` / `850～950年` / `100~200年`
  —— 一律留给 `year_norm`（它在**本层之前**跑）与 TN。判据见 `_year_territory()`：
  某一端 ≥3 位且紧跟「年」⇒ 年份地盘，整体跳过。正常顺序下年份区间早被
  `year_norm` 换成汉字，本层根本看不到；这道否决是给 `YEAR_NORMALIZE=0` 的配置兜底，
  **和 `num_value_norm._four_digit_year` 同一个理由**。
- **裸四位数对**（`2020~2024`、`1500~1800`）后面没有单位/量词：可能是年份也可能是
  钱数，**方向不唯一 ⇒ 不猜**，交回 TN（与今天的行为一致）。后面有单位
  （`1000~2000元`）才按数值读。
- **`-` / `—` / `－` 等连字符**：`3-5天` 该读「三到五天」，但同一个字符在
  `400-123-4567`（号码）、`2020-2024`（年份）、`A-1`（编号）里含义完全不同，
  误判代价高于收益。**本层只收波浪号家族**，连字符仍交回 TN（它读成「减」，
  这是另一个已知缺陷，见模块末尾「下一步」）。
- **两端有前导 0 的数字**（`007~008`、`08~09`）：多半是编号，不是数值。
- **超过 `MAX_DIGITS` 位的数字**：可能是号码，交回号码层 / TN。
- **三段及以上**（`1~2~3`）：字面上不是「从 A 到 B」，不猜。
- **`~` 单边**（`约~10个`、`10~`）：构不成区间。

顺序与落点
==========
`glossary → name_punct → year_norm → time_norm → range_norm → num_value_norm → number_norm`

- **必须在 `year_norm` 之后**：年份区间先被它换成汉字（`一五五零到一八五零年`），
  本层只看到纯汉字、不会去"重读"一遍 —— 这是「不影响年份逻辑」的结构性保证，
  不靠正则互相避让（`tests/test_range_norm.py` 第 4 部分把这条写成了断言）。
- **必须在 `num_value_norm` 之前**：本层把两端换成汉字后，数值层再也看不到那串
  阿拉伯数字。反过来（数值层先跑）不成立：它的 `_RUN_SEPS` 否决会把两端原样留下，
  这正是今天 `10 ~15 岁` 里 `10` 活着进模型的原因。
- 落点同其它层：`queue_worker._execute_task` 作用于**合成副本**，不回写
  `task["lines"]`（任务详情、重试计费、磁盘存档一律是用户原文）。

启用条件：`RANGE_NORMALIZE=1`（默认）。改 `.env` 后必须重启 backend —— config 只在启动时读。

版本可观测
==========
`/api/version` 报 `text_pipeline.range_normalize` 与 **`range_norm_separator`**
（取值而非开关）。理由同 `year_norm_min_digits`：只有布尔开关时，「输出『到』」
与「输出『至』」两版都是 `true`，端点上分辨不出来。看到 `range_normalize: true`
即说明进程加载的是含本层的代码。

下一步（刻意留白）
==================
连字符区间（`3-5天`）没做。要做的话判据应当是「两侧都是 1~2 位数字、且紧跟
量词/单位」，避开号码与年份 —— 那是独立的一层，不该塞进这里。
"""

from __future__ import annotations

import os
import re
from decimal import Decimal

# `num_value_norm` 的东西**刻意 import 而非复制**：
# `read_value` 决定「15 → 十五」「1000 → 一千」，`_should_use_er` 决定「2 → 两还是二」，
# `VALUE_UNITS` 决定「后面是不是量词」。复制一份就会出现两层读法不一致
# （本层「两到六个月」、数值层「二个月」）。
from .num_value_norm import (
    MAX_DIGITS,
    VALUE_UNITS,
    _context_before,
    _should_use_er,
    read_value,
)

# 默认开启：修的是已确证的真实缺陷（`~` 被整个丢掉），且命中面窄（两侧必须都是数字）。
ENABLED = os.environ.get("RANGE_NORMALIZE", "1").strip() != "0"

# 区间分隔符统一输出「到」。为什么不保留 `~`：汉字之间的 `~` 没有任何一层再管它
# （TN 实测对 `十~十五` 原样输出），模型面对它行为不可预期。
RANGE_SEPARATOR = "到"

# 波浪号家族。**只收波浪号**，理由见模块 docstring「不碰」第三条。
#   U+007E ~   U+FF5E ～   U+301C 〜   U+223C ∼   U+02DC ˜   U+2053 ⁓
SEP_CHARS = "~～〜∼˜⁓"

# 允许出现在数字两侧、且不改变语义的空白（含不间断空格 —— 从网页复制常见）。
_SPACE = " \t\u00a0"

# 数量级后缀：只认 万/亿/百万/千万（见 docstring「数量级只认…」）。
_MULT_VALUES = {"百": 100, "千": 1000, "万": 10000, "亿": 100000000}
_MULT_PATTERN = r"(?:[百千]?[万亿])"

# 百分号：`5%~10%` / `10~15%`。
_PERCENT_CHARS = "%％"

_NUM_PATTERN = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"

# 左端不允许紧跟在「数字/字母/小数点/千分位逗号/正负号」之后 ——
# `A1~2`、`v1.5~2`、`-5~3` 都不是区间。
_RANGE_PATTERN = re.compile(
    rf"(?<![\dA-Za-z.,+\-−＋])"
    rf"(?P<l>{_NUM_PATTERN})"
    rf"(?P<lmult>{_MULT_PATTERN})?"
    rf"(?P<pad1>[{_SPACE}]*)"
    rf"(?:(?P<lpct>[{_PERCENT_CHARS}])(?P<pad2>[{_SPACE}]*))?"
    rf"[{_SPACE}]*(?P<sep>[{SEP_CHARS}])[{_SPACE}]*"
    rf"(?P<r>{_NUM_PATTERN})"
    rf"(?P<pad3>[{_SPACE}]*)"
    rf"(?:(?P<rpct>[{_PERCENT_CHARS}])|(?P<rmult>{_MULT_PATTERN}))?"
)

# 年份地盘的最小位数（与 `year_norm.MIN_DIGITS` 对齐：3 位起才可能是年份）。
YEAR_MIN_DIGITS = 3

# 兜底：两端**已经是汉字数字**的波浪号也要收敛成「到」。
# 这层专门收前一道工序留下的残局 —— `9:30~10:30` 先被 `time_norm` 换成
# 「九点三十分~十点三十分」，本层主规则（两侧必须是阿拉伯数字）就再也看不到它了；
# 留在汉字之间的 `~` 没有任何一层会管，模型面对它行为不可预期。
# 判据是「波浪号左右各自不远处的文字里含汉字数字」（`分` / `岁` 这类度量字也算数，
# 故不能只看紧邻那个字符），且不跨标点借语境。
# 只认汉字数字：两侧是阿拉伯数字的形态归主规则管，主规则否决过的
# （年份 / 编号 / 分段号码）**不许在这里被"救"回来**。
_CN_NUMERALS = "零〇一二三四五六七八九十百千万亿两"
_CN_NEAR = 8          # 语境窗口：够装下「九点三十分」这种带度量字的数
_CN_BREAK = "。！？；，、:：;!?,\n"
_HAS_TILDE = re.compile(rf"[{SEP_CHARS}]")


def _numeral_near(text: str, *, left: bool) -> bool:
    """波浪号左/右侧不远处的文字里有没有汉字数字（不跨标点借语境）。"""
    if left:
        window = text[-_CN_NEAR:]
        pos = max((window.rfind(s) for s in _CN_BREAK), default=-1)
        if pos >= 0:
            window = window[pos + 1:]
    else:
        window = text[:_CN_NEAR]
        hits = [p for p in (window.find(s) for s in _CN_BREAK) if p >= 0]
        if hits:
            window = window[:min(hits)]
    return any(ch in _CN_NUMERALS for ch in window)


def _replace_residual(text: str) -> str:
    """把「汉字数字 ~ 汉字数字」的波浪号换成「到」，并吃掉两侧空格。"""
    if not _HAS_TILDE.search(text):
        return text
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in SEP_CHARS:
            # 只取必要长度的窗口：`_numeral_near` 本身也只看 ±_CN_NEAR 字，
            # 避免每个波浪号都拼一次长串（单章两万字、多处区间时是无谓的 O(n²)）。
            left = "".join(out[-_CN_NEAR:])
            right = text[i + 1:i + 1 + _CN_NEAR]
            if _numeral_near(left, left=True) and _numeral_near(right, left=False):
                while out and out[-1] in _SPACE:
                    out.pop()
                out.append(RANGE_SEPARATOR)
                i += 1
                while i < len(text) and text[i] in _SPACE:
                    i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _digits_len(raw: str) -> int:
    """整数部分的位数（去掉千分位逗号、小数部分）。"""
    return len(raw.replace(",", "").partition(".")[0])


def _leading_zero(raw: str) -> bool:
    """`007` / `08` 这类带前导 0 的多位数字 ⇒ 多半是编号，不是数值。"""
    int_part = raw.replace(",", "").partition(".")[0]
    return len(int_part) > 1 and int_part.startswith("0")


def _year_territory(text: str, end: int, raw: str) -> bool:
    """这一端是不是「年份」（⇒ 整段区间交给 year_norm / TN，本层不碰）。

    判据：整数位 ≥3 且紧跟「年」。`1550~1850年` / `2011年~2015年` 命中；
    `10~15年`（时长）不命中 —— 两位不可能是年份，该读「十到十五年」。
    """
    if _digits_len(raw) < YEAR_MIN_DIGITS:
        return False
    return text[end:].lstrip(_SPACE).startswith("年")


def _mult_value(raw: str | None) -> int:
    """`None` → 1；`万` → 10000；`百万` → 1000000。"""
    if not raw:
        return 1
    value = 1
    for ch in raw:
        value *= _MULT_VALUES[ch]
    return value


def _bare_four_digit(raw: str) -> bool:
    """纯四位、无小数、无千分位 —— 可能是年份。"""
    return len(raw) == 4 and raw.isdigit()


def _follows_unit(text: str, end: int) -> bool:
    """右端之后（跳过空格）紧接量词/单位。"""
    rest = text[end:].lstrip(_SPACE)
    return any(rest.startswith(u) for u in VALUE_UNITS)


def _chain_follows(text: str, end: int) -> bool:
    """右端之后又是「分隔符 + 数字」⇒ 三段及以上（`1~2~3`），不是「从 A 到 B」。"""
    i = end
    while i < len(text) and text[i] in _SPACE:
        i += 1
    if i >= len(text) or text[i] not in SEP_CHARS:
        return False
    i += 1
    while i < len(text) and text[i] in _SPACE:
        i += 1
    return i < len(text) and text[i].isdigit()


def _scaled_word(raw: str, mult: int, use_er: bool) -> str | None:
    """按数量级放大后再读：`1` ×万 → 一万；`15000` → 一万五千。

    返回 None ⇒ 调用方整体放弃，交回 TN，两种情形：
    - 非整数（`0.0001~0.0002万`）；
    - 放大后超过 `MAX_DIGITS`（`5~10亿` → 亿级要 9 位）。**刻意不扩到亿**：
      `read_value` 只按「万」分段，9 位以上会静默丢高位；而 TN 对 `5~10亿` /
      `1亿~2亿` 实测读得对（五到十亿 / 一亿到两亿），交给它比我们自己造一个
      半吊子的亿级读法更安全。
    """
    if mult == 1:
        return read_value(raw, use_er=use_er)
    value = Decimal(raw.replace(",", "")) * mult
    if value != value.to_integral_value():
        return None
    digits = str(int(value))
    if len(digits) > MAX_DIGITS:  # 放大后越界，`read_value` 会静默丢高位
        return None
    return read_value(digits, use_er=use_er)


def _replace(match: re.Match) -> str:
    text = match.string
    left, right = match.group("l"), match.group("r")

    # ── 否决区（先判，命中即原样返回）──────────────────────────────
    if _digits_len(left) > MAX_DIGITS or _digits_len(right) > MAX_DIGITS:
        return match.group(0)
    if _leading_zero(left) or _leading_zero(right):
        return match.group(0)
    if _year_territory(text, match.end("l") + len(match.group("lmult") or ""), left):
        return match.group(0)
    if _year_territory(text, match.end("r"), right):
        return match.group(0)
    if _chain_follows(text, match.end()):
        return match.group(0)

    percent = bool(match.group("lpct") or match.group("rpct"))
    left_mult = _mult_value(match.group("lmult"))
    right_mult = _mult_value(match.group("rmult"))
    if percent:
        # `5%~10%`：百分号已经把量级说死了，数量级后缀不参与
        left_mult = right_mult = 1
    elif left_mult == 1 or right_mult == 1:
        # 只有一侧写了量级（`1万~2万` 里两侧都有；`1~2万` 只有右侧）⇒ 两端共用
        shared = max(left_mult, right_mult)
        left_mult = right_mult = shared

    # 裸四位数字对、后面又没有量词 ⇒ 可能是年份（`2020~2024`），方向不唯一，不猜
    if not percent and left_mult == 1 and right_mult == 1 \
            and _bare_four_digit(left) and _bare_four_digit(right) \
            and not _follows_unit(text, match.end()):
        return match.group(0)

    after = text[match.end():match.end() + 6].lstrip(_SPACE)
    left_er = _should_use_er(left, None, percent, _context_before(text, match.start("l")), "")
    right_er = _should_use_er(right, None, percent, "", after)

    left_word = _scaled_word(left, left_mult, left_er)
    right_word = _scaled_word(right, right_mult, right_er)
    if left_word is None or right_word is None:
        return match.group(0)

    if percent:
        left_word = "百分之" + left_word
        right_word = "百分之" + right_word
    return f"{left_word}{RANGE_SEPARATOR}{right_word}"


def normalize_ranges(text: str) -> str:
    """把「数字 波浪号 数字」读成「X到Y」；其余原样保留。"""
    if not text:
        return text
    text = _RANGE_PATTERN.sub(_replace, text)
    return _replace_residual(text)


def apply_range_rules(lines: list) -> list:
    """返回「区间已换汉字」的新 lines，**不修改入参**（与 stores.apply_glossary 同风格）。

    未启用（RANGE_NORMALIZE=0）时原样返回，调用方可先判断 ENABLED 省开销。
    """
    if not ENABLED:
        return lines
    out: list = []
    for line in lines:
        if not isinstance(line, dict):
            out.append(line)
            continue
        new_line = dict(line)
        text = new_line.get("text")
        if isinstance(text, str) and text:
            new_line["text"] = normalize_ranges(text)
        out.append(new_line)
    return out
