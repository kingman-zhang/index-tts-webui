#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不发音符号归一化：合成前预处理（2026-10-28）。

## 问题

用户报障：「文本里有 `；（）[ ]` 这类符号时，音频里会听到**很短的一个声音**」。

这些符号人类不会读出来，但 IndexTTS2 的文本前端不会「丢掉」它们，而是
**折成别的字符再送进模型**。`indextts/utils/front.py:16-52` 的 char_rep_map：

| 输入 | char_rep_map 之后 | 进模型的 piece |
|------|------------------|----------------|
| `（` `）` `(` `)` `[` `]` `【` `】` `《` `》` `「` `」` `“` `”` `"` | `'` | `'`（**孤立 token**） |
| `；` `;` `：` `:` `，` `、` | `,` | `▁,`（标准停顿） |
| `。` | `.` | `.` |
| `—` `～` `~` `·` | `-` | `-`（**孤立 token**） |

差别全在最后一列（`webui-backend/data/models/bpe.model`，vocab 12000，
本机实测）：

    '他 说 （ 笑 ） 你 好'  → ▁ 他 ▁ 说 ▁ **'** ▁ 笑 ▁ **'** ▁ 你 ▁ 好
    '他 说 ， 你 好'        → ▁ 他 ▁ 说 **▁,** ▁ 你 ▁ 好

`'` 是**没有 `▁` 词边界、也不表示任何音素**的孤立 piece。模型是自回归
生成语义 token，每个 text token 位置都必须产出音频；遇到 `'` 它无音可依
（中文语料里 `'` 几乎只出现在英文缩写），于是吐出一小段气声/轻音节 ——
听感就是「很短的一个声音」，每个括号各来一下。

注意这一条**不是 unk**（和 `name_punct.py` 里那个中点变体不同）：落点存在、
编码也成功，所以 `--audit` 那类 unk 检查抓不到它，只能靠这份说明或
`TTS_LOG_TOKENS=1` 的 token 转储看出来。

## 策略

在送引擎之前，把这些符号换成模型确定认识的形态：

- **括号 / 方括号（插入语边界）→ `，`**：`余华（作家）说` → `余华，作家，说`。
  保留插入语的停顿感；换成 `'` 则是两个怪音，直接删则是连读。
- **书名号 / 引号（内容标记）→ 删除**：`《盗梦空间》是由` → `盗梦空间是由`。
  它们只标记"这是书名/引用"，本身不表示停顿，连读才对。
- **破折号 `—` `―` → `，`**：char_rep_map 会把它变成 `-`（孤立 piece，有被
  读成「减/杠」的风险），换逗号更稳。
- **分号 / 冒号（全角与半角）→ `，`**：壳里的 char_rep_map 本来也会这么做，
  在这里做是为了**不依赖引擎侧前端版本**（云端平台的前端不由我们管）。
- 随后做清理：折叠连续逗号、删掉句末标点前的逗号、去掉行首行尾多余的逗号。

## 边界（刻意不做的事）

- **`%` `$` `¥` `/` `+` `=` `#` 等一律不动**：它们有 TN 读法（`50%` → 百分之五十、
  `$12.5` → 十二点五美元），删了会把读法一起删掉。它们该由数值层/TN 负责。
- **半角 `'` 不动**：英文缩写 `it's` 靠它，删了会把 `is` 丢掉。
- **`[pause:1.5]` / `<#>` 必须原样保留**：本层跑在 queue_worker 的前处理链里，
  早于 runner 的 `split_by_pauses`。先占位保护、处理完再还原，否则停顿语法
  会被当成方括号删掉。
- **纯符号行**：处理后若变成空串，一律**原样返回**（宁可留着，也不要造出一个
  空段落让 runner 跳过 —— 那会悄悄吃掉一个停顿位）。

## 顺序

放在前处理链**最后**（`number_norm` 之后）：前面的层要靠 `（）`、`「」` 做语境
判断（如 `num_value_norm` 的「第」上下文），先删会让它们看不见原文。

    术语表 → 人名中点 → 年份 → 时间 → 区间 → 数值 → 号码 → **本层**

## 开关

    SYMBOL_NORMALIZE=0     # 关闭本层（缺省=启用）
"""

from __future__ import annotations

import os
import re

# 缺省启用：修的是「符号进模型变怪音」的确证缺陷，不是口味调整。
ENABLED = os.environ.get("SYMBOL_NORMALIZE", "1").strip().lower() not in (
    "", "0", "false", "no", "off",
)

# 规则集版本。**规则改了就要递增**：`/api/version` 的 text_pipeline 只有布尔开关时，
# 「括号→逗号」与「括号→删除」两版都是 true，端点上分辨不出进程跑的是哪一版
# （与 year_norm.MIN_DIGITS / time_norm.MAX_PARTS 报取值域同一个理由）。
#   1 = 2026-10-28 首版：括号→逗号、书名号/引号→删除、破折号→逗号（数字区间除外）、
#       分号/冒号→逗号，外加连续逗号/顿号清理
RULE_VERSION = 1

# 停顿标记必须原样穿过本层（见模块 docstring「边界」）。
# 与 mono_runner.PAUSE_TOKEN_RE / tts-server podcast_engine.PAUSE_PATTERN 同源语义。
_PAUSE_TAG = re.compile(r"\[pause:\s*[0-9]*\.?[0-9]+\s*\]|<#>")
_PAUSE_OPEN, _PAUSE_CLOSE = "\x00P", "\x00"

# 括号类 → 逗号（插入语，保留停顿）
_BRACKET_CHARS = "（）()［］[]【】〔〕〖〗｛｝{}"
# 书名号 / 引号类 → 删除（只标记内容，不表示停顿）
_QUOTE_CHARS = "《》〈〉「」『』“”‘’\u201c\u201d"
# 破折号（U+2014 / U+2015）→ 逗号。**两侧都是数字时不动**：
# `2010—2015` 该读「到」，而 range_norm 的 SEP_CHARS 刻意不含破折号
# （见 app/range_norm.py:72「`-` / `—` / `－` 等连字符」那一节），
# 本层无权替它决定，留着交回 TN 比读成「二零一零，二零一五」好。
_DASH_CHARS = "—―"

_BRACKET_RE = re.compile(f"[{re.escape(_BRACKET_CHARS)}]")
_QUOTE_RE = re.compile(f"[{re.escape(_QUOTE_CHARS)}\"]")
_DASH_RE = re.compile(f"[{re.escape(_DASH_CHARS)}]+")
_SEMICOLON_RE = re.compile(r"[；;：:]")

# 清理：逗号/顿号连写 → 一个逗号（`张三、，2，` 这种删除后的接缝）；
# 逗号挨着句末标点 → 删逗号；行首行尾多余的逗号 → 删
_COMMA_RUN_RE = re.compile(r"[，、]{2,}")
_COMMA_BEFORE_STOP_RE = re.compile(r"[，]\s*(?=[。！？!?…])")
_LEADING_COMMA_RE = re.compile(r"^[\s，、]+")
_TRAILING_COMMA_RE = re.compile(r"[\s，、]+$")
# `苹果 (Apple) 公司` → 括号带着原空格被换成逗号，会留下「空格 + 逗号」。
# 只剖半角空格/制表（不含 \n：不能把段落边界吞掉）。
_SPACE_BEFORE_CJK_COMMA_RE = re.compile(r"[ \t]+(?=，)")

# 英文分支的同类清理（半角逗号 + 空格形态）：`, ,` 折叠、`, .` 去逗号、
# 行首行尾多余的逗号与空格。英文里逗号后面该有一个空格，所以替换时用 ", "。
_HAS_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_EN_COMMA_RUN_RE = re.compile(r",(?:\s*,)+")
_EN_COMMA_BEFORE_STOP_RE = re.compile(r",\s*(?=[.!?…])")
_EN_LEADING_RE = re.compile(r"^[\s,]+")
_EN_TRAILING_RE = re.compile(r"[\s,]+$")
_EN_MULTI_SPACE_RE = re.compile(r"(?<=\S) {2,}")
_EN_SPACE_BEFORE_COMMA_RE = re.compile(r"[ \t]+(?=,)")


def _dash_replacement(match: re.Match, text: str, comma: str) -> str:
    """破折号 → 逗号；数字区间原样保留（见 _DASH_CHARS 上方注释）。"""
    start, end = match.start(), match.end()
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before.isdigit() and after.isdigit():
        return match.group(0)
    return comma


def clean_symbols(text: str) -> str:
    """把不发音的符号换成模型认识的形态（幂等）。

    返回新字符串；入参为 None / 空串时原样返回。

    逗号按**整行语言**选形态：整行没有汉字就走英文分支用 `", "`。理由是
    往英文句子里插一个全角 `，` 会读成什么不由我们决定（模型对混排的偏好未知），
    而 `,` 是英文语料里确定的停顿符。混排（含汉字）一律按中文处理 —— 主体是中文。
    """
    if not text:
        return text

    comma = ", " if not _HAS_CJK_RE.search(text) else "，"

    # ① 先保护停顿标记（`[` `]` `<` `>` 都在待处理字符集里）
    protected: list[str] = []

    def _stash(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"{_PAUSE_OPEN}{len(protected) - 1:04d}{_PAUSE_CLOSE}"

    work = _PAUSE_TAG.sub(_stash, text)

    # ② 符号替换
    work = _BRACKET_RE.sub(comma, work)
    work = _DASH_RE.sub(lambda m: _dash_replacement(m, work, comma), work)
    work = _SEMICOLON_RE.sub(comma, work)
    work = _QUOTE_RE.sub("", work)

    # ③ 清理
    if comma == "，":
        work = _SPACE_BEFORE_CJK_COMMA_RE.sub("", work)
        work = _COMMA_RUN_RE.sub("，", work)
        work = _COMMA_BEFORE_STOP_RE.sub("", work)
        work = _LEADING_COMMA_RE.sub("", work)
        work = _TRAILING_COMMA_RE.sub("", work)
    else:
        work = _EN_SPACE_BEFORE_COMMA_RE.sub("", work)
        work = _EN_COMMA_RUN_RE.sub(",", work)
        work = _EN_COMMA_BEFORE_STOP_RE.sub("", work)
        work = _EN_LEADING_RE.sub("", work)
        work = _EN_TRAILING_RE.sub("", work)
        work = _EN_MULTI_SPACE_RE.sub(" ", work)

    # ④ 还原停顿标记
    def _restore(match: re.Match) -> str:
        index = int(match.group(1))
        return protected[index] if index < len(protected) else match.group(0)

    work = re.sub(rf"{_PAUSE_OPEN}(\d{{4}}){_PAUSE_CLOSE}", _restore, work)

    # ⑤ 纯符号行：清完是空 ⇒ 原样返回（不要造空段落，见 docstring「边界」）
    return work if work.strip() else text


def apply_symbol_rules(lines: list) -> list:
    """返回「符号已归一化」的新 lines，**不修改入参**。

    与 stores.apply_glossary / name_punct.apply_name_separator_rules 同风格：
    逐行取 `text`，非 dict / 无 text 的行原样透传。
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
            new_line["text"] = clean_symbols(text)
        out.append(new_line)
    return out
