"""年份读法归一化：把四位年份改成逐位读法（默认开启，YEAR_NORMALIZE=0 关闭）。

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
2. **独立年份**：`(?<!\\d)(\\d{4})\\s*年` → 逐位读，并吃掉数字与「年」之间的空格。

为什么分隔符要换成「到」：TN 能把 `1550-1850年` 处理成「到」，但前提是**数字还是数字**；
一旦我们先把它换成汉字（`一五五零-一八五零年`），那个 `-` 就再没人管了 ——
实测 TN 对 `一五五零-一八五零年` 原样输出，模型面对汉字之间的半角 `-` 行为不可预期。
统一成「到」语义明确、不会被误读。

不碰
====
- **两位数字**：`30 年房贷` → TN 给「三十 年」✓，本层不介入（`\\d{4}` 下限即可挡住）。
- **三位年份**（`公元850年`）：TN 读「八百五十年」——按年份习惯应读「八五零年」，
  这是 TN 的既有取舍。本层暂不处理，避免范围蔓延（要处理得靠「公元」锚定，
  已记录为待定项）。
- **右端无「年」的连字符**（`2020-2024`）：可能是减法或范围，交回 TN。
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

# 默认开启：这是一条「本来该有、被链路切换弄丢」的规则，且精度很高（只认四位年份）。
ENABLED = os.environ.get("YEAR_NORMALIZE", "1").strip() != "0"

# 年份逐位读用「一」而非「幺」—— 「二零一一年」才对，「二零幺幺年」是错的。
# 注意不能复用 number_norm.read_digits()：那个默认把 1 读成「幺」（为号码场景设计）。
CHINESE_DIGITS = "零一二三四五六七八九"

# 区间分隔符：统一输出「到」。见模块 docstring 里「为什么分隔符要换成到」。
RANGE_SEPARATOR = "到"

# 年份区间：右端必须明确带「年」才触发……但右端带「年」时 TN 自己也能读对
# （`2020-2024年` → 二零二零到二零二四年）。真正需要救的是**带空格**与**～/— 系分隔符**
# 这两种 TN 处理不好的写法，以及左端被按数值读的情况。
_YEAR_RANGE_PATTERN = re.compile(
    r"(?<!\d)(\d{4})\s*(年)?\s*(～|~|至|到|—|-)\s*(\d{4})\s*年"
)
# 独立年份：`\s*` 是关键 —— 它同时负责「吃掉空格」。
_YEAR_PATTERN = re.compile(r"(?<!\d)(\d{4})\s*年")


def year_digits(digits: str) -> str:
    """四位年份 → 逐位汉字：`2011` → `二零一一`。"""
    return "".join(CHINESE_DIGITS[int(d)] for d in digits)


def _replace_year_range(match: re.Match) -> str:
    left = year_digits(match.group(1))
    left_suffix = match.group(2) or ""
    return f"{left}{left_suffix}{RANGE_SEPARATOR}{year_digits(match.group(4))}年"


def _replace_year(match: re.Match) -> str:
    return year_digits(match.group(1)) + "年"


def normalize_years(text: str) -> str:
    """把四位年份改成逐位读法；其余原样保留。"""
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
