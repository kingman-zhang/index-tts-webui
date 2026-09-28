#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""词条 key 的「中点等价类展开」自测（stores 层）。

背景：中译外国人名的间隔号有 10 个 Unicode 变体，而词条匹配是逐字符精确的 ——
用户按一种写法配好词条，原文换成另一种写法就静默失效（实例：`9・11` U+30FB
配了词条，原文写成 `9·11` U+00B7 时不命中，且两侧是数字、name_punct 也不介入，
原样进引擎变 unk 怪音）。

在**临时数据目录**中运行，绝不触碰真实 data/：
    cd webui-backend && python tests/test_glossary_sep_variants.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# 必须在 import app.* 之前指定独立数据目录（app.config 在 import 时读取），
# 并清掉开关，确保测的是默认行为（默认开）。
_TMP = tempfile.mkdtemp(prefix="glossary_sep_test_")
os.environ["DATA_DIR"] = _TMP
os.environ.pop("GLOSSARY_SEP_VARIANTS", None)
os.environ.pop("GLOSSARY_SEP_VARIANTS_MAX", None)

from app import name_punct, stores  # noqa: E402

U30FB = "\u30fb"   # ・ 片假名中点（用户输入法常出这个）
U00B7 = "\u00b7"   # · 标准间隔号（front.py 唯一认识的那个）
U2027 = "\u2027"   # ‧ 连字点
UFF65 = "\uff65"   # ･ 半角片假名中点

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def pairs(terms: list) -> list:
    return [(t["original"], t["replacement"]) for t in terms]


def main() -> None:
    print(f"临时数据目录: {_TMP}\n")

    print("── 1. 变体枚举 ──")
    vs = stores.separator_variants(f"9{U30FB}11")
    check("单中点 → 9 个变体", len(vs) == 9, str(len(vs)))
    check("变体不含 key 自身", f"9{U30FB}11" not in vs)
    check("变体集合 == NAME_SEPARATORS（共 10 种写法）",
          len(set(vs) | {f"9{U30FB}11"}) == len(name_punct.NAME_SEPARATORS))
    check("无中点 → 无变体", stores.separator_variants("说服") == [])
    check("两个中点超上限 64 → 跳过",
          stores.separator_variants(f"A{U30FB}B{U30FB}C") == [])
    check("显式放宽 limit 后可展开 99 个",
          len(stores.separator_variants(f"A{U30FB}B{U30FB}C", limit=100)) == 99)

    print("\n── 2. 管理视角 vs 合成视角 ──")
    stores.save_global_glossary([
        {"original": f"9{U30FB}11", "replacement": "九幺幺"},
        {"original": "说服", "replacement": "说福"},
    ])
    admin = stores.load_glossary(None)
    synth = stores.load_glossary_for_synthesis(None)
    check("load_glossary 不展开（管理界面看到的就是用户写的）", len(admin) == 2, str(pairs(admin)))
    check("load_glossary_for_synthesis 展开", len(synth) == 11, str(len(synth)))
    check("不含中点的词条不产生变体",
          sum(1 for t in synth if t["original"] == "说服") == 1)
    idx = [i for i, t in enumerate(synth) if t["original"] == f"9{U30FB}11"][0]
    tail = [t["original"] for t in synth[idx + 1: idx + 10]]
    check("9 个变体紧随原条目之后（保持相对顺序）",
          len(tail) == 9 and all(x.startswith("9") and x.endswith("11") for x in tail), str(tail))
    check("变体继承同一个 replacement",
          all(t["replacement"] == "九幺幺" for t in synth[idx: idx + 10]))

    print("\n── 3. 显式写下的 key 优先，不被变体覆盖 ──")
    stores.save_user_glossary("u_1", [{"original": f"9{U00B7}11", "replacement": "九一一"}])
    synth = stores.load_glossary_for_synthesis("u_1")
    hits = [(t["original"], t["replacement"]) for t in synth
            if t["original"] in (f"9{U00B7}11", f"9{U30FB}11")]
    check("用户显式写的变体保留自己的替换值",
          (f"9{U00B7}11", "九一一") in hits, str(hits))
    check("该 key 不出现重复条目", len([1 for o, _r in hits if o == f"9{U00B7}11"]) == 1)
    check("原条目仍是全局库的值", (f"9{U30FB}11", "九幺幺") in hits, str(hits))

    print("\n── 4. 用户覆盖整条时，变体跟着走 ──")
    stores.save_user_glossary("u_2", [{"original": f"9{U30FB}11", "replacement": "九一一一"}])
    synth2 = stores.load_glossary_for_synthesis("u_2")
    grp = [t["replacement"] for t in synth2 if t["original"].startswith("9") and t["original"].endswith("11")]
    check("全部 10 种写法都用用户库的值", len(grp) == 10 and set(grp) == {"九一一一"}, str(grp))

    print("\n── 5. 空替换（停用/删除）不展开 ──")
    stores.save_global_glossary([{"original": f"9{U30FB}11", "replacement": ""}])
    m = stores.load_glossary_for_synthesis(None)
    check("空 replacement 不生成变体（否则会把 9·11 整段删掉）", len(m) == 1, str(pairs(m)))

    print("\n── 6. 半角减号 `-` 刻意不纳入变体 ──")
    check("`-` 会破坏 2020-2025 这类范围写法，不自动展开",
          "-" not in name_punct.NAME_SEPARATORS)
    check("9-11 不是 9・11 的变体", f"9-11" not in stores.separator_variants(f"9{U30FB}11"))

    print("\n── 7. 端到端：10 种写法都能命中并替换 ──")
    stores.save_global_glossary([{"original": f"9{U30FB}11", "replacement": "九幺幺"}])
    terms = stores.load_glossary_for_synthesis(None)
    bad = []
    for sep in name_punct.NAME_SEPARATORS:
        text = f"那年发生了9{sep}11事件"
        out = stores.apply_glossary([{"text": text}], terms)[0]["text"]
        if out != "那年发生了九幺幺事件":
            bad.append((hex(ord(sep)), out))
    check(f"{len(name_punct.NAME_SEPARATORS)} 种中点写法全部替换为「九幺幺」", not bad, str(bad))

    print("\n── 8. 开关与 source 透传 ──")
    stores.GLOSSARY_SEP_VARIANTS = False
    try:
        check("GLOSSARY_SEP_VARIANTS=False 时不展开",
              len(stores.load_glossary_for_synthesis(None)) == 1)
    finally:
        stores.GLOSSARY_SEP_VARIANTS = True

    with_src = stores.load_glossary_for_synthesis(None, with_source=True)
    variants = [t for t in with_src if t["original"] != f"9{U30FB}11"]
    check("变体条目透传 source", len(variants) == 9
          and all(t.get("source") == "global" for t in variants), str(variants[:2]))

    print("\n── 9. 不修改入参 ──")
    src = [{"original": f"9{U30FB}11", "replacement": "九幺幺"}]
    out = stores.expand_separator_variants(src)
    check("返回新列表", out is not src)
    check("入参未被改动", len(src) == 1)

    print(f"\n{'=' * 60}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 60}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
