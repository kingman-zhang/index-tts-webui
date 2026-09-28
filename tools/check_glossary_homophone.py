#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""术语表同音性校验器（v2）。

术语表是「逐字同音替换」机制。硬标准：

    替换词的读音 == 原词的正确读音（逐字、含声调）

本脚本用 pypinyin 批量校验，并输出可直接落地的候选 JSON。
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from pypinyin import Style, pinyin

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY = ROOT / "webui-backend" / "data" / "glossary.json"
OUT = ROOT / "tools" / "glossary_proposed.json"

# 现有条目人工标注的正确读音（校验基准）
EXISTING_TARGETS = {
    "AI": None,
    "说服": "shuō fú",
    "9・11": None,
    "鳏夫": "guān fū",
    "掷骰子": "zhì tóu zi",
    "重头": "chóng tóu",
}

# (原词, 正确读音, 建议替换词)
GROUPS: dict[str, list[tuple[str, str, str]]] = {
    "一、日常高频多音词": [
        ("游说", "yóu shuì", "游睡"),
        ("提防", "dī fáng", "低防"),
        ("给予", "jǐ yǔ", "己雨"),
        ("参与", "cān yù", "参玉"),
        ("与会", "yù huì", "玉会"),
        ("勉强", "miǎn qiǎng", "勉抢"),
        ("强迫", "qiǎng pò", "抢迫"),
        ("倔强", "jué jiàng", "倔匠"),
        ("劲敌", "jìng dí", "敬敌"),
        ("薄弱", "bó ruò", "伯弱"),
        ("剥削", "bō xuē", "拨靴"),
        ("堵塞", "dǔ sè", "堵色"),
        ("鲜为人知", "xiǎn wéi rén zhī", "显为人知"),
        ("恐吓", "kǒng hè", "恐贺"),
        ("埋怨", "mán yuàn", "蛮怨"),
        ("尽管", "jǐn guǎn", "紧管"),
        ("尽量", "jǐn liàng", "紧量"),
        ("露面", "lòu miàn", "漏面"),
        ("露馅", "lòu xiàn", "漏馅"),
        ("露马脚", "lòu mǎ jiǎo", "漏马脚"),
        ("中奖", "zhòng jiǎng", "重奖"),
        ("中意", "zhòng yì", "重意"),
        ("称心", "chèn xīn", "趁心"),
        ("称职", "chèn zhí", "趁职"),
        ("创伤", "chuāng shāng", "窗伤"),
        ("重创", "zhòng chuāng", "重窗"),
        ("上当", "shàng dàng", "上荡"),
        ("当作", "dàng zuò", "荡作"),
        ("间隔", "jiàn gé", "见隔"),
        ("间断", "jiàn duàn", "见断"),
        ("间谍", "jiàn dié", "见谍"),
        ("看守", "kān shǒu", "刊守"),
        ("看管", "kān guǎn", "刊管"),
        ("笼罩", "lǒng zhào", "垄罩"),
        ("笼络", "lǒng luò", "垄络"),
        ("翘首", "qiáo shǒu", "桥首"),
        ("似的", "shì de", "是的"),
        ("半身不遂", "bàn shēn bù suí", "半身不随"),
        ("呕吐", "ǒu tù", "呕兔"),
        ("呜咽", "wū yè", "呜夜"),
        ("哽咽", "gěng yè", "哽夜"),
        ("佣金", "yòng jīn", "用金"),
        ("粘稠", "nián chóu", "年稠"),
        ("症结", "zhēng jié", "争结"),
        ("正月", "zhēng yuè", "争月"),
        ("应酬", "yìng chóu", "硬酬"),
        ("应聘", "yìng pìn", "硬聘"),
        ("应届", "yīng jiè", "英届"),
        ("载重", "zài zhòng", "再重"),
        ("记载", "jì zǎi", "记宰"),
        ("折本", "shé běn", "蛇本"),
        ("宝藏", "bǎo zàng", "宝葬"),
        ("果脯", "guǒ fǔ", "果府"),
        ("号啕", "háo táo", "豪啕"),
        ("和面", "huó miàn", "活面"),
        ("教书", "jiāo shū", "交书"),
        ("丧事", "sāng shì", "桑事"),
        ("刹车", "shā chē", "沙车"),
        ("扇风", "shān fēng", "山风"),
        ("拓片", "tà piàn", "踏片"),
        ("遗赠", "wèi zèng", "位赠"),
        ("择菜", "zhái cài", "宅菜"),
        ("牛仔", "niú zǎi", "牛宰"),
        ("处理", "chǔ lǐ", "楚理"),
        ("的确", "dí què", "敌确"),
        ("厌恶", "yàn wù", "厌勿"),
        ("分量", "fèn liàng", "份量"),
        ("磨坊", "mò fáng", "磨房"),
        ("冠军", "guàn jūn", "贯军"),
        ("会计", "kuài jì", "快计"),
        ("几乎", "jī hū", "机乎"),
        ("校对", "jiào duì", "叫对"),
        ("结实", "jiē shi", "街实"),
        ("空白", "kòng bái", "控白"),
        ("落枕", "lào zhěn", "涝枕"),
        ("反省", "fǎn xǐng", "反醒"),
        ("标识", "biāo zhì", "标致"),
        ("通红", "tòng hóng", "痛红"),
        ("旋风", "xuàn fēng", "炫风"),
        ("畜牧", "xù mù", "蓄牧"),
        ("折腾", "zhē teng", "遮腾"),
        ("重复", "chóng fù", "虫复"),
        ("着装", "zhuó zhuāng", "酌装"),
        ("悄然", "qiǎo rán", "巧然"),
        ("散文", "sǎn wén", "伞文"),
        ("宿舍", "sù shè", "宿社"),
        ("盛饭", "chéng fàn", "成饭"),
        ("扁舟", "piān zhōu", "偏舟"),
        ("钥匙", "yào shi", "要匙"),
        ("绿林", "lù lín", "路林"),
        ("踏实", "tā shi", "塌实"),
        ("系鞋带", "jì xié dài", "记鞋带"),
        ("时髦", "shí máo", "时毛"),
    ],
    "二、其他多音字高频词": [
        ("逮捕", "dài bǔ", "代捕"),
        ("召开", "zhào kāi", "兆开"),
        ("恪守", "kè shǒu", "客守"),
        ("古刹", "gǔ chà", "古岔"),
        ("谄媚", "chǎn mèi", "产媚"),
        ("讪笑", "shàn xiào", "善笑"),
        ("刻薄", "kè bó", "刻伯"),
        ("菲薄", "fěi bó", "匪伯"),
        ("边塞", "biān sài", "边赛"),
        ("要塞", "yào sài", "要赛"),
        ("累赘", "léi zhuì", "雷赘"),
        ("擂台", "lèi tái", "泪台"),
        ("露脸", "lòu liǎn", "漏脸"),
        ("作践", "zuò jian", "坐践"),
    ],
    "三、专有名词 / 地名 / 人名": [
        ("秘鲁", "bì lǔ", "必鲁"),
        ("六安", "lù ān", "路安"),
        ("蚌埠", "bèng bù", "蹦布"),
        ("台州", "tāi zhōu", "胎州"),
        ("丽水", "lí shuǐ", "离水"),
        ("蒙古", "měng gǔ", "猛古"),
        ("厦门", "xià mén", "夏门"),
        ("燕京", "yān jīng", "烟京"),
        ("龟裂", "jūn liè", "军裂"),
        ("吐蕃", "tǔ bō", "吐波"),
        ("可汗", "kè hán", "克寒"),
        ("单于", "chán yú", "蝉于"),
        ("华山", "huà shān", "化山"),
        ("会稽", "kuài jī", "快基"),
        ("番禺", "pān yú", "潘禺"),
        ("尉迟", "yù chí", "玉迟"),
        ("骠骑", "piào qí", "票骑"),
    ],
}

# 目标读音无常用同音字 → 换词法不可用
NO_HOMOPHONE = [
    ("着", "zháo", "着急/着火/着凉/睡着", "普通话 zháo 音只有「着」字"),
    ("难", "nàn", "难民/责难/灾难", "nàn 无常用同音字"),
    ("宁", "nìng", "宁可/宁愿/宁可", "nìng 只有生僻字「佞/泞」"),
    ("横", "hèng", "蛮横/横财/强横", "hèng 无常用同音字"),
    ("蒙", "mēng", "蒙骗/蒙人/瞎蒙", "mēng 无常用同音字"),
    ("迫", "pǎi", "迫击炮", "pǎi 无常用同音字"),
    ("钻", "zuàn", "钻石/电钻", "zuàn 仅「攥」且生僻"),
    ("揣", "chuǎi", "揣度/揣测", "chuǎi 无常用同音字"),
    ("得", "děi", "得亏/得劲儿", "děi 无常用同音字"),
    ("扎", "zhá", "挣扎/札", "需借「轧/札」等生僻字"),
    ("巷", "hàng", "巷道", "hàng 无常用同音字"),
    ("模", "mú", "模样/模具", "mú 无常用同音字"),
    ("发", "fà", "头发/发型/理发", "fà 仅「珐」且生僻"),
    ("色", "shǎi", "掉色/变色儿", "shǎi 无常用同音字"),
    ("舍", "shě", "舍不得", "与 shè 并存，需上下文"),
    ("闷", "mēn", "闷热/闷头/闷声", "mēn 无常用同音字（「焖」读 mèn）"),
    ("作", "zuō", "作坊/作践", "zuō 无常用同音字"),
    ("南", "nā", "南无阿弥陀佛", "nā 无常用同音字（「那」读 nà）"),
]


def tone_of(word: str) -> str:
    if not word.strip():
        return ""
    return " ".join(p[0] for p in pinyin(word, style=Style.TONE,
                                         errors=lambda x: [c for c in x]))


TONE_MARKS = {"\u0304": "1", "\u0301": "2", "\u030c": "3", "\u0300": "4"}


def parse_target(spec: str) -> list[tuple[str, str]]:
    """把人工写作的 "chóng tóu" 解析成 [(chong,2),(tou,2)]。"""
    out: list[tuple[str, str]] = []
    for syl in spec.split():
        if syl[-1].isdigit():
            out.append((syl[:-1].lower().replace("ü", "v"), syl[-1]))
            continue
        base, tone = [], "5"
        for ch in unicodedata.normalize("NFD", syl):
            mark = TONE_MARKS.get(ch)
            if mark:
                tone = mark
            else:
                base.append(ch)
        out.append(("".join(base).lower().replace("ü", "v"), tone))
    return out


def parse_replacement(word: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in pinyin(word, style=Style.TONE3, errors=lambda x: [c for c in x]):
        s = str(p[0]).lower()
        if s and s[-1].isdigit():
            out.append((s[:-1], s[-1]))
        else:
            out.append((s, "5"))
    return out


def compare(target: str, replacement: str) -> tuple[str, str]:
    """返回 (判定, 说明)。判定 ∈ {PASS, 轻声, 风险}。"""
    a, b = parse_target(target), parse_replacement(replacement)
    if len(a) != len(b):
        return "风险", f"音节数不一致 {a} vs {b}"
    if a == b:
        return "PASS", ""
    if [x[0] for x in a] != [y[0] for y in b]:
        return "风险", f"声母/韵母不同 {a} vs {b}"
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    if all(x[1] == "5" or y[1] == "5" for x, y in diffs):
        return "轻声", f"仅轻声差异 {diffs}"
    return "风险", f"声调不同 {diffs}"


def main() -> None:
    print("=" * 80)
    print("一、现有术语表逐条校验")
    print("=" * 80)
    terms = json.loads(GLOSSARY.read_text(encoding="utf-8")) if GLOSSARY.exists() else []
    for t in terms:
        o, r = t["original"], t["replacement"]
        target = EXISTING_TARGETS.get(o)
        if target is None:
            print(f"  [跳过] {o!r:10} -> {r!r:10} 非中文字词（拆读/数字读法）")
            continue
        verdict, why = compare(target, r)
        print(f"  [{verdict:4}] {o!r:8} -> {r!r:8} "
              f"应读={target:16} 替换词实读={tone_of(r)}")
        if verdict == "风险":
            print(f"         ^ {why}：该条会把正确读音读错，需改字或删除")

    proposed = []
    print("\n" + "=" * 80)
    print("二、候选补充词校验")
    print("=" * 80)
    total = bad = soft = 0
    for group, items in GROUPS.items():
        print(f"\n  ▎{group}")
        for o, target, r in items:
            total += 1
            verdict, why = compare(target, r)
            bad += verdict == "风险"
            soft += verdict == "轻声"
            print(f"    [{verdict:4}] {o:10} -> {r:10} "
                  f"应读={target:22} 实读={tone_of(r)}"
                  + (f"   {why}" if verdict != "PASS" else ""))
            proposed.append({"original": o, "replacement": r})
    print(f"\n  候选合计 {total} 条：完全同音 {total - bad - soft} 条 / "
          f"仅轻声差异 {soft} 条（可接受）/ 真错 {bad} 条")

    print("\n" + "=" * 80)
    print("三、换词法不可用的读音（目标音无常用同音字，需另想办法）")
    print("=" * 80)
    for ch, y, words, why in NO_HOMOPHONE:
        print(f"  {words:22} {y:6} {ch}   —— {why}")

    OUT.write_text(json.dumps(proposed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n候选已写入: {OUT.relative_to(ROOT)}（{len(proposed)} 条，未生效）")

    # ── 导出可读清单 ────────────────────────────────────────────────────
    md = ["# 术语词汇表：预置多音词清单", "",
          "> 由 `tools/check_glossary_homophone.py` 自动生成。"
          "所有条目均经 pypinyin 逐字比对声母、韵母、声调。", "",
          "## 一、现有条目", "", "| 原词 | 替换为 | 应读 | 校验 |", "|---|---|---|---|"]
    for t in terms:
        o, r = t["original"], t["replacement"]
        target = EXISTING_TARGETS.get(o)
        if target is None:
            md.append(f"| {o} | {r} | — | 非中文字词 |")
        else:
            v, _ = compare(target, r)
            mark = {"PASS": "通过", "轻声": "轻声差异", "风险": "**需修正**"}[v]
            md.append(f"| {o} | {r} | {target} | {mark} |")

    md += ["", "## 二、建议补充", ""]
    for group, items in GROUPS.items():
        md += [f"### {group}", "", "| 原词 | 替换为 | 正确读音 | 校验 |", "|---|---|---|---|"]
        for o, target, r in items:
            v, _ = compare(target, r)
            mark = {"PASS": "通过", "轻声": "轻声差异", "风险": "**风险**"}[v]
            md.append(f"| {o} | {r} | {target} | {mark} |")
        md.append("")

    md += ["## 三、换词法覆盖不到的读音", "",
           "这些音在普通话里没有常用同音字，无法用换词法解决，"
           "只能靠拼音标注或接受现状：", "",
           "| 例词 | 读音 | 字 | 说明 |", "|---|---|---|---|"]
    for ch, y, words, why in NO_HOMOPHONE:
        md.append(f"| {words} | {y} | {ch} | {why} |")
    md.append("")

    md_path = ROOT / "术语表-预置多音词清单.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"可读清单已写出: {md_path.name}")

    print("\n" + "=" * 80)
    print("四、对现有「重头 -> 从头」的修正建议")
    print("=" * 80)
    print("  「从头」读 cóng tóu（从=cóng），而「重头」应读 chóng tóu（重=chóng）。")
    print("  声母 c / ch 不同，该条等于把正确音读成了错音。候选改法：")
    for cand in ["崇头", "虫头"]:
        verdict, why = compare("chóng tóu", cand)
        print(f"    重头 -> {cand}   [{verdict}] 实读={tone_of(cand)}")


if __name__ == "__main__":
    main()
