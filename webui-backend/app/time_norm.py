"""时间读法归一化：把 `时:分` 改成中文读法（默认开启，TIME_NORMALIZE=0 关闭）。

覆盖 `12:30` → `十二点三十分`、`9:05` → `九点零五分`、`8:00` → `八点整`。

为什么需要在 backend 单独做一层（2026-09-30）
============================================
这条规则**本来就有**，但只存在于 `tts-server/podcast_engine.py`
（`_TIME_PATTERN` / `_replace_time` / `_chinese_hour`），由 `_sanitize_text()`
在**本地 GPU 引擎**的合成链路上调用。

而云引擎（302.ai / SiliconFlow / autodl.art）**不经过 tts-server**：
`queue_worker._execute_task` 里那行注释写得很直白 ——

    配音/播客模式：不进 tts-server 播客引擎，走引擎适配层在 backend 进程内合成

于是走云引擎时，`12:30` 会以阿拉伯形态进模型，读法完全不可控：冒号可能被读成
「比」、数字可能读成「一二比三零」，或者干脆被 TN 折成别的形态。这与
「阿拉伯数字不在 BPE 词表里」是同一类问题（见 `app/num_value_norm.py` 顶部）。

⇒ 规则必须提到 backend，读法才与跑哪个引擎无关。与 `year_norm.py` 是同一个
迁移故事：**不是代码丢了，是链路换了，规则留在了被绕过的那一侧**。

为什么不指望 TN（2026-09-30 wetext 替身实测）
==============================================
TN 对时间**大部分时候读得对**，缺口是具体的两类，不是"完全不管"：

    12:30          ->  十二点三十分              ✓
    会议定在12:30开始 ->  会议定在十二点三十分开始     ✓
    下午 3:16 开会   ->  下午 三点十六分开会         ✓
    8:00           ->  八点                      ✓（少了「整」）
    12 : 30        ->  十二比三十                 ✗  冒号两侧带空格 ⇒ 退化成比例
    0:30           ->  零比三十                   ✗  小时为 0 ⇒ 退化成比例
    0:00           ->  零比零零                   ✗  同上

「带空格就退化」与 `year_norm` 撞的是**同一个坑**（`2011 年` → 两千零一十一 年）——
从网页/文档/PPT 粘贴的文本经常带空格。

更要紧的是：**云引擎的 TN 是服务端实现**，302.ai / SiliconFlow / autodl.art 各自
不同，我们既控制不了也测不到（本地这台跑的是 wetext，不代表线上会怎样）。
而输出汉字对 TN 是**透明的**（实测汉字进 TN 原样输出）。所以在本层直接吐汉字：
结果可预期、可测试、与跑哪个引擎无关。

规则（高精度，宁可少改）
========================
`(?<![\\d:：])(\\d{1,2})\\s*[:：]\\s*(\\d{2})(?![\\d:：])`

- 分钟**固定两位**是这条正则的关键：`3:2`（比分）、`16:9`（比例）、`1:2`（版本号）
  都不会命中。
- 两个否定环同时挡**数字与冒号**：`123:45` / `12:345` 这类更长数字串不命中，
  `12:30:45`（时:分:秒）与 `1:12:30` 也**整串**不命中 ⇒ 原样交回 TN
  （TN 实测把 `12:30:45` 读成「十二点三十分四十五秒」，比本层自己截两段好）。
  只挡数字是不够的：那样 `12:30:45` 会被截成「十二点三十分:45」，留下一个孤立
  冒号 ⇒ 冒号仍可能被读成「比」，比不做更糟 —— 旧 tts-server 正则正是这个毛病。
- 冒号两侧允许空格（`12 : 30`），空格被吃掉 —— 从网页粘贴的文本经常带空格。
- 小时必须 0~23、分钟必须 0~59，否则原样返回交回 TN（`99:99` 不是时间）。
  `24:00` 也因此不碰：它有「二十四点」这种合法读法，交给 TN 更合适。
- 读法：`8:00` → 八点整；`9:05` → 九点零五分（补零）；`12:30` → 十二点三十分。
  小时与分钟共用**同一个** 0~99 中文读法函数（`10`→十、`15`→十五、`20`→二十）。

不碰
====
- **分钟一位**：`3:2` / `16:9` / `1:2`（正则要求两位）。
- **三段式**：`12:30:45`（时:分:秒）—— 整串不命中，原样交回 TN（它读得对）。
  `MAX_PARTS` 目前是 2；若将来要自己读秒，改它并让 /api/version 报出来。
- **超范围**：`99:99`、`24:00`。
- **更长数字串**：`123:45`、`12:345`。

必须与 tts-server 的 `_replace_time` 保持等价（重要）
====================================================
音色**试听**路径（`routes/voices.py` → tts-server `/api/synthesize`）**不经过本链**
（试听是即时的，不建队列任务、不走 `queue_worker` 前处理）。所以 tts-server 侧
**必须保留**它自己的时间规则 —— 两处规则若不同，用户会听到「试听对、正式合成不对」
（或反之），这种不一致比规则不完善更糟。

⇒ 两边刻意保持同一套读法，改动必须同步；各自的测试都要覆盖。
`tts-server/tests/test_time_rule.py` 与 `tests/test_time_norm.py` 里的用例表逐条对应。

**本次相对 tts-server 旧实现的行为变更**：分钟 ≥ 10 由**逐位读**（`12:30` →
「十二点三零分」）改为**规范读法**（「十二点三十分」）。旧实现只对 `minute < 10`
补零读得规范（`12:05` → 「十二点零五分」），≥10 则是逐位拼接，`"三零"` 这种读法
在配音稿里很出戏；小时部分本来就是规范读法（`20` → 「二十」），分钟只是漏了同一
处理。tts-server 侧已同步改（见 `podcast_engine._chinese_under_100`）。

已知取舍（与 tts-server 一致，刻意不加额外否决）
===============================================
`3:16` 这种「经文/章节引用」会被读成「三点十六分」。加一条左语境否决（前有
`第`/`章`/`节`/`卷`）能挡住它，但**试听路径不会跟着变**（见上），于是试听与合成
又会不一致 —— 用「偶发误读」换「稳定不一致」不划算。故不加，维持两边等价。

落点：`queue_worker._execute_task` 作用于**合成副本**（与术语表/年份同一位置），
不回写 `task["lines"]`。顺序：glossary → name_punct → year_norm → time_norm
→ num_value_norm → number_norm。

为什么排在这个位置：`num_value_norm` 的否决表里「两侧都是数字的分隔符（日期/时间/
区间/版本）」本来就把 `12:30` 列为**否决**（含 `:`），所以它不会与号码层抢；
本层先把它换掉，后面两层就再也看不到那串阿拉伯数字。放在 `year_norm` 之后是因为
年份规则只认「数字 + 年」，两者命中集合不相交。

启用条件：`TIME_NORMALIZE=1`（默认）。改 `.env` 后必须重启 backend ——
config 只在启动时读。

版本可观测
==========
`/api/version` 报 `text_pipeline.time_norm_enabled` 与 **`time_norm_max_parts`**
（取自本模块的 `MAX_PARTS`）。为什么报取值域而不是只报开关：这正是
`year_norm` 踩过的坑 —— 只有布尔开关时，「只做时:分」与「将来做时:分:秒」两版
都是 `true`，**从端点上分辨不出来**，只能去读 git 史。加一个数字字段就一眼可判。
"""

from __future__ import annotations

import os
import re

# 默认开启：修的是已确证的真实缺口（时间只在本地引擎有规则，云引擎完全缺失），
# 且命中面窄（必须 `\d{1,2}:\d{2}` 且时/分都在合法范围内）。
ENABLED = os.environ.get("TIME_NORMALIZE", "1").strip() != "0"

# 逐位读用「一」而非「幺」——「十二点」「零五分」才对，「幺二点」是错的。
CHINESE_DIGITS = "零一二三四五六七八九"

# 本层受理的「段数」：2 = 只处理 时:分。将来支持 时:分:秒（`12:30:45`）时变成 3。
# **这是给 /api/version 判版本用的取值域**（见模块 docstring 末节）。
MAX_PARTS = 2

# 时/分的合法取值范围；超出即认为不是时间，原样交回 TN。
HOUR_MIN, HOUR_MAX = 0, 23
MINUTE_MIN, MINUTE_MAX = 0, 59

# 时:分。分钟固定两位 ⇒ 比分/比例/版本号（`3:2`/`16:9`/`1:2`）天然不命中；
# 两个否定环同时挡**数字与冒号**：`123:45`/`12:345` 这类更长数字串不命中，
# `12:30:45`（时:分:秒）与 `1:12:30` 也整串不命中 ⇒ 原样交回 TN。
# 只挡数字的话，`12:30:45` 会被截成「十二点三十分:45」—— 留下一个孤立冒号，
# 比不做更糟（旧 tts-server 正则就是这个毛病）。
_TIME_PATTERN = re.compile(r"(?<![\d:：])(\d{1,2})\s*[:：]\s*(\d{2})(?![\d:：])")


def chinese_under_100(n: int) -> str:
    """0~99 → 规范中文读法：`0`→零、`5`→五、`10`→十、`15`→十五、`20`→二十、
    `30`→三十、`45`→四十五。

    小时（0~23）与分钟（0~59）共用这一个函数 —— 旧的 tts-server 实现里小时走规范
    读法、分钟 ≥10 走逐位读法（`30`→「三零」），是漏了没共用。
    """
    if n < 10:
        return CHINESE_DIGITS[n]
    tens, ones = divmod(n, 10)
    head = "十" if tens == 1 else CHINESE_DIGITS[tens] + "十"
    return head + (CHINESE_DIGITS[ones] if ones else "")


def replace_time(match: re.Match) -> str:
    """单个 `时:分` 匹配 → 中文读法；时/分越界时原样返回。"""
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (HOUR_MIN <= hour <= HOUR_MAX and MINUTE_MIN <= minute <= MINUTE_MAX):
        return match.group(0)
    hour_text = chinese_under_100(hour)
    if minute == 0:
        return f"{hour_text}点整"
    # <10 补零（`9:05` → 九点零五分），其余按数值读（`12:30` → 十二点三十分）
    minute_text = f"零{CHINESE_DIGITS[minute]}" if minute < 10 else chinese_under_100(minute)
    return f"{hour_text}点{minute_text}分"


def normalize_times(text: str) -> str:
    """把时间改成中文读法；其余原样保留（幂等：汉字里已无 `时:分` 形态）。"""
    if not text:
        return text
    return _TIME_PATTERN.sub(replace_time, text)


def apply_time_rules(lines: list) -> list:
    """返回「时间已中文化」的新 lines，**不修改入参**（与 stores.apply_glossary 同风格）。

    未启用（TIME_NORMALIZE=0）时原样返回，调用方可先判断 ENABLED 省开销。
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
            new_line["text"] = normalize_times(text)
        out.append(new_line)
    return out
