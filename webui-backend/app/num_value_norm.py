"""数值读法归一化：把「数值语境」的阿拉伯数字换成汉字（默认开启）。

为什么需要这一层（2026-09-29）
==============================
用户报：`美国自 2005 年至 2007 年的飞行安全度是汽车的 230 倍。` 里的 `230`
被读成了「二三零」。

逐层查过，**各道关没有一道碰它**：

- `number_norm.py`：只处理 ≥4 位，`230` 是 3 位，压根不进候选；且「倍」在它的
  否决表里 —— 双保险不命中；
- `year_norm.py`：只认「年」；
- `name_punct.py`：只管人名中点；
- 引擎适配层：把 `req.text` 原样塞进工作流。

⇒ 数字是**以阿拉伯形态**离开 backend 的。而 IndexTTS 的 bpe 词表（12000）里
**没有阿拉伯数字**（实测 `"230倍"` → `['▁','230','倍']`，`230` 就是 `<unk>`(id=2)），
模型在那个位置只能自由发挥 —— 真机实测读法在「两百三十 / 二三零」之间**摇摆**。
同一个句子里 `2005 年`/`2007 年` 之所以没事，只是因为**年份规则先把它们换成了汉字**。

## 与号码层的关系：正交互补，命中集合不相交

本层不是「重写数字规则」，而是补上号码层的**正对面**：

| | 号码层 `number_norm` | 数值层（本模块） |
|---|---|---|
| 前提 | **没有**单位、**没有**幅度词 | **必须有**单位或幅度词 |
| 靠什么判定 | 号码语境词 / 手机形态 / 带分隔号码 | 右邻单位、左邻幅度词、紧跟 `%` |
| 动作 | 逐位读（`幺幺零`） | 数值读（`一百一十`） |

第①条正好互为反面 ⇒ 两层在同一段文本上的命中集合**交集恒为空**、并集覆盖
「有明确语境」的全部数字。`tests/test_num_value_norm.py` 里有这条属性断言。

顺带补上一个此前**无人管辖**的缺口：`第110位` —— 号码层因 `≥4 位` 不接、
本层此前不存在、云端 TN 不接（3 位短数字卡在缝里）。

## 判定顺序：否决优先，永不猜

1. **形态**：`(?<![0-9A-Za-z.,])` 起、`(?![0-9])` 止 ⇒ 前后紧邻数字/字母/小数点的
   一律不进来（`v2.0`、`SN12345`、`A4` 天然出局）。
2. **否决（优先于命中）**：数字两侧出现 `- – — － ~ ～ / : . ,` 且另一侧还是数字
   ⇒ 不是数值语境。这一条同时挡住日期 `2026-09-28`、时间 `3:30`、区间 `100-200`、
   版本 `1.5.2`、带分隔号码 `400-123-4567`（那些要么归号码层，要么交回 TN）。
3. **命中**：右邻（跳过空格后）是单位/量词，或左语境窗口末尾是幅度词，或紧跟 `%`/`％`。
4. 其余一律原样保留，交回 TN。

## 转换规则：抄 TN 的答案，不自己发明

`tests/test_num_value_norm.py` 里有一张 **wetext 基准表**（`110元`→一百一十元、
`105`→一百零五、`1200米`→一千二百米、`2000次`→两千次、`30%`→百分之三十…），
本层的输出与之逐字比对。

**但「两」的用法刻意不复刻**：TN 自带 lexicon，`2元/2个/2米`→两、`2倍/2年/2月/2号`→二，
按词不按位。本层用一套自洽规则 —— **最高位是 2 且落在百/千位、或独立的一个 2 ⇒ 两**
（`2`→两、`200`→两百、`2000`→两千、`2200`→两千二百、`1200`→一千二百；`20`→二十）。
比对时做 **「两 ≡ 二」等价化**，否则会假失败。

## 与 year_norm 的顺序耦合（有意为之）

前处理顺序固定为 `glossary → name_punct → year_norm → 本层 → number_norm`：

- `2011年` / `公元850年` 由 `year_norm` **先**换成汉字，本层看不到，行为不受影响；
- `year_norm` 刻意**否决**的形态（`距今850年` 这类时长）会落到本层，由「年」命中
  ⇒ 读「八百五十」—— 正是时长该有的读法。两层在此**天然接续**，不必再加词表。
- ⚠️ 反过来说：本层**假定 year_norm 先跑过**。若把 `YEAR_NORMALIZE=0` 关掉，
  `2011年` 会落到本层被读成「两千零一十一年」（年份读法丢失）。这是刻意的顺序耦合，
  四位数字紧邻「年」时本层另有一道否决兜着（见 `_four_digit_year`），
  但仍不建议关掉 year_norm。

## 版本可观测

`/api/version` 报 `text_pipeline.num_value_normalize` 与 `num_value_max_digits`。
**布尔开关证明不了版本**（2026-09-29 已踩过一次：`year_norm_enabled` 在两版都是 true），
所以报出取值域；部署自检断言 `num_value_max_digits >= 8`。

启用条件：`NUM_VALUE_NORMALIZE=0` 关闭（默认开）。改 `.env` 后必须重启 backend ——
config 只在启动时读。

范围（v1，用户 2026-09-29 定）：**整数 / 小数 / 百分号 / 千分位**。
区间（`100-200人`）不做 —— 要处理两头加换「到」，属第二条规则，见否决第 2 条。
"""

from __future__ import annotations

import os
import re

# 复用号码层的三样东西。**刻意 import 而不是复制**：
# `_context_before` 必须与号码层是同一个函数，否则两层的「左语境窗口」会出现
# 细微差异，「命中集合不相交」这条性质就不再成立。
from .number_norm import NUMERIC_PREFIXES, UNITS, _context_before

# 默认开启：修的是已确证的真实缺陷（阿拉伯数字进模型 = unk），
# 且命中面窄（必须有单位或幅度词），精度高于号码层。NUM_VALUE_NORMALIZE=0 关闭。
ENABLED = os.environ.get("NUM_VALUE_NORMALIZE", "1").strip() != "0"

# 受理的最大位数（取值域，供 /api/version 与部署自检判断「是哪一版」）。
# 超过这个长度的数字串不受理，直接交回号码层 / TN（`快递单号1234567890` 就靠这条
# 落到号码层手里）。为什么是 8：8 位以内只需「万」分段，不必引入「亿」；
# 再长基本是号码，归号码层更合适。
MAX_DIGITS = 8

_CN_DIGITS = "零一二三四五六七八九"
_SMALL_UNITS = ("", "十", "百", "千")

# 本层的单位表 = 号码层的 UNITS + 补录。补录理由：`UNITS` 是为「号码 vs 数值」的
# 否决设计的，只收最容易被误判成号码的那些；做正向命中时缺一批高频量词。
# 最主要的是「位」—— `第110位` 就卡在「位」不在表里、而号码层又不接 3 位。
VALUE_EXTRA_UNITS = (
    "位", "届", "期", "款", "条", "项", "种", "类", "张", "把", "支", "间",
    "座", "家", "辆", "门", "股", "份", "笔", "级", "档", "例", "起", "场", "局",
)
VALUE_UNITS = tuple(dict.fromkeys(UNITS + VALUE_EXTRA_UNITS))

# 两侧都是数字时才构成否决的分隔符（日期 / 时间 / 区间 / 版本 / 带分隔号码）。
_RUN_SEPS = "-–—－~～/:.,"
# 正负号：`-5度` → 负五度、`+5度` → 正五度（与 TN 一致）。
_SIGN_CHARS = "-−+＋"
_SIGN_WORD = {"-": "负", "−": "负", "+": "正", "＋": "正"}

# 候选数字。两分支：带千分位 / 不带。`(\s*)` 负责吃掉「数字 ↔ 单位」之间的空格
# （用户从网页粘贴的文本经常带空格，TN 正是被这个空格打挂的）。
_CANDIDATE = re.compile(
    r"(?<![0-9A-Za-z.,])([-−+＋])?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(\s*)([%％]?)(?![0-9])"
)


def _read_below_10000(n: int, top: bool = False) -> str:
    """0~9999 的数值读法。`top=True` 表示这是整个数的最高段（决定单个 2 读「两」）。"""
    parts: list[str] = []
    zero_pending = False
    for pos in (3, 2, 1, 0):
        d = (n // (10 ** pos)) % 10
        if d == 0:
            if parts:
                zero_pending = True
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        if d == 2 and not parts and top and (pos in (3, 2) or n == 2):
            # 最高段且最高位是 2，落在百/千位 → 「两」（200 → 两百、2000 → 两千、
            # 2200 → 两千二百）；独立的一个 2（`2元`/`第2名`）也读「两」。
            # 其余一律「二」：十位上的 2（20 → 二十）、**非最高段**的 2
            # （22000 → 两万二千，不是「两万两千」）、非最高位的 2
            # （1200 → 一千二百）。
            parts.append("两")
        elif d == 1 and pos == 1 and not parts:
            pass  # 十位前导 1 省「一」：10 → 十、15 → 十五（115 仍是 一百一十五）
        else:
            parts.append(_CN_DIGITS[d])
        parts.append(_SMALL_UNITS[pos])
    return "".join(parts)


def _read_int(digits: str) -> str:
    """整数 → 汉字（千分位会被忽略）。最高 8 位，只用到「万」分段。"""
    n = int(digits.replace(",", ""))
    if n == 0:
        return "零"
    wan, ge = divmod(n, 10000)
    if wan == 0:
        return _read_below_10000(ge, top=True)
    text = _read_below_10000(wan, top=True) + "万"
    if ge == 0:
        return text
    if ge < 1000:
        text += "零"  # 10005 → 一万零五；25000 → 两万五千（不补零）
    return text + _read_below_10000(ge)


def read_value(raw: str) -> str:
    """数值串 → 汉字：`230` → `两百三十`、`3.5` → `三点五`、`1,234` → `一千二百三十四`。"""
    s = raw.replace(",", "")
    if "." in s:
        int_part, _, frac = s.partition(".")
        return _read_int(int_part) + "点" + "".join(_CN_DIGITS[int(c)] for c in frac)
    return _read_int(s)


def _nonspace_before(text: str, i: int) -> str:
    """位置 i 左边第一个非空白字符（没有则空串）。"""
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    return text[j] if j >= 0 else ""


def _nonspace_after(text: str, i: int) -> str:
    """位置 i 右边第一个非空白字符（没有则空串）。"""
    j = i
    while j < len(text) and text[j] in " \t":
        j += 1
    return text[j] if j < len(text) else ""


def _in_separated_run(text: str, start: int, end: int) -> bool:
    """数字两侧是否有「夹在数字之间的分隔符」（日期/时间/区间/版本/带分隔号码）。

    注意这里的 `prev and ...` 判空：`"" in "-–—"` 恒为 True（空串是任何字符串的
    子串），漏掉这个守卫会让句首/句末的数字被静默否决 —— year_norm 踩过同一个坑。

    空格也算一种「分隔符」，但只在跨过空格后还是数字时才算：`1 234元` 是写坏了的
    千分位，两个数都不该当数值读（TN 对它也只读对一半：「一 两百三十四元」）；
    而 `是汽车的 230 倍` 里那个空格后面是汉字，不受影响。
    """
    prev = text[start - 1] if start > 0 else ""
    prev2 = text[start - 2] if start > 1 else ""
    nxt = text[end] if end < len(text) else ""
    nxt2 = text[end + 1] if end + 1 < len(text) else ""
    if prev and prev in _RUN_SEPS and prev2.isdigit():
        return True
    if nxt and nxt in _RUN_SEPS and nxt2.isdigit():
        return True
    if prev in " \t" and _nonspace_before(text, start).isdigit():
        return True
    if nxt in " \t" and _nonspace_after(text, end).isdigit():
        return True
    return False


def _four_digit_year(text: str, start: int, end: int) -> bool:
    """四位数字紧邻「年」⇒ 交回 TN / year_norm，本层不当数值读。

    正常顺序下这种形态到不了本层（year_norm 先换成汉字了）。这道否决是给
    `YEAR_NORMALIZE=0` 的配置兜底：那时若本层硬读成「两千零一十一年」，
    比留给 TN 更糟（TN 对 `2011年` 能读对）。三位数字不受此限 ——
    `距今850年` 正是靠本层兜底读成「八百五十年」。
    """
    after = text[end:end + 2].lstrip(" \t")
    return after.startswith("年") and len(text[start:end].replace(",", "")) == 4


def _replace(match: re.Match) -> str:
    sign = match.group(1)
    raw = match.group(2)
    percent = match.group(4)

    num_start = match.start() + (len(sign) if sign else 0)
    num_end = num_start + len(raw)
    text = match.string

    # 位数上限。**必须在替换前挡掉**：超过 MAX_DIGITS 的数字串 `_read_int` 只按
    # 「万」分段，多出来的高位会被静默丢弃（`999999999` → 九千九百九十九万九千九百九十九，
    # 即 99999999 —— 少了一个数量级），比不处理糟得多。超过上限的交回号码层 / TN。
    if len(raw.replace(",", "").partition(".")[0]) > MAX_DIGITS:
        return match.group(0)
    if _in_separated_run(text, num_start, num_end):
        return match.group(0)
    if _four_digit_year(text, num_start, num_end):
        return match.group(0)

    # 命中：紧跟百分号（百分号要前置成「百分之」，不是「三十%」）；
    # 或右邻单位/量词；或左语境窗口末尾是幅度词。
    if percent:
        word = "百分之" + read_value(raw)
    elif any(text[num_end:num_end + 4].lstrip(" \t").startswith(u) for u in VALUE_UNITS):
        word = read_value(raw)
    elif any(_context_before(text, num_start).endswith(p) for p in NUMERIC_PREFIXES):
        word = read_value(raw)
    else:
        return match.group(0)

    return (_SIGN_WORD.get(sign, "") if sign else "") + word


def normalize_values(text: str) -> str:
    """把数值语境的阿拉伯数字换成汉字；其余原样保留。"""
    if not text:
        return text
    return _CANDIDATE.sub(_replace, text)


def apply_value_rules(lines: list) -> list:
    """返回「数值已换汉字」的新 lines，**不修改入参**（与 stores.apply_glossary 同风格）。

    未启用（NUM_VALUE_NORMALIZE=0）时原样返回，调用方可先判断 ENABLED 省开销。
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
            new_line["text"] = normalize_values(text)
        out.append(new_line)
    return out
