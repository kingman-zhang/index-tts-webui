#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人名分隔号归一化（app/name_punct.py）自测。

用法（在 webui-backend 下）：
    python tests/test_name_punct.py

三部分：
  1. 文本层：每个变体都被收敛、每种「不像人名」的用法都不被误伤
  2. 不变性：不改入参、非 dict 行原样透传、ENABLED=0 时整体关闭
  3. token 层（可选）：能找到 bpe.model 时，验证归一化确实**减少了 unk**
     —— 这是本模块存在的理由，光看字符串相等不够。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import name_punct  # noqa: E402
from app.name_punct import (  # noqa: E402
    ENABLED,
    NAME_SEPARATORS,
    TARGET,
    apply_name_separator_rules,
    normalize_name_separators,
)

PASS = FAIL = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    ok = actual == expected
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         expect: {expected!r}\n         actual: {actual!r}")


# (原文, 期望输出, 说明) —— 默认 target=drop
CASES: list[tuple[str, str, str]] = [
    # —— 变体全部收敛 ——
    ("法国哲学家保罗\u30fb萨特说过", "法国哲学家保罗萨特说过", "U+30FB 片假名中点（用户报的那种）"),
    ("保罗\u00b7萨特", "保罗萨特", "U+00B7 中文间隔号"),
    ("保罗\u2022萨特", "保罗萨特", "U+2022 项目符号"),
    ("保罗\u2027萨特", "保罗萨特", "U+2027 连字点"),
    ("保罗\u2219萨特", "保罗萨特", "U+2219 项目符号运算符"),
    ("保罗\u22c5萨特", "保罗萨特", "U+22C5 点运算符"),
    ("保罗\uff65萨特", "保罗萨特", "U+FF65 半角片假名中点"),
    ("保罗\u0387萨特", "保罗萨特", "U+0387 希腊分号点"),
    # —— 真实人名 ——
    ("克里斯托弗\u30fb诺兰", "克里斯托弗诺兰", "三字+中点+两字"),
    ("约瑟夫\u30fb高登-莱维特", "约瑟夫高登-莱维特", "连字符不动，只删中点"),
    ("J\u30fbK\u30fb罗琳", "JK罗琳", "连续多个分隔号"),
    ("让-保罗\u30fb萨特", "让-保罗萨特", "前缀连字符 + 中点"),
    ("保罗 \u30fb 萨特", "保罗萨特", "分隔号两侧带空格，一并吞掉"),
    ("保罗\u30fb萨特\u30fb", "保罗萨特\u30fb", "行尾悬挂的点不动（不像是人名分隔）"),
    # —— 刻意不动：不像人名分隔号 ——
    ("\u2022 第一点", "\u2022 第一点", "行首项目符号"),
    ("\u2022 保罗\u30fb萨特", "\u2022 保罗萨特", "行首符号不动，句中分隔号照删"),
    ("结束了\uff0e下一段", "结束了\uff0e下一段", "全角句点 U+FF0E 不碰"),
    ("结束了\u2024下一段", "结束了\u2024下一段", "单点前导 U+2024 不碰"),
    ("三点\u2026\u2026", "三点\u2026\u2026", "省略号不碰"),
    ("没有分隔号的普通句子", "没有分隔号的普通句子", "无分隔号 → 恒等"),
    ("", "", "空串"),
]


def main() -> int:
    print(f"NAME_PUNCT_NORMALIZE 生效状态: ENABLED={ENABLED}  TARGET={TARGET}\n")

    print("── 1. 文本层（默认 drop）──")
    for src, expected, why in CASES:
        check(f"{src!r}  （{why}）", normalize_name_separators(src, "drop"), expected)

    print("\n── 2. 其它目标形态 ──")
    check("target=space", normalize_name_separators("保罗\u30fb萨特", "space"), "保罗 萨特")
    check("target=dot（交回 front.py 变 '-'）",
          normalize_name_separators("保罗\u30fb萨特", "dot"), "保罗\u00b7萨特")
    check("target=pause（本地会变 ',' → 真停顿）",
          normalize_name_separators("保罗\u30fb萨特", "pause"), "保罗\u3001萨特")
    try:
        normalize_name_separators("保罗\u30fb萨特", "bogus")
        check("非法 target 抛错", "没抛错", "抛 ValueError")
    except ValueError:
        check("非法 target 抛错", "ValueError", "ValueError")

    print("\n── 3. lines 层：不改入参、透传非 dict ──")
    src_lines = [{"text": "保罗\u30fb萨特", "speaker": "A"}, {"text": "普通行"}, "raw-string"]
    snapshot = [dict(x) if isinstance(x, dict) else x for x in src_lines]
    out = apply_name_separator_rules(src_lines)
    check("返回新列表", out is not src_lines, True)
    check("入参未被修改", src_lines, snapshot)
    check("text 已归一化", out[0]["text"], "保罗萨特")
    check("其它字段保留", out[0]["speaker"], "A")
    check("非 dict 行原样透传", out[2], "raw-string")
    check("空 text 不炸", apply_name_separator_rules([{"text": ""}])[0]["text"], "")

    print("\n── 4. 开关关闭时整体旁路 ──")
    saved = name_punct.ENABLED
    try:
        name_punct.ENABLED = False
        same = [{"text": "保罗\u30fb萨特"}]
        check("ENABLED=False → 原样返回（同一对象）",
              apply_name_separator_rules(same) is same, True)
    finally:
        name_punct.ENABLED = saved

    print("\n── 5. token 层：归一化是否真的消掉了 unk ──")
    run_unk_check()

    print(f"\n{'=' * 60}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


def _find_bpe() -> Path | None:
    """找 bpe.model：环境变量 → backend/data/models → 上游仓库根。"""
    here = Path(__file__).resolve().parents[1]
    cands = [
        os.environ.get("BPE_MODEL", ""),
        str(here / "data" / "models" / "bpe.model"),
        str(here.parents[0] / "index-tts-main" / "bpe.model"),
        str(here.parents[0] / "index-tts-main" / "checkpoints" / "bpe.model"),
    ]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    return None


def run_unk_check() -> None:
    """用 sentencepiece 直接编码，比较归一化前后的 unk 个数。

    为什么不能用「和 expect 字符串比」代替：本模块的存在理由是**消掉 unk**，
    字符串相等只是手段。这里不做完整 front.normalize（那要 torch stub），
    只验证最核心的一条：分隔号字符确实进了 <unk>。
    """
    bpe = _find_bpe()
    if bpe is None:
        print("  [SKIP] 未找到 bpe.model（可设 BPE_MODEL=... 指定），跳过 token 层校验")
        return
    try:
        from sentencepiece import SentencePieceProcessor
    except ImportError:
        print("  [SKIP] 未安装 sentencepiece，跳过 token 层校验")
        return

    sp = SentencePieceProcessor(model_file=str(bpe))
    unk = sp.unk_id()
    print(f"  bpe.model = {bpe}  unk_id={unk}  词表={sp.GetPieceSize()}")

    # 注：完整链路里全角逗号等会被 front.py 的 char_rep_map 先换掉，
    # 这里直连 sp 会带来一个恒定基线 unk，所以只比较「相对变化」。
    for sep_name, sep in [
        ("U+30FB \u30fb", "\u30fb"),
        ("U+00B7 \u00b7", "\u00b7"),
        ("U+2022 \u2022", "\u2022"),
        ("U+2027 \u2027", "\u2027"),
        ("U+FF65 \uff65", "\uff65"),
    ]:
        raw = f"法国哲学家保罗{sep}萨特说过"
        fixed = normalize_name_separators(raw, "drop")
        raw_unk = sp.Encode(raw, out_type=int).count(unk)
        fixed_unk = sp.Encode(fixed, out_type=int).count(unk)
        check(f"{sep_name}：unk {raw_unk} → {fixed_unk}（归一化后应减少）",
              fixed_unk < raw_unk, True)

    # 反向：不含分隔号的句子不该被本模块改动
    plain = "法国哲学家保罗萨特说过"
    check("无分隔号句 unk 数不变",
          sp.Encode(normalize_name_separators(plain, "drop"), out_type=int).count(unk),
          sp.Encode(plain, out_type=int).count(unk))


if __name__ == "__main__":
    raise SystemExit(main())
