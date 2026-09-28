#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外国人名分隔号（中点）归一化：合成前预处理。

## 问题（2026-09-28 定位，token 级证据）

中译外国人名的间隔号在不同输入法/来源下会写成**不同的 Unicode 字符**，
而 IndexTTS 的 `front.py` 只在 `char_rep_map` 里认 `·`(U+00B7) 这一个：

    char_rep_map = {..., "·": "-", ...}      # front.py:25，只此一条

于是同一句「保罗·萨特」，只因为中间那个点长得不一样，走向完全不同：

| 输入分隔符   | `front.normalize()` 后      | 进模型的 token | 结果        |
|-------------|----------------------------|---------------|------------|
| `·` U+00B7  | `保罗-萨特`                 | `'-'`         | 在词表 ✅    |
| `・` U+30FB  | `保罗・萨特`（原样保留）      | `'・'`        | **unk ❌**  |
| `•` U+2022  | 原样保留                     | `'•'`         | **unk ❌**  |
| `‧` U+2027  | 原样保留                     | `'‧'`         | **unk ❌**  |

`unk_token_id = 2`（词表 12000）—— 模型在这个位置拿到的**不是任何已知音素**，
吐出什么是完全自由的。用户报的「保罗・萨特 中间多出一个怪音」即源于此；
而标准中点 `·` 因为被换成了 `-`，反而躲过了 unk。

复现方式：
  - token 级：skill `indextts-text-frontend-repro`（本地跑文本前端，不需要 torch）
  - 听感级：`tools/probe_name_middot.py`（真机多引擎、多变体、重复采样试听）

## 策略

把**所有变体**收敛到同一个目标形态，消除 unk 分叉。目标由 `NAME_PUNCT_TARGET` 选：

- `drop`（默认）：删掉分隔符 → 「保罗萨特」。
  本地实测：**删除与空格产出的 token 序列完全相同**（`tokenize_by_CJK_char`
  本就会在每个汉字之间插入 `▁`），所以删除不损失任何韵律信息，只是最省事。
- `space`：换成半角空格。token 序列与 `drop` 一致，区别只在"原文里看得见"。
- `dot`：换成标准 `·`(U+00B7)，交回 `front.py` 再变 `-`（多塞一个 `-` token）。
- `pause`：换成顿号 → 本地变 `,`，**会产生明显停顿**，想强调名字断读时用。

## 边界（刻意不做的事）

只处理**两侧都是汉字或字母**的分隔符。因此行首项目符号（`• 第一点`）、
纯符号串、行尾悬挂的点一律不动 —— 避免把列表标记误当人名分隔号。

句式标点类字符（`．`U+FF0E、`﹒`U+FE52、`․`U+2024）**刻意不收**：
它们更常作句号用，「结束了．下一段」这种会被误伤。

## 开关

改 `.env` 后需重启 backend（config 仅启动时读）：

    NAME_PUNCT_NORMALIZE=0      # 关闭本模块（缺省=启用）
    NAME_PUNCT_TARGET=pause     # drop(默认) / space / dot / pause
"""

from __future__ import annotations

import os
import re

# 缺省启用：这一项修的是「字符进词表就变 unk」的确证缺陷，不是口味调整。
# 关掉只需 NAME_PUNCT_NORMALIZE=0。
ENABLED = os.environ.get("NAME_PUNCT_NORMALIZE", "1").strip().lower() not in (
    "", "0", "false", "no", "off",
)

TARGET = os.environ.get("NAME_PUNCT_TARGET", "drop").strip().lower() or "drop"

# 「人名分隔号」的各种 Unicode 变体（同一视觉符号的不同码位）
#   00B7 · 中文间隔号     0387 · 希腊分号点    16EB ᛫ 卢恩单标点
#   2022 • 项目符号       2027 ‧ 连字点      2219 ∙ 项目符号运算符
#   22C5 ⋅ 点运算符       2E31 ⸱ 词分隔中点    30FB ・ 片假名中点
#   FF65 ･ 半角片假名中点
NAME_SEPARATORS = "\u00b7\u0387\u16eb\u2022\u2027\u2219\u22c5\u2e31\u30fb\uff65"

TARGETS: dict[str, str] = {
    "drop": "",
    "space": " ",
    "dot": "\u00b7",
    "pause": "\u3001",
}

# 两侧必须是「汉字 / 字母」。（lookbehind 必须定宽，故写成单字符类）
_WORD = r"[\u3400-\u4dbf\u4e00-\u9fffA-Za-z]"
_SEP_RE = re.compile(rf"(?<={_WORD})\s*[{re.escape(NAME_SEPARATORS)}]\s*(?={_WORD})")


def normalize_name_separators(text: str, target: str | None = None) -> str:
    """把夹在文字中间的人名分隔号统一成目标形态。

    只改「汉字/字母 + 分隔号 + 汉字/字母」这种形态；其余原样返回。
    """
    if not text:
        return text
    key = (target or TARGET).strip().lower()
    if key not in TARGETS:
        raise ValueError(f"未知的 NAME_PUNCT_TARGET: {key!r}（可选：{', '.join(TARGETS)}）")
    return _SEP_RE.sub(TARGETS[key], text)


def apply_name_separator_rules(lines: list) -> list:
    """返回「分隔号已归一化」的新 lines，**不修改入参**（与 stores.apply_glossary 同风格）。

    未启用（NAME_PUNCT_NORMALIZE=0）时原样返回，调用方可以先判断 ENABLED 省开销。
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
            new_line["text"] = normalize_name_separators(text)
        out.append(new_line)
    return out
