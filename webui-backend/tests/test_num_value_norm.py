#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数值读法归一化（app/num_value_norm.py）自测。

用法（在 webui-backend 下）：
    python tests/test_num_value_norm.py

八部分：
  1. 文本层：该改的改（整数/小数/百分号/千分位/正负号/带空格/序数）
  2. 反例清单：不该动的一律不动（日期/时间/区间/版本/号码/超长/无单位）
  3. 与 TN 的互操作（装了 wetext 才跑）：**3a 必须逐字一致**（序数/小数/百分号/
     符号这些不能靠「两≡二」蒙过去）、3b 两≡二 等价化后一致、3c 刻意不一致的清单
     （每条都要说清为什么我们更对 / 为什么接受）—— 这一部分才是本层敢自己动手的依据
  4. 与 year_norm 的顺序耦合：全链输出（年份逐位读 + 数值读）必须同时成立
  5. 与号码层**命中集合不相交**（属性断言）—— 这是「两层不打架」的可证形式
  6. 不变性：不改入参、非 dict 行透传、ENABLED=0 时整体关闭、幂等
  7. 自描述：MAX_DIGITS 必须与正则**实际受理**的位数一致
     （`/api/version` 与部署自检靠它分辨「哪一版规则在跑」）
  8. 「两 / 二」判定的三档优先级（2026-09-30 用户报 `第2章`→「第两章」后新增）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import number_norm  # noqa: E402
from app.num_value_norm import (  # noqa: E402
    ENABLED,
    MAX_DIGITS,
    apply_value_rules,
    normalize_values,
    read_value,
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


def canon(s: str) -> str:
    """比对用的规范化（宽松版）：去掉所有空白、「两」与「二」视为等价。

    只用于**普通量词**那批用例。序数 / 小数 / 百分号 / 符号那批必须走 `canon_exact`
    —— 那里「两」和「二」是**对错之分**，等价化会把「第两章」这种错放过去。

    为什么普通量词要等价化：TN 自带 lexicon，`2元`→两元 而 `2倍`→二倍（按词不按位），
    本层用统一规则（单个 2 + 计数性量词 → 两），不对齐这套不规则词表，见模块 docstring。
    空白：本层把「数字 ↔ 单位」之间的空格吃掉了（`230 倍` → `两百三十倍`），
    wetext 保留空格（`两百三十 倍`）—— 吃空格是有意的，见 NUMBER_NORMALIZATION.md 第九节。
    """
    return "".join(s.split()).replace("两", "二")


def canon_exact(s: str) -> str:
    """比对用的规范化（严格版）：只去空白，「两」与「二」**不**等价。

    序数（`第2章`）、小数（`2.5`）、百分号（`2%`）、符号（`-2度`）这四类里，
    「两」就是错的 —— 中文没有「第两章」「两点五倍」「百分之两」。这些用例
    必须与 TN 逐字相同，否则用户听到的就是错音。
    """
    return "".join(s.split())


# ── 1. 该改的 ────────────────────────────────────────────────────────────
CASES: list[tuple[str, str, str]] = [
    # —— 核心：用户报障的那句 ——
    (
        "美国自 2005 年至 2007 年的飞行安全度是汽车的 230 倍。",
        "美国自 2005 年至 2007 年的飞行安全度是汽车的 两百三十倍。",
        "★ 用户报障句：230 换汉字（年份留给 year_norm，见第 4 部分）",
    ),
    ("是汽车的 230 倍", "是汽车的 两百三十倍", "★ 吃掉「数字 ↔ 单位」间的空格；数字前那个属上下文，保留"),
    ("230倍", "两百三十倍", "无空格"),
    # —— 整数：进位/补零/两 ——
    ("110元", "一百一十元", "前导 1 在十位省「一」"),
    ("110 元", "一百一十元", "带空格"),
    ("105人", "一百零五人", "中间空位补「零」"),
    ("1005人", "一千零五人", "跨位补零"),
    ("1020人", "一千零二十人", "尾部 0 不读"),
    ("1200米", "一千二百米", "非最高位的 2 用「二」"),
    ("1100人", "一千一百人", "含两个 1"),
    ("1234个", "一千二百三十四个", "四位全读"),
    ("2000次", "两千次", "最高位 2 → 两"),
    ("2吨", "两吨", "独立的一个 2 → 两"),
    ("22元", "二十二元", "十位上的 2 → 二"),
    ("220吨", "两百二十吨", "百位 2 → 两"),
    ("2200元", "两千二百元", "千位 2 → 两、百位 2 → 二"),
    ("20000元", "两万元", "万位 2 → 两"),
    ("22000元", "两万二千元", "★ 第二段最高位 2 → 二（不是「两万两千」）"),
    ("12000元", "一万二千元", "第二段 2 → 二"),
    ("10005元", "一万零五元", "跨万段补零"),
    ("25000元", "两万五千元", "万段后不补零"),
    # —— 小数 / 千分位 ——
    ("3.5倍", "三点五倍", "小数读「点」"),
    ("0.5倍", "零点五倍", "零点五"),
    ("1,234元", "一千二百三十四元", "千分位"),
    # —— 百分号：必须前置成「百分之」 ——
    ("30%", "百分之三十", "百分号前置"),
    ("30％", "百分之三十", "★ 全角 ％ 也认（TN 对它不处理）"),
    ("0.3%", "百分之零点三", "小数百分比"),
    # —— 正负号 ——
    ("-5度", "负五度", "负号"),
    ("气温-5度", "气温负五度", "句中负号"),
    ("+5度", "正五度", "正号"),
    # —— 幅度词命中（右侧没有单位词的情况）——
    ("第110位", "第一百一十位", "★ 此前三层都不管的缺口：号码层要 ≥4 位、TN 只到 3 位"),
    ("第3名", "第三名", "序数"),
    ("约500人", "约五百人", "约"),
    ("超过2000米", "超过两千米", "超过"),
    ("仅有5人", "仅有五人", "仅有"),
    ("高达230倍", "高达两百三十倍", "高达"),
    # ══ ★ 序数：单个的 2 读「二」（2026-09-30 用户报障 `第2章` → 「第两章」）══
    ("第2章", "第二章", "★ 中文里根本没有「第两」这个组合"),
    ("第2章 社会中的自我", "第二章 社会中的自我", "★ 用户报障原文（章节名）"),
    ("第2部分", "第二部分", "序数"),
    ("第2次", "第二次", "序数"),
    ("第2年", "第二年", "序数"),
    ("第2期", "第二期", "序数"),
    ("第2届", "第二届", "序数"),
    ("第2个", "第二个", "序数（TN 给「第两个」，本层更规范，见 3c）"),
    ("第20章", "第二十章", "十位上的 2 本来就读「二」"),
    # 序数**只改单个的 2**：多位数与数位单位不受影响
    ("第200章", "第两百章", "★ 200 仍读「两百」（与 TN 一致）"),
    ("第2000章", "第两千章", "同上"),
    ("第2万章", "第两万章", "★「万」是数位单位，优先于序数判定（TN 同）"),
    ("2千万", "两千万", "数位单位"),
    # —— 序数性量词（表示编号/次序，不是计数）——
    ("2月", "二月", "月份 ⇒ 二月（不是「两月」）"),
    ("2号", "二号", "日期 / 编号"),
    ("2号线", "二号线", "线路编号"),
    ("2楼", "二楼", "楼层编号"),
    ("2班", "二班", "班级编号"),
    ("2年级", "二年级", "年级"),
    # —— 小数 / 百分号 / 正负号：单个的 2 读「二」——
    ("2.5倍", "二点五倍", "小数 ⇒ 二点五（不是「两点五」）"),
    ("2.5万", "二点五万", "小数优先于数位单位"),
    ("2%", "百分之二", "百分数 ⇒ 百分之二（不是「百分之两」）"),
    ("-2度", "负二度", "负号 ⇒ 负二度"),
    ("+2度", "正二度", "正号"),
    ("-2万", "负二万", "符号优先于数位单位"),
    ("-2.5度", "负二点五度", "符号 + 小数"),
    # 小数 / 符号**同样只改单个的 2**
    ("200.5倍", "两百点五倍", "整数部分 200 仍读「两百」（与 TN 一致）"),
    ("-200度", "负两百度", "★ 也只改单个 2（TN 给「负二百度」，本层更保守，见 3c）"),
]

# ── 2. 不该动的 ──────────────────────────────────────────────────────────
NEGATIVE: list[tuple[str, str]] = [
    ("100-200人", "区间：v1 刻意不做（要两头处理 + 换「到」）"),
    ("2020-2024年", "两段年份：归 year_norm"),
    ("2026-09-28", "日期"),
    ("3:30", "时间"),
    ("12:30", "时间"),
    ("3:30分", "时间（若咬掉会变成「3:三十分」）"),
    ("400-123-4567", "带分隔号码：归号码层"),
    ("1.5.2", "版本号"),
    ("v2.0", "前邻字母"),
    ("SN12345", "前邻字母"),
    ("2.0版", "版本"),
    ("105", "无单位、无幅度词 ⇒ 不是数值语境（裸数字交回 TN）"),
    ("20", "同上，且两位裸数字 TN 读得对"),
    ("快递单号1234567890", f"> {MAX_DIGITS} 位：不受理，归号码层"),
    ("拨打110", "号码语境（无单位）⇒ 归号码层读「幺幺零」"),
    ("12306", "号码"),
    ("客服热线10086", "号码"),
    ("1 234元", "★ 空格千分位：两个数都不当数值读（TN 也只读对一半）"),
    ("今年是第", "没有数字"),
    ("", "空串"),
    ("18012345678", "11 位手机号：不受理，归号码层"),
]


def run_text_layer() -> None:
    print("1. 文本层：该改的改")
    for src, expected, note in CASES:
        check(f"{src!r}  ({note})", normalize_values(src), expected)
    print("\n2. 反例清单：不该动的一律不动")
    for src, note in NEGATIVE:
        check(f"{src!r}  ({note})", normalize_values(src), src)


def run_read_value() -> None:
    print("\n-- read_value 直接调用（不做语境判断）--")
    check("230", read_value("230"), "两百三十")
    check("1,234", read_value("1,234"), "一千二百三十四")
    check("3.5", read_value("3.5"), "三点五")
    check("0", read_value("0"), "零")
    check("10", read_value("10"), "十")
    check("100", read_value("100"), "一百")
    check("101", read_value("101"), "一百零一")
    check("99999999", read_value("99999999"), "九千九百九十九万九千九百九十九")
    # use_er（「该读二」语境）：**只改单个的 2**，多位数一律不受影响
    check("2   （默认，计数语境）", read_value("2"), "两")
    check("2   （use_er=True）", read_value("2", use_er=True), "二")
    check("20  （use_er=True，本来就读二）", read_value("20", use_er=True), "二十")
    check("200 （use_er=True，仍读「两」）", read_value("200", use_er=True), "两百")
    check("2000（use_er=True，仍读「两」）", read_value("2000", use_er=True), "两千")
    check("2.5 （use_er=True）", read_value("2.5", use_er=True), "二点五")
    check("1200（use_er=True，非最高位本来就是二）", read_value("1200", use_er=True), "一千二百")


def run_oracle() -> None:
    """与 wetext（WeTextProcessing 的 C++ 移植）逐字对照。

    没装 wetext 就跳过 —— 这一部分不是「锦上添花」，而是本层敢自己动手的依据：
    自己实现「数值 → 汉字」最容易跑偏的不是算法，是**读法习惯**。
    """
    print("\n3. 与 TN 的互操作（wetext 基准）")
    try:
        import wetext
    except ImportError:
        print("  [SKIP] 未安装 wetext（pip install wetext 后可跑）")
        return

    tn = wetext.Normalizer(lang="zh", operator="tn")

    # ── 3a：必须**逐字一致**（只去空白，不把「两」当「二」）──
    # 序数 / 小数 / 百分号 / 符号这四类里，「两」就是**错音**；等价化会把错放过去，
    # 所以这批走 canon_exact。
    exact = [
        # 整数 / 量词 / 数位单位
        "230倍", "230 倍", "110元", "110 元", "105人", "1005人", "1020人", "1200米",
        "1100人", "1234个", "2000次", "2吨", "22元", "220吨", "2200元", "20000元",
        "12000元", "10005元", "25000元", "50万", "1.5万", "20万", "3亿",
        # 小数 / 千分位 / 百分号 / 符号
        "3.5倍", "0.5倍", "1,234元", "30%", "0.3%", "-5度", "+5度",
        # 幅度词命中
        "第110位", "第3名", "约500人", "超过2000米", "仅有5人", "第230倍", "余额110元",
        # ★ 2026-09-30 新增：序数 / 序数性量词 / 小数 / 百分号 / 符号
        "第2章", "第2部分", "第2次", "第2年", "第2期", "第2届", "第20章", "第200章",
        "第2000章", "第2万章", "2千万",
        "2月", "2号", "2号线", "2楼", "2班", "2年级",
        "2.5倍", "2.5万", "2%", "-2度", "+2度", "-2.5度", "-2万",
        "2个", "2张", "2元", "2米",
    ]
    for src in exact:
        check(f"{src!r}  TN 逐字一致",
              canon_exact(normalize_values(src)), canon_exact(tn.normalize(src)))

    # ── 3b：两 ≡ 二 等价化后一致（普通量词；两种读法都自然）──
    soft = ["2倍", "2年", "2次", "2成", "2期", "2届", "2章", "2角"]
    for src in soft:
        check(f"{src!r}  两/二 等价化后一致",
              canon(normalize_values(src)), canon(tn.normalize(src)))
    print("         注：TN 的「两」按词不按位（2元→两元 但 2倍→二倍），本层对**计数性")
    print("         量词**统一用「两」（两次/两年/两倍都是自然口语）。两种读法都通，")
    print("         故只断言「等价化后一致」。")

    # ── 3c：刻意不一致 —— 每条都要有理由 ──
    print("\n   -- 刻意不一致（都要有理由，不能是 bug）--")
    check("30％", normalize_values("30％"), "百分之三十")
    print(f"         TN 对全角 ％ 不处理（{tn.normalize('30％')!r}），本层认全角 ⇒ 本层更对")
    check("涨了110点", normalize_values("涨了110点"), "涨了一百一十点")
    print(f"         TN 读「幺幺零点」（{tn.normalize('涨了110点')!r}）：这里的「点」是百分点，")
    print("         数值读才对；TN 把它当号码的一部分 ⇒ 本层更对")
    # TN 的词典在「第 + 单个 2 + 纯计数词」上翻车（给「第两个」「第两名」「第两天」）；
    # 中文里序数一律读「二」⇒ 本层更规范。
    for src, ours in (("第2个", "第二个"), ("第2名", "第二名"), ("第2天", "第二天")):
        check(f"{src!r} 序数一律读二（TN 给 {tn.normalize(src)!r}）", normalize_values(src), ours)
    check("-200度", normalize_values("-200度"), "负两百度")
    print(f"         TN 读「{tn.normalize('-200度')!r}」（它把符号语境的整串都读了「二」）。")
    print("         本层保守：「二」只改**单个的 2**，多位的 200 保「两」")
    print("         —— 与 `第200章`→第两百章、`200.5倍`→两百点五倍 保持一致。")
    # 用户报障整句：TN 修不了（它自己就把 230 读成「两百三十 倍」带空格，
    # 云端的 TN 更是完全没转），本层必须修掉。
    full = "美国自 2005 年至 2007 年的飞行安全度是汽车的 230 倍。"
    check("报障句 数值部分已换汉字", "230" in normalize_values(full), False)


def run_year_chain() -> None:
    """与 year_norm 的顺序耦合：前处理顺序是 year_norm → 本层，不能被换掉。"""
    print("\n4. 与 year_norm 的顺序耦合（year_norm → 本层）")
    from app.year_norm import normalize_years

    chain = [
        # (原文, 期望, 说明)
        (
            "美国自 2005 年至 2007 年的飞行安全度是汽车的 230 倍。",
            "美国自 二零零五年到二零零七年的飞行安全度是汽车的 两百三十倍。",
            "★ 年份逐位读 + 数值读，「这个错那个对」的原因就是前者被 year_norm 接走了",
        ),
        ("距今850年", "距今八百五十年", "★ year_norm 刻意否决的时长形态 ⇒ 本层接住，读数值才对"),
        ("公元850年", "公元八五零年", "带「公元」⇒ year_norm 已换掉，本层看不到"),
        ("100-200年", "100-200年", "两层都不碰：区间两侧都被否决"),
    ]
    for src, expected, note in chain:
        check(f"{src!r}  ({note})", normalize_values(normalize_years(src)), expected)

    # 顺序反了会怎样：先把 230 交给本层没问题，但年份必须先走 year_norm，
    # 否则四位年份会被本层按数值读（这是刻意的顺序耦合，见模块 docstring）。
    check("顺序耦合：四位年份若先过本层会读错", normalize_values("2011年"), "2011年")
    check("  └ 四位数字紧邻「年」的兜底否决仍然生效", normalize_values("2011 年"), "2011 年")


def run_disjoint_with_number_layer() -> None:
    """属性断言：同一段文本上，数值层与号码层**不会同时命中**。

    论证：号码层的第①②条否决恰恰是「后跟单位/量词」「前面是幅度词」——
    正好是本层的命中条件；两层的左语境窗口又是同一个函数（`_context_before`）。
    所以两层的命中集合交集恒为空、并集覆盖「有明确语境」的全部数字。
    这条性质一破，就意味着某处出现了重复替换或两边都撒手。
    """
    print("\n5. 与号码层命中集合不相交（属性）")
    corpus = [
        "美国自 2005 年至 2007 年的飞行安全度是汽车的 230 倍。",
        "快递单号1234567890", "拨打110报警", "客服热线10086", "我的电话是 135-4567-8900",
        "分机8021", "110元", "110个", "110号房间", "第110位", "股价110", "余额110",
        "涨了110点", "订单金额110", "身份证110101199001011234", "400-123-4567",
        "2026-09-28", "3:30", "230倍", "30%", "约500人", "共12个", "第3名",
        "1,234元", "3.5倍", "-5度", "2万", "100-200人",
    ]
    both_fired: list[str] = []
    for t in corpus:
        v = normalize_values(t)
        n = number_norm.normalize_numbers(t)
        if v != t and n != t:
            both_fired.append(f"{t!r}: 数值层→{v!r} 号码层→{n!r}")
    check("无一段被两层同时命中", both_fired, [])
    if both_fired:
        for line in both_fired:
            print(f"         {line}")
    touched = sum(1 for t in corpus
                  if normalize_values(t) != t or number_norm.normalize_numbers(t) != t)
    print(f"         （语料 {len(corpus)} 句，其中 {touched} 句被某一层接管）")


def run_invariance() -> None:
    print("\n6. 不变性")
    lines = [{"text": "是汽车的 230 倍"}, {"text": ""}, "raw-string", {"no_text": 1}]
    snapshot = [dict(x) if isinstance(x, dict) else x for x in lines]
    out = apply_value_rules(lines)
    check("入参未被修改", lines, snapshot)
    check("返回新对象（非同一 list）", out is lines, False)
    check("非 dict 行透传", out[2], "raw-string")
    check("无 text 的 dict 透传", out[3], {"no_text": 1})
    check("空 text 不改", out[1], {"text": ""})
    check("正常行已替换", out[0]["text"], "是汽车的 两百三十倍")
    # 注：数字**前面**的空格属于上下文，保留；只有「数字 ↔ 单位」之间那个被吃掉。
    check("幂等：跑两遍结果相同", normalize_values(normalize_values("是汽车的 230 倍")),
          "是汽车的 两百三十倍")

    # 开关：ENABLED=0 时整体关闭（模块级读取，用子进程验证避免污染本次进程）
    import subprocess
    # 路径与 cwd 都显式给死：`sys.path.insert(0, '.')` 这种写法会**随调用目录变化**
    # —— 从仓库根跑时子进程 import 失败、stdout 为空串，看起来像「功能没生效」，
    # 实际是测试自己坏了（本次就踩了一次）。所以失败时把 stderr 一起报出来。
    root = str(Path(__file__).resolve().parents[1])
    code = (
        f"import sys; sys.path.insert(0, {root!r});"
        "from app.num_value_norm import ENABLED, apply_value_rules;"
        "print(ENABLED, apply_value_rules([{'text':'230倍'}])[0]['text'])"
    )
    env = dict(os.environ, NUM_VALUE_NORMALIZE="0")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, cwd=root)
    actual = r.stdout.strip() if r.returncode == 0 else f"exit={r.returncode} {r.stderr.strip()[-200:]}"
    check("NUM_VALUE_NORMALIZE=0 时关闭（子进程）", actual, "False 230倍")
    check("默认开启", ENABLED, True)


def run_self_description() -> None:
    print("\n7. 自描述：MAX_DIGITS 与正则实际受理范围一致")
    check(f"{MAX_DIGITS} 位受理", normalize_values("9" * MAX_DIGITS + "元"),
          "九千九百九十九万九千九百九十九元" if MAX_DIGITS == 8 else read_value("9" * MAX_DIGITS) + "元")
    check(f"{MAX_DIGITS + 1} 位不受理", normalize_values("9" * (MAX_DIGITS + 1) + "元"),
          "9" * (MAX_DIGITS + 1) + "元")


def run_er_decision() -> None:
    """「两 / 二」判定的三档优先级（2026-09-30 新增的一节）。

    初版把「单个 2 一律读两」收得太宽 —— 用户报 `第2章` 被读成「第两章」。
    现在按三档判定，**顺序本身就是语义**，所以逐档断言：

        ① 符号 / 小数 / 百分号          ⇒ 二（压过 ②③）
        ② 后面紧跟数位单位（万/亿/千/百/十）⇒ 两（压过 ③）
        ③ 其余看序数语境（前缀「第」或序数性量词）⇒ 二，否则两

    并且三档**都只作用于「单个的 2」** —— 这正是「第2章 → 第二章」而
    「第200章 → 第两百章」的原因。
    """
    print("\n8. 「两 / 二」判定的三档优先级")
    from app.num_value_norm import _ER_PREFIXES, _ER_UNITS, _NUMERAL_UNITS, _should_use_er

    # ① 符号 / 小数 / 百分号
    check("① 负号", _should_use_er("2", "-", "", "", "度"), True)
    check("① 正号", _should_use_er("2", "+", "", "", "度"), True)
    check("① 小数", _should_use_er("2.5", "", "", "", "倍"), True)
    check("① 百分号", _should_use_er("2", "", "%", "", ""), True)
    check("① 压过②：负号 + 万", _should_use_er("2", "-", "", "", "万"), True)
    check("① 压过②：小数 + 万", _should_use_er("2.5", "", "", "", "万"), True)
    # ② 数位单位
    check("② 数位单位（万）", _should_use_er("2", "", "", "", "万章"), False)
    check("② 数位单位（千万）", _should_use_er("2", "", "", "", "千万"), False)
    check("② 压过③：第 + 万", _should_use_er("2", "", "", "第", "万章"), False)
    # ③ 序数语境
    check("③ 序数前缀「第」", _should_use_er("2", "", "", "第", "章"), True)
    check("③ 序数性量词（月）", _should_use_er("2", "", "", "", "月"), True)
    check("③ 序数性量词（班）", _should_use_er("2", "", "", "", "班"), True)
    check("③ 普通计数量词（个）", _should_use_er("2", "", "", "", "个"), False)
    check("③ 完全没有语境", _should_use_er("2", "", "", "", ""), False)

    # 「二」的语境只改单个的 2
    check("单个 2 受语境影响", normalize_values("第2章"), "第二章")
    check("多位的 200 不受影响", normalize_values("第200章"), "第两百章")
    check("多位的 2000 不受影响", normalize_values("第2000章"), "第两千章")
    check("小数里的多位不受影响", normalize_values("200.5倍"), "两百点五倍")

    # 三张表本身要能被核对（改了表这里就跟着变，防「表被悄悄掏空」）
    check("序数前缀表", list(_ER_PREFIXES), ["第"])
    check("序数性量词表", list(_ER_UNITS), ["月", "号", "楼", "班", "年级"])
    check("数位单位表", list(_NUMERAL_UNITS), ["万", "亿", "千", "百", "十"])


if __name__ == "__main__":
    run_text_layer()
    run_read_value()
    run_oracle()
    run_year_chain()
    run_disjoint_with_number_layer()
    run_invariance()
    run_self_description()
    run_er_decision()
    print(f"\n{'=' * 60}\nPASS={PASS}  FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)
