#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文本诊断：一句话为什么被读错 / 我配的词条为什么没生效。

TTS 读错字的投诉里，绝大多数不是「模型不行」，而是**送进去的文本已经不是用户以为的那份**。
这条链路有五道关，任何一道静默改变文本，用户都看不见：

    原文 --[术语表 str.replace]--> --[人名分隔号归一化]--> --[年份读法]--> --[数字读法]--> 发往引擎

（2026-09-29 补第五道关：年份读法原先只存在于 tts-server 的本地引擎链路上，
云引擎链路绕过它 ⇒「以前修好的年份读法又坏了」。详见 webui-backend/app/year_norm.py。）

本工具把这条链路**逐步打印**出来，并把「看不见的字符」曝光：
同一个视觉符号（中点、破折号、引号、空格）可能有多个 Unicode 码位，
`str.replace` 是精确匹配 —— 码位差一个，词条就静默失效。

用法（在**仓库根目录**执行，cwd 不影响结果）：

    # 诊断一句话（最常用）
    python3 tools/diagnose_text.py "那年发生了9・11事件"

    # 从文件读（每行按一句诊断；建议一次别超过 50 行）
    python3 tools/diagnose_text.py --file script.txt

    # 只巡检词表本身（找码位脆弱的词条、空替换、重复项）
    python3 tools/diagnose_text.py --audit

    # 只打印环境（在服务器上确认词表版本时用）
    python3 tools/diagnose_text.py --env

数据目录非默认时（与 glossary_admin.py 一致）：

    python3 tools/diagnose_text.py --data-dir /path/to/data --env

> 本脚本从仓库根 tools/ 运行（与 diagnose_pinyin.py / probe_line_pipeline.py 同一层）。
> 默认 DATA_DIR 固定指向 `webui-backend/data`，**不跟随 cwd** —— 否则在仓库根执行时
> 会去读 `./data`（仓库根那个 data 是另一套东西），报告「词表不存在」，把人带偏。

判定「会变成 unk」需要 bpe.model（data/models/bpe.model，可从 modelscope 获取，
见 MEMORY）。缺失时该节自动跳过，其余功能不受影响。
注意：unk 检查是**本地 front.py 参考**；云端（302.ai / autodl.art）的文本前端
未必与本地一致，因此它只用于「提前发现明显可疑的字符」，不是云端行为的断言。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from collections import Counter
from pathlib import Path

# 让 `import app` 生效（tools/ 与 webui-backend/ 同级）
BACKEND_ROOT = Path(__file__).resolve().parent.parent / "webui-backend"
sys.path.insert(0, str(BACKEND_ROOT))

# DATA_DIR 钉在 webui-backend/data，不跟随 cwd。
# app.config 的缺省值自 2026-09-28 起已是「相对 backend 根」（原先 "./data" 是
# 相对 cwd 的），所以这行严格来说是冗余的；保留它是因为本脚本从仓库根调用时
# 一旦有人把 config 的缺省改回去，静默读到**仓库根的 data/**（另一套东西）会
# 报「全局词表不存在」—— 一个会把人带偏的假警报。显式 --data-dir 仍优先。
os.environ.setdefault("DATA_DIR", str(BACKEND_ROOT / "data"))

from app import config as app_config  # noqa: E402  （读 --data-dir，必须在 stores 之前）
from app import name_punct, number_norm, stores, year_norm  # noqa: E402
from app.stores import (  # noqa: E402
    apply_glossary,
    expand_separator_variants,
    load_global_glossary,
    load_glossary_for_synthesis,
)

# ── 字符分类辅助 ────────────────────────────────────────────────────────
CJK_RANGES = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F),
)

# 「看起来一样但不是一个字符」的重灾区，用于把可疑字符点名说清楚
SUSPICIOUS_NAMES = {
    "\u00b7": "中文间隔号（标准中点）",
    "\u0387": "希腊分号点",
    "\u16eb": "卢恩单标点",
    "\u2022": "项目符号圆点",
    "\u2027": "连字点（hyphenation point）",
    "\u2219": "项目符号运算符",
    "\u22c5": "点运算符",
    "\u2e31": "词分隔中点",
    "\u30fb": "片假名中点",  # ← 用户最常用，但很多工具不认
    "\uff65": "半角片假名中点",
    "\u2010": "连字符 HYPHEN",
    "\u2011": "不换行连字符",
    "\u2012": "数字跨度连字符",
    "\u2013": "短破折号 EN DASH",
    "\u2014": "长破折号 EM DASH",
    "\u2015": "水平线",
    "\uff0d": "全角连字符",
    "\u2212": "数学减号",
    "\u002d": "半角连字符 ASCII",
    "\u0020": "半角空格 ASCII",
    "\u3000": "全角空格",
    "\u00a0": "不换行空格",
    "\u2007": "数字空格",
    "\u200b": "零宽空格",
    "\u201c": "左双引号",
    "\u201d": "右双引号",
    "\u300c": "左直角引号",
    "\u300d": "右直角引号",
    "\uff0c": "全角逗号",
    "\u3001": "顿号",
    "\uff1a": "全角冒号",
    "\u2026": "省略号",
    "\uff5e": "全角波浪线",
}


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in CJK_RANGES)


def is_plain(ch: str) -> bool:
    """无需在报告里点名的字符：ASCII 可见字符、汉字、常见中文标点。"""
    if ch.isascii() and ch.isprintable():
        return True
    if is_cjk(ch):
        return True
    return ch in "，。！？；：“”‘’（）《》〈〉—…、％"


def char_label(ch: str) -> str:
    name = SUSPICIOUS_NAMES.get(ch)
    if name:
        return name
    try:
        return unicodedata.name(ch)
    except ValueError:
        return "未命名字符"


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ── 环境 ────────────────────────────────────────────────────────────────
def show_env() -> dict:
    terms = load_global_glossary()
    synth_terms = load_glossary_for_synthesis(None)
    section("环境")
    print(f"DATA_DIR          : {app_config.DATA_DIR.resolve()}")
    print(f"全局词表（真源）  : {app_config.GLOSSARY_PATH.resolve()}"
          f"  {'存在' if app_config.GLOSSARY_PATH.exists() else '!! 不存在'}")
    print(f"用户词表目录      : {app_config.GLOSSARY_USERS_DIR.resolve()}")
    print(f"全局词条数        : {len(terms)}")
    print(f"合成生效条数      : {len(synth_terms)}"
          f"（中点变体展开 {'开' if stores.GLOSSARY_SEP_VARIANTS else '关'}"
          f"，单条上限 {stores.MAX_SEP_VARIANTS}）")
    print(f"人名分隔号归一化  : {'开' if name_punct.ENABLED else '关'}"
          f"（目标形态 {name_punct.TARGET}）")
    print(f"年份读法归一化    : {'开' if year_norm.ENABLED else '关'}"
          f"（四位年份逐位读，如 2011 年 → 二零一一年）")
    print(f"数字读法归一化    : {'开' if number_norm.ENABLED else '关'}")
    return {"terms": len(terms), "synth_terms": len(synth_terms)}


# ── 字符巡检 ────────────────────────────────────────────────────────────
def scan_chars(text: str) -> list:
    """列出所有「可能需要点名」的字符及其码位。"""
    odd = [(i, ch) for i, ch in enumerate(text) if not is_plain(ch)]
    if not odd:
        return []
    counter = Counter(ch for _i, ch in odd)
    print(f"{'字符':<6}{'码位':<10}{'次数':<6}名称")
    for ch, n in counter.most_common():
        tag = ""
        if ch in name_punct.NAME_SEPARATORS:
            tag = "  ← 人名分隔号变体，会被归一化"
        print(f"{ch!r:<6}U+{ord(ch):04X}    {n:<6}{char_label(ch)}{tag}")
    return odd


# ── 链路 ────────────────────────────────────────────────────────────────
def apply_front_char_map(text: str) -> str:
    """复现 front.py 的 char_rep_map（本地引擎在送进分词器前的字符替换）。"""
    for key in sorted(FRONT_CHAR_REP_MAP, key=len, reverse=True):
        text = text.replace(key, FRONT_CHAR_REP_MAP[key])
    return text


def run_chain(text: str, terms: list, member_id: str | None) -> dict:
    """按 queue_worker._execute_task 的真实顺序跑一遍，返回各阶段文本。"""
    hit: list[tuple[str, str]] = []
    after_glossary = text
    for t in terms:
        original, replacement = t.get("original", ""), t.get("replacement", "")
        if original and original in after_glossary:
            hit.append((original, replacement))
            after_glossary = after_glossary.replace(original, replacement)

    line = [{"text": after_glossary}]
    after_punct = (name_punct.apply_name_separator_rules(line)[0]["text"]
                   if name_punct.ENABLED else after_glossary)
    after_year = (year_norm.apply_year_rules([{"text": after_punct}])[0]["text"]
                  if year_norm.ENABLED else after_punct)
    after_number = (number_norm.apply_number_rules([{"text": after_year}])[0]["text"]
                    if number_norm.ENABLED else after_year)
    return {
        "original": text,
        "hits": hit,
        "after_glossary": after_glossary,
        "after_punct": after_punct,
        "after_year": after_year,
        "final": after_number,
        # 引擎侧清洗（本地 front.py 的 char_rep_map）：云端是否有同表未知
        "after_front": apply_front_char_map(after_number),
    }


def show_chain(stage: dict, verbose: bool = False) -> None:
    src, final = stage["original"], stage["final"]
    print(f"原文   : {src}")
    if stage["hits"]:
        shown = "、".join(f"{o}→{r}" for o, r in stage["hits"][:8])
        more = f" …（共 {len(stage['hits'])} 条）" if len(stage["hits"]) > 8 else ""
        print(f"术语表 : 命中 {shown}{more}")
    else:
        print("术语表 : 无命中")
    if stage["after_punct"] != stage["after_glossary"]:
        print(f"分隔号 : {stage['after_punct']}")
    elif verbose:
        print("分隔号 : （未改动）")
    if stage["after_year"] != stage["after_punct"]:
        print(f"年份   : {stage['after_year']}")
    elif verbose:
        print("年份   : （未改动）")
    if stage["final"] != stage["after_year"]:
        print(f"数字   : {stage['final']}")
    elif verbose:
        print("数字   : （未改动）")
    if stage["after_front"] != final:
        print(f"引擎侧 : {stage['after_front']}")
        print("         ↑ 本地 front.py 的字符替换表（如 ·→-）会再改一道；")
        print("           云端引擎是否有同一张表，目前未知。")
    if not stage["hits"] and stage["after_front"] == src:
        print("       （原文未被任何一道关改动）")


# ── unk 检查（可选） ────────────────────────────────────────────────────
def load_sp():
    """加载 bpe.model；不可用时返回 None（不报错，优雅降级）。

    按顺序找：环境变量 → 当前 DATA_DIR → 项目固定位置 → 上游仓库。
    加上后两个是因为 `--data-dir` 指向别处（例如诊断服务器语境）时，
    词表该跟着换，但 bpe.model 是同一份，不该因此丢失检查能力。
    """
    candidates = [
        os.environ.get("BPE_MODEL", ""),
        str(Path(app_config.DATA_DIR) / "models" / "bpe.model"),
        str(BACKEND_ROOT / "data" / "models" / "bpe.model"),
        str(BACKEND_ROOT.parent / "index-tts-main" / "bpe.model"),
        str(BACKEND_ROOT.parent / "index-tts-main" / "checkpoints" / "bpe.model"),
    ]
    model = next((Path(c) for c in candidates if c and Path(c).is_file()), None)
    if model is None:
        return None
    try:
        import sentencepiece as spm
    except ImportError:
        return None
    sp = spm.SentencePieceProcessor()
    if not sp.load(str(model)):
        return None
    print(f"（bpe.model = {model}）")
    return sp


# front.py 的字符替换表（本地引擎在文本清洗阶段应用），抄录自
# index-tts-main/indextts/utils/front.py:16-52（2026-09-28）。
# 作用：把常见中文标点换成词表内的 ASCII 形态 —— 。→. ，→, “→' —→- ·→-
# 因此这些字符**不会**变 unk。不在此表中的字符则原样进词表。
FRONT_CHAR_REP_MAP = {
    "：": ",", "；": ",", ";": ",", "，": ",", "。": ".", "！": "!", "？": "?",
    "\n": " ", "·": "-", "、": ",", "...": "…", ",,,": "…", "，，，": "…",
    "……": "…", "“": "'", "”": "'", '"': "'", "‘": "'", "’": "'",
    "（": "'", "）": "'", "(": "'", ")": "'", "《": "'", "》": "'",
    "【": "'", "】": "'", "[": "'", "]": "'", "「": "'", "」": "'",
    "—": "-", "～": "-", "~": "-", ":": ",", "$": ".",
}


def symbol_risks(sp, text: str) -> list:
    """找出「经 front.py 字符映射后仍不在词表」的字符 —— 即会变 unk 的那些。

    为什么不用「逐个删字符再比 unk 总数」：SentencePiece 会把连续未知字符合并成
    一个 <unk>，而且裸编码对 ASCII 数字本身就有伪影（`9·11` 与 `911` 得到完全
    相同的 token），删除实验因此不可用。改成**分离单字符判定**：
    只问「这个非 ASCII 字符，经 front.py 的映射后，单独编码会不会 unk」。
    实测该判据与完整前端链路（front.normalize + tokenize_by_CJK_char）结论一致。

    只查非 ASCII 字符：数字/字母/半角标点是 TN 与词表的正常输入，误报即噪声。
    """
    out = []
    for ch in dict.fromkeys(text):
        if ch.isascii():
            continue
        mapped = FRONT_CHAR_REP_MAP.get(ch, ch)
        if not mapped:
            continue
        if sp.unk_id() in sp.Encode(mapped, out_type=int):
            out.append((ch, mapped, char_label(ch)))
    return out


# ── 词表巡检 ────────────────────────────────────────────────────────────
def audit_glossary() -> None:
    terms = load_global_glossary()
    synth = load_glossary_for_synthesis(None)
    tail = f" → 合成时展开为 {len(synth)} 条" if len(synth) != len(terms) else ""
    section(f"词表巡检（全局库 {len(terms)} 条{tail}）")

    fragile, empty_rep, dup = [], [], {}
    for t in terms:
        original = t.get("original", "")
        replacement = t.get("replacement", "")
        dup.setdefault(original, []).append(replacement)
        if not replacement:
            empty_rep.append(original)
        odd = [c for c in original if not is_plain(c)]
        if odd:
            fragile.append((original, [(c, f"U+{ord(c):04X}", char_label(c)) for c in odd]))

    def only_separators(chars) -> bool:
        return all(c in name_punct.NAME_SEPARATORS for c, _code, _name in chars)

    covered = [(o, cs) for o, cs in fragile if only_separators(cs)]
    risky = [(o, cs) for o, cs in fragile if not only_separators(cs)]

    if stores.GLOSSARY_SEP_VARIANTS:
        print(f"\n[中点类条目] {len(covered)} 条 —— 合成时自动展开成全部码位变体，无需担心原文写法：")
        if not covered:
            print("  （无）")
        for original, _chars in covered:
            n = len(stores.separator_variants(original))
            print(f"  {original!r} → 自动覆盖 {n + 1} 种写法")
    else:
        print(f"\n[中点类条目] {len(covered)} 条 —— **变体展开已关闭**（GLOSSARY_SEP_VARIANTS=0）：")
        if not covered:
            print("  （无）")
        for original, chars in covered:
            detail = "  ".join(f"{c!r}({code})" for c, code, _name in chars)
            print(f"  {original!r}   {detail}  —— 原文换一种中点即静默失效")

    print(f"\n[其它码位敏感的条目] {len(risky)} 条 —— 只对下面这种精确写法生效：")
    if not risky:
        print("  （无）")
    for original, chars in risky:
        detail = "  ".join(f"{c!r}({code} {name})" for c, code, name in chars)
        print(f"  {original!r}")
        print(f"      {detail}")

    if fragile:
        print("\n提示：词条 key 与用户输入必须**码位完全一致**。从公众号 / 网页 / Word")
        print("      粘贴过来的文本常把 `・`(U+30FB) 变成 `·`(U+00B7)，肉眼无法分辨。")
        print("      中点类已自动展开（见上）；其它特殊字符请补词条或用诊断命令核对原文。")

    if empty_rep:
        print(f"\n[空替换 = 停用该词] {len(empty_rep)} 条：{'、'.join(empty_rep)}")

    repeats = {k: v for k, v in dup.items() if len(v) > 1}
    if repeats:
        print(f"\n[重复 original] {len(repeats)} 条（后一条实际不生效，因为先匹配先替换）：")
        for k, v in repeats.items():
            print(f"  {k!r} → {v}")


# ── 主流程 ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="诊断一句文本在送进 TTS 之前被怎么改了，以及词条为什么没生效。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("text", nargs="?", help="要诊断的文本")
    ap.add_argument("--file", help="从文件读文本（每行按一句诊断）")
    ap.add_argument("--audit", action="store_true", help="巡检全局词表")
    ap.add_argument("--env", action="store_true", help="只打印环境")
    ap.add_argument("--no-unk", action="store_true", help="跳过 unk 检查")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印每个中间阶段")
    # app.config 在导入时已用 parse_known_args 消费了 --data-dir，但不会把它从
    # argv 移除；这里补一个同名参数接收，否则 argparse 会报 unrecognized。
    ap.add_argument("--data-dir", default=None,
                    help="数据目录（真正的生效方是 app.config，此处仅为接收）")
    args = ap.parse_args()

    show_env()
    if args.env:
        return 0

    if args.audit:
        audit_glossary()
        return 0

    texts: list[str] = []
    if args.file:
        path = Path(args.file)
        if not path.exists():
            raise SystemExit(f"文件不存在: {path}")
        texts = [ln.rstrip("\n") for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(texts) > 50:
            print(f"\n!! 文件 {len(texts)} 行，只诊断前 50 行")
            texts = texts[:50]
    elif args.text:
        texts = [args.text]
    else:
        ap.print_help()
        return 1

    # 用合成路径的词表（含中点变体展开），与真实合成保持一致
    terms = load_glossary_for_synthesis(None)
    sp = None if args.no_unk else load_sp()
    if sp is None and not args.no_unk:
        print("\n（未找到 data/models/bpe.model，跳过 unk 检查；见 MEMORY 里的获取方式）")

    for idx, text in enumerate(texts, 1):
        section(f"[{idx}/{len(texts)}] {text[:60]}{'…' if len(text) > 60 else ''}")

        print("\n-- 看不见的字符 --")
        odd = scan_chars(text)
        if not odd:
            print("（无：汉字、ASCII 与常见中文标点）")

        print("\n-- 五道关 --")
        stage = run_chain(text, terms, None)
        show_chain(stage, verbose=args.verbose)

        risks = []
        if sp is not None:
            print("\n-- 危险的符号（本地 front.py 判据，云端引擎未知） --")
            risks = symbol_risks(sp, stage["final"])
            if not risks:
                print("（无：每个非 ASCII 字符都能在词表里找掉落点）")
            else:
                for ch, mapped, name in risks:
                    why = ("front.py 的字符表里没有它，原样进词表"
                           if mapped == ch else f"映射为 {mapped!r} 后仍未知")
                    print(f"  {ch!r} U+{ord(ch):04X}  {name} —— {why}")
                print("  模型在这些位置拿不到已知音素，读出什么是自由的。")
                print("  解法一：补一条词条把它换成汉字读音（如 9・11 → 九幺幺）")
                print("  解法二：改用表内形态（· U+00B7 会被 front.py 换成 '-'）")

        print()
        if risks:
            print("[!] 上面点名的符号是最可能的出错位置。")
        elif sp is not None and stage["after_front"] != text:
            print(f"[i] 送进引擎的是：「{stage['after_front'][:80]}」")
        elif sp is not None:
            print("[ok] 这句话没有会被读错的字符。")
        elif stage["after_front"] != text:
            print(f"[i] 送进引擎的是：「{stage['after_front'][:80]}」"
                  "（未做符号检查，结论不完整）")
        else:
            print("[i] 未加载 bpe.model，符号层面没做检查 —— 别把「无输出」当成「没问题」。")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
