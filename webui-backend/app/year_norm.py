"""年份读法归一化：把年份改成逐位读法（默认开启，YEAR_NORMALIZE=0 关闭）。

覆盖四位（`2011 年` → `二零一一年`）与三位（`公元850年` → `公元八五零年`，带语境否决）。

为什么需要在 backend 单独做一层（2026-09-29）
============================================
这条规则**本来就有**，但只存在于 `tts-server/podcast_engine.py`
（`_YEAR_PATTERN` / `_replace_year` / `_normalize_reading_text`），
由 `_sanitize_text()` 在**本地 GPU 引擎**的合成链路上调用。

而线上是 Docker 部署 + 云端引擎（`TTS_ENGINE_PREFERRED` 指向 302.ai / autodl.art），
`queue_worker._execute_task` 里那行注释写得很直白：

    配音/播客模式：不进 tts-server 播客引擎，走引擎适配层在 backend 进程内合成

⇒ tts-server 的那条年份规则**完全不在链路上**。这就是「以前修好了、现在又坏了」的
原因：不是代码丢了，是链路换了，规则留在了被绕过的那一侧。

为什么不能指望 TN（它其实会读年份）
====================================
IndexTTS 自带 TN 确实有年份规则，但它要求数字与「年」**紧邻**。实测
（wetext 替身，2026-09-29，`lang='zh', operator='tn'`）：

    2011年            -> 二零一一年              ✓
    2011 年           -> 两千零一十一 年          ✗  一个空格就退化
    2011年3月11日      -> 二零一一年三月十一日      ✓
    2011 年 3 月 11 日 -> 两千零一十一 年 三月 十一日 ✗
    1550～1850年间     -> 一千五百五十～一八五零年间  ✗  左端按数值读
    2011年度报告       -> 二零一一年度报告          ✓（「年度」不是反例）

而从网页/文档/PPT 粘贴的文本**经常带空格**，不能指望 TN 兜住。
更要紧的是：云引擎的 TN 是**服务端实现，我们控制不了**，行为不可预期。
所以在本层直接输出汉字 —— 汉字对 TN 透明（实测 `二零一一年` 进 TN 原样输出），
结果与跑哪个引擎无关、可预期、可测试。

规则（高精度，宁可少改）
========================
1. **年份区间**：`1550～1850年间` / `1550年至1850年` / `1550 ～ 1850 年间`
   → 两端都逐位读，**分隔符统一为「到」**。
   只在右端明确带「年」时触发，避免把普通减法误当区间。
2. **独立年份**：`(?<!\\d)(\\d{3,4})\\s*年` → 逐位读，并吃掉数字与「年」之间的空格。
   四位一律改；**三位**（`公元850年` → `公元八五零年`，2026-09-29 起）加一道
   否决，见下。

为什么分隔符要换成「到」：TN 能把 `1550-1850年` 处理成「到」，但前提是**数字还是数字**；
一旦我们先把它换成汉字（`一五五零-一八五零年`），那个 `-` 就再没人管了 ——
实测 TN 对 `一五五零-一八五零年` 原样输出，模型面对汉字之间的半角 `-` 行为不可预期。
统一成「到」语义明确、不会被误读。

三位年份为什么要否决（2026-09-29）
==================================
四位年份几乎不可能是「时长」（`2011年` 就是年份），所以四位无条件改。三位不一样：
`850年` 既可能是公元 850 年，也可能是「距今 850 年」这类时长。实测 TN 对这两种
一律给「八百五十年」——年份该读「八五零年」、时长该读「八百五十年」，它只能选一个。

判据取**紧邻数字之前的词**（窗口 4 字）：
- 时长/序数语境（`距今` `历时` `长达` `已有` `第` `共` `约` …）→ 是时长或序数，**不改**，
  交回 TN 读「八百五十年」；
- 其余（含 `公元` / `公元前` / 句首裸写）→ 是年份，逐位读。

**只对三位生效**：四位在时长语境下也照样改（`距今2011年` 是病句，不值得为它引入
分支），这样四位的行为一个字节都没变。

三位不碰的另一种形态是**破折号前的三位**：`100-200年` 里的 `200年` 若被改，
`100-200年`（时长区间，该读「一百到两百年」）就毁了。故三位数字前面是
`-–—－～~` 之一时跳过。注意 `从850年到950年` 不受影响 —— 那里的三位**前面是「到」**
而不是数字之间的分隔符，两端都照改。

不碰
====
- **两位数字**：`30 年房贷` → TN 给「三十 年」✓，本层不介入（`\\d{3}` 下限即可挡住）。
- **纯三位时长**：`距今850年` / `历时850年` / `第850年`（见上「三位年份为什么要否决」）。
- **数字间的三位**：`100-200年`（见上）。
- **右端无「年」的连字符**（`2020-2024`）：可能是减法或范围，交回 TN。
- **两端都三位的区间**（`850～950年`）：右端不带四位 ⇒ 不触发区间规则，
  左三位前面又是 `～`（排除符）⇒ 也不触发独立规则，原样交回 TN。
  这是刻意的：三位双端区间与「时长区间」在字面上无法区分。
- **`2011年度报告`**：已实测 TN 读「二零一一年度报告」，这是**正确**读法
  （「2023年度报告」就该读「二零二三年度报告」），故不加特例 —— 加了反而会让
  带空格与不带空格两种写法读法不一致。

落点：`queue_worker._execute_task` 作用于**合成副本**（与术语表/中点归一化同一位置），
不回写 `task["lines"]`。顺序：glossary → name_punct → year_norm → number_norm。

启用条件：`YEAR_NORMALIZE=1`（默认）。改 `.env` 后必须重启 backend —— config 只在启动时读。
"""

from __future__ import annotations

import os
import re

# 默认开启：这是一条「本来该有、被链路切换弄丢」的规则，且精度高
# （只认 3~4 位 + 「年」，三位另有时长语境否决）。
ENABLED = os.environ.get("YEAR_NORMALIZE", "1").strip() != "0"

# 年份逐位读用「一」而非「幺」—— 「二零一一年」才对，「二零幺幺年」是错的。
# 注意不能复用 number_norm.read_digits()：那个默认把 1 读成「幺」（为号码场景设计）。
CHINESE_DIGITS = "零一二三四五六七八九"

# 区间分隔符：统一输出「到」。见模块 docstring 里「为什么分隔符要换成到」。
RANGE_SEPARATOR = "到"

# 年份区间：右端必须明确带「年」才触发……但右端带「年」时 TN 自己也能读对
# （`2020-2024年` → 二零二零到二零二四年）。真正需要救的是**带空格**与**～/— 系分隔符**
# 这两种 TN 处理不好的写法，以及左端被按数值读的情况。
# 右端限定 4 位：`100-200年` 这种双三位更像「时长区间」（一百到两百年），不能当年份。
_YEAR_RANGE_PATTERN = re.compile(
    r"(?<!\d)(\d{3,4})\s*(年)?\s*(～|~|至|到|—|-)\s*(\d{4})\s*年"
)
# 独立年份：`\s*` 是关键 —— 它同时负责「吃掉空格」。
_YEAR_PATTERN = re.compile(r"(?<!\d)(\d{3,4})\s*年")

# ── 三位年份的否决（只对三位生效，四位行为不变）────────────────────────
# 时长/序数语境：这些词紧邻数字右侧（窗口内）⇒ 是「时长」或「序数」不是年份。
# `距今850年` / `历时850年` / `第850年` → 交回 TN 读「八百五十年」。
THREE_DIGIT_VETO = (
    "距今", "历时", "长达", "历经", "已有", "用了", "持续", "经过", "整整",
    "超过", "不到", "相隔", "相差", "每隔", "阔别", "等了", "约", "近", "共",
    "仅", "只有", "第",
)
VETO_WINDOW = 4
# 数字间分隔符：三位数字前面是它 ⇒ 该串更像时长区间（`100-200年`），跳过。
# 不含「到」「至」——`从850年到950年` 里的三位前面正是「到」，那两个都要改。
RANGE_SEPARATORS = "-–—－～~"


def year_digits(digits: str) -> str:
    """年份 → 逐位汉字：`2011` → `二零一一`、`850` → `八五零`。"""
    return "".join(CHINESE_DIGITS[int(d)] for d in digits)


def _three_digit_rejected(text: str, start: int) -> bool:
    """三位的否决判定：时长/序数语境，或前面是数字间的分隔符。"""
    # 注意先判空：`"" in RANGE_SEPARATORS` 恒为 True（空串是任何字符串的子串），
    # 句首的三位会被这条静默吞掉。
    prev = text[start - 1] if start > 0 else ""
    if prev and prev in RANGE_SEPARATORS:
        return True
    before = text[max(0, start - VETO_WINDOW):start]
    return any(before.endswith(w) for w in THREE_DIGIT_VETO)


def _replace_year_range(match: re.Match) -> str:
    left = year_digits(match.group(1))
    left_suffix = match.group(2) or ""
    return f"{left}{left_suffix}{RANGE_SEPARATOR}{year_digits(match.group(4))}年"


def _replace_year(match: re.Match) -> str:
    digits = match.group(1)
    # 只对三位否决；四位一律改（`2011年` 不可能是时长）。
    if len(digits) == 3 and _three_digit_rejected(match.string, match.start()):
        return match.group(0)
    return year_digits(digits) + "年"


def normalize_years(text: str) -> str:
    """把年份改成逐位读法；其余原样保留。"""
    if not text:
        return text
    text = _YEAR_RANGE_PATTERN.sub(_replace_year_range, text)
    return _YEAR_PATTERN.sub(_replace_year, text)


def apply_year_rules(lines: list) -> list:
    """返回「年份已逐位读」的新 lines，**不修改入参**（与 stores.apply_glossary 同风格）。

    未启用（YEAR_NORMALIZE=0）时原样返回，调用方可先判断 ENABLED 省开销。
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
            new_line["text"] = normalize_years(text)
        out.append(new_line)
    return out
