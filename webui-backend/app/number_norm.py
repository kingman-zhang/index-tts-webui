"""数字读法归一化：合成前预处理（默认关闭，NUM_NORMALIZE=1 启用）。

为什么需要单独一层
------------------
IndexTTS 的文本前端**自带**中文文本归一化（TN）：

| 平台 | 实现 |
|---|---|
| Linux（服务器/自建引擎） | `tn.chinese.normalizer.Normalizer`（WeTextProcessing + pynini） |
| macOS / Windows | `wetext.Normalizer`（WeTextProcessing 的 C++ 移植） |
| 云引擎（302.ai / SiliconFlow / autodl.art） | 服务端实现，**不受我们控制** |

它本来就会做语境判断。实测（2026-09-28，wetext 替身）：

    拨打110        -> 拨打幺幺零        号码读法 ✓
    110元          -> 一百一十元        数值读法 ✓
    第110位        -> 第一百一十位      数值读法 ✓
    12306          -> 幺二三零六        号码读法 ✓
    客服热线10086  -> 客服热线幺零零八六 号码读法 ✓

**所以「110 元读成幺幺零」这类担心并不成立**，不用改。真正确认的缺口有两类：

1. **长数字串被按数值读**（≥7 位，本模块负责修）：
   `快递单号1234567890` -> `快递单号十二亿三千四百五十六万七千八百九十`
   `身份证110101199001011234` -> 混读成「一百零一万一千二百三十四」
2. **无单位数字偏号码读法**（本模块**故意不管**）：
   `股价110` / `余额110` / `涨了110点` -> 幺幺零。这是 TN 的取舍（裸短数字默认
   当号码），要改就得自己实现「数值 -> 汉字」并接管全部数字，风险远大于收益。

设计原则：**只补缺口，不与 TN 抢活**。因此本模块不做通用数字归一化，
只在「有明确号码语境或号码形态」时把数字**逐位**读成汉字；其余一律返回原文本，
交给 TN。汉字对 TN 是透明的（实测 `幺幺零` 进 TN 原样输出），所以替换结果稳定。

否决优先：后跟单位/量词（元/个/点/月…）或前面是幅度词（第/共/金额/股价…）
时一律不改——避免把数值误读成号码。

落点：`queue_worker._execute_task` 里对**合成副本**生效（与术语表同一位置），
不回写 `task["lines"]`，任务详情与存档仍是用户原文。
"""

from __future__ import annotations

import os
import re

# 默认关闭；启用：NUM_NORMALIZE=1（改 .env 后需重启 backend，config 仅启动时读）
ENABLED = os.environ.get("NUM_NORMALIZE", "0").strip() == "1"

# 号码语境关键词：数字前 CONTEXT_WINDOW 字内出现其一，即认为在说号码
CONTEXT_WORDS = (
    "单号", "订单", "编号", "账号", "帐号", "卡号", "身份证", "手机", "电话", "座机",
    "传真", "分机", "热线", "客服", "快递", "工号", "学号", "车牌", "社保", "医保",
    "邮编", "流水", "序列号", "SN", "QQ", "微信", "支付宝", "转", "拨打",
)
CONTEXT_WINDOW = 24       # 数字左侧看多少字
CONTEXT_AFTER_WINDOW = 8  # 数字右侧看多少字（「1234 是我的电话」）

# 否决词：紧跟在数字后 → 数值语境（TN 已能读对，别抢）
UNITS = (
    "元", "块", "角", "毛", "分", "钱", "年", "月", "日", "号", "点", "个", "次", "人",
    "天", "小时", "分钟", "秒", "岁", "倍", "成", "层", "楼", "页", "章", "节", "名",
    "度", "米", "千米", "公里", "里", "斤", "两", "克", "千克", "吨", "升", "毫升",
    "平方", "立方", "亿", "万", "千", "百", "十", "万", "w", "W", "k", "K", "%", "％",
)
# 否决词：紧邻数字之前 → 数值语境
NUMERIC_PREFIXES = (
    "第", "共", "约", "近", "超过", "超", "不到", "总计", "合计", "金额", "价格", "价值",
    "收入", "成本", "费用", "预算", "利润", "股价", "市值", "余额", "进度", "体脂率",
    "增长", "上涨", "下跌", "约等于", "达到", "高达", "仅", "只有",
)

# ≥4 位连续数字（3 位的像 110/119 交给 TN，它已经读对）
LONG_DIGITS = re.compile(r"(?<!\d)(\d{4,})(?!\d)")
# 带分隔符的号码：400-123-4567 / 135-4567-8900 / 021-88886666（末段可长）
GROUPED_NUMBER = re.compile(r"(?<![\d\-–—－])(\d{2,4}(?:[-–—－]\d{2,8}){1,4})(?![\d\-–—－])")
# 日期/年份区间形态：2026-09-28 / 2026/9/28 —— 不是号码，交回 TN
DATE_LIKE = re.compile(r"^\d{4}[-\-/–—－]\d{1,2}(?:[-\-/–—－]\d{1,2})?$")
# 11 位手机号（形态即证据）
MOBILE = re.compile(r"1[3-9]\d{9}$")

# 号码里的 1 读什么：默认「幺」（口语与 TN 对裸号码的处理一致）；
# NUM_ONE_READ=yi 时读「一」（部分人/场景更习惯）。
_ONE_READ = "一" if os.environ.get("NUM_ONE_READ", "").strip().lower() in ("yi", "1") else "幺"

# 逐位读法
_DIGIT_READ = {
    "0": "零", "1": _ONE_READ, "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
}
_SEPARATORS = "-–—－ "


def read_digits(s: str) -> str:
    """把数字串逐位读成汉字（分隔符丢弃、非数字字符原样保留）。"""
    return "".join(_DIGIT_READ.get(c, "" if c in _SEPARATORS else c) for c in s)


def _context_before(text: str, idx: int) -> str:
    """数字左侧的语境窗口；遇到句末标点即截断（不跨句借语境，不借冒号断）。"""
    seg = text[max(0, idx - CONTEXT_WINDOW):idx]
    for sep in "。！？；!?;\n":
        pos = seg.rfind(sep)
        if pos >= 0:
            seg = seg[pos + 1:]
    return seg


def _context_after(text: str, idx: int) -> str:
    """数字右侧的语境窗口（「1234 是我的电话」这类后置说法）；同样不跨句。"""
    seg = text[idx:idx + CONTEXT_AFTER_WINDOW]
    for sep in "。！？；!?;\n":
        pos = seg.find(sep)
        if pos >= 0:
            seg = seg[:pos]
    return seg


def _is_number_context(text: str, start: int, end: int, matched: str) -> bool:
    """判定这个数字串是否为「号码」。否决优先于命中。"""
    after = text[end:end + 2]
    before = _context_before(text, start)

    # ① 否决：后跟单位/量词 → 数值
    if any(after.startswith(u) for u in UNITS):
        return False
    # ② 否决：前面是幅度词（「第110」「股价110」「金额110」）→ 数值
    if any(before.endswith(p) for p in NUMERIC_PREFIXES):
        return False
    # ③ 否决：小数/版本号等带小数点结构交回 TN；日期/年份区间（2020-2024）同理
    if text[end:end + 1] == "." or text[max(0, start - 1):start] == ".":
        return False
    if DATE_LIKE.match(matched.strip()):
        return False
    # ③b 否决：两段式「年份-年份」（2020-2024）——只有像长号码的才当号码
    groups = [g for g in re.split(r"[-–—－\s]", matched.strip()) if g]
    if len(groups) == 2 and re.fullmatch(r"(19|20)\d{2}", groups[0]) and len(groups[1]) == 4:
        return False

    # ④ 命中：前/后窗口出现号码语境词
    if any(w in before for w in CONTEXT_WORDS):
        return True
    if any(w in _context_after(text, end) for w in CONTEXT_WORDS):
        return True
    # ⑤ 命中：11 位手机号
    if MOBILE.match(re.sub(r"[^\d]", "", matched)):
        return True
    # ⑥ 命中：带分隔符的号码形态（400-123-4567）。收紧——至少三段，或总位数 ≥10，
    #    否则「2020-2024」这类区间会被误判
    if any(c in _SEPARATORS for c in matched):
        total_digits = len(re.sub(r"\D", "", matched))
        if len(groups) >= 3 or total_digits >= 10:
            return True
    # ⑦ 其余：不猜，交回 TN
    return False


def normalize_numbers(text: str) -> str:
    """把文本中的号码逐位读成汉字；非号码数字原样保留。"""
    if not text:
        return text

    # 第一遍：带分隔符的号码（400-123-4567）。在原文上匹配，偏移与 text 一致。
    out = GROUPED_NUMBER.sub(
        lambda m: read_digits(m.group(0))
        if _is_number_context(text, m.start(), m.end(), m.group(0))
        else m.group(0),
        text,
    )

    # 第二遍：纯数字串。在上一遍的结果上匹配，因此语境判断也用同一个串
    # （替换后长度可能变化，用原文偏移会错位）。
    out = LONG_DIGITS.sub(
        lambda m: read_digits(m.group(0))
        if _is_number_context(out, m.start(), m.end(), m.group(0))
        else m.group(0),
        out,
    )
    return out


def apply_number_rules(lines: list) -> list:
    """返回「号码已逐位读」的新 lines，**不修改入参**（与 stores.apply_glossary 同风格）。

    未启用（NUM_NORMALIZE 非 1）时原样返回，调用方可以先判断 ENABLED 省开销。
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
            new_line["text"] = normalize_numbers(text)
        out.append(new_line)
    return out
