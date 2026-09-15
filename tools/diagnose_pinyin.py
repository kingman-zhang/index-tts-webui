#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IndexTTS 多音字（拼音标注）链路诊断。

在 TTS 服务器上运行，用 index-tts 的 python 环境：

    cd /root/index-tts
    ./.venv/bin/python /path/to/diagnose_pinyin.py --text "让我们重头再来一遍"

不需要加载模型权重，只用到 checkpoints/bpe.model，跑完约几秒。

它按顺序回答四个问题：
  1. 这台服务器上的 front.py 支不支持拼音标注？
  2. 术语表的替换有没有真的发生？
  3. 替换后的文本过完 normalizer，拼音还在不在？
  4. 拼音有没有变成模型的输入 token？
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

# ── 与 indextts/utils/front.py 保持一致的静态副本（静态模式用）──────────────
PINYIN_TONE_PATTERN = (
    r"(?<![a-z])((?:[bpmfdtnlgkhjqxzcsryw]|[zcs]h)?"
    r"(?:[aeiouüv]|[ae]i|u[aio]|ao|ou|i[aue]|[uüv]e|[uvü]ang?|uai|"
    r"[aeiuv]n|[aeio]ng|ia[no]|i[ao]ng)|ng|er)([1-5])"
)
CJK_RANGE_PATTERN = (
    r"([\u1100-\u11ff\u2e80-\ua4cf\ua840-\uD7AF\uF900-\uFAFF"
    r"\uFE30-\uFE4F\uFF65-\uFFDC\U00020000-\U0002FFFF])"
)

RULE = "─" * 68


def hr(title):
    print(f"\n{RULE}\n{title}\n{RULE}")


def tokenize_by_cjk_char(line, do_upper_case=True):
    chars = re.split(CJK_RANGE_PATTERN, line.strip())
    return " ".join([w.strip().upper() if do_upper_case else w.strip()
                     for w in chars if w.strip()])


def save_pinyin_tones(text):
    """复刻 front.py：把 CHONG2 这类标注替换成 <pinyin_a> 占位符。"""
    lst = re.findall(re.compile(PINYIN_TONE_PATTERN, re.IGNORECASE), text)
    if not lst:
        return text, None
    lst = list(set("".join(p) for p in lst))
    out = text
    for i, py in enumerate(lst):
        out = out.replace(py, f"<pinyin_{chr(ord('a') + i)}>")
    return out, lst


def restore_pinyin_tones(text, lst):
    """复刻 front.py：把占位符还原成大写拼音。"""
    if not lst:
        return text
    out = text
    for i, py in enumerate(lst):
        out = out.replace(f"<pinyin_{chr(ord('a') + i)}>", py.upper())
    return out


def show(label, value):
    print(f"  {label:<22} {value!r}")


def main():
    ap = argparse.ArgumentParser(description="IndexTTS 拼音标注链路诊断")
    ap.add_argument("--indextts-home", default="/root/index-tts")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--text", default="让我们重头再来一遍")
    ap.add_argument("--static", action="store_true",
                    help="不导入 indextts，只做静态复刻检查")
    args = ap.parse_args()

    home = Path(args.indextts_home)
    model_dir = Path(args.model_dir or home / "checkpoints")
    front_py = home / "indextts" / "utils" / "front.py"
    bpe_path = model_dir / "bpe.model"
    glossary_path = Path(args.glossary) if args.glossary else None

    print(f"indextts-home : {home}")
    print(f"model-dir     : {model_dir}")
    print(f"术语表        : {glossary_path or '(未指定，跳过第 2 步)'}")
    print(f"待测文本      : {args.text!r}")

    # ── 1. 版本指纹 ────────────────────────────────────────────────────────
    hr("1) front.py 是否支持拼音标注")
    if not front_py.exists():
        print(f"  !! 找不到 {front_py}")
    else:
        src = front_py.read_text(encoding="utf-8", errors="replace")
        for name in ["PINYIN_TONE_PATTERN", "save_pinyin_tones",
                     "restore_pinyin_tones", "correct_pinyin",
                     "_protect_pronunciation_annotations"]:
            print(f"  {'有  ' if name in src else '没有'}  {name}")
        m = re.search(r"PINYIN_TONE_PATTERN\s*=\s*r?\"(.+?)\"\s*\n", src)
        if m:
            same = m.group(1) == PINYIN_TONE_PATTERN
            print(f"  正则与官方一致: {same}")
        # 实测：正则能不能认出 CHONG2
        rx = re.compile(PINYIN_TONE_PATTERN, re.IGNORECASE)
        for probe in ["CHONG2", "chong2", "CHONG2头"]:
            hit = re.findall(rx, probe)
            print(f"  正则识别 {probe!r}: {'是 ' + str(hit) if hit else '否'}")

    # ── 2. 术语表替换 ─────────────────────────────────────────────────────
    hr("2) 术语表替换是否发生")
    replaced = args.text
    if glossary_path is None:
        print("  跳过（未指定 --glossary）")
    elif not glossary_path.exists():
        print(f"  !! 术语表文件不存在: {glossary_path}")
    else:
        raw = json.loads(glossary_path.read_text(encoding="utf-8"))
        print(f"  共 {len(raw)} 条:")
        for t in raw:
            mark = "  <<< 命中" if str(t.get("original")) in args.text else ""
            print(f"    {t.get('original')!r} -> {t.get('replacement')!r}{mark}")
        for t in raw:
            replaced = replaced.replace(str(t.get("original")),
                                        str(t.get("replacement")))
        show("替换前", args.text)
        show("替换后", replaced)
        if replaced == args.text:
            print("  !! 文本未变化 —— 说明没有任何术语命中这段文本。")
            print("     （术语表没生效，问题在这一层，不在 TTS 服务端）")

    # ── 3. normalizer ─────────────────────────────────────────────────────
    hr("3) 过完 normalizer，拼音还在不在")
    normalizer = None
    if not args.static:
        sys.path.insert(0, str(home))
        try:
            import indextts.utils.front as front_mod  # noqa: PLC0415
            real_front = Path(front_mod.__file__).resolve()
            print(f"  实际 import 的 front.py : {real_front}")
            expected = front_py.resolve()
            if real_front != expected:
                print("  !! 与 --indextts-home 下的不是同一个文件！")
                print(f"     （期望 {expected}）")
                print("     venv 里可能装了另一份 indextts，盖住了源码目录。")
            from indextts.utils.front import TextNormalizer  # noqa: PLC0415
            normalizer = TextNormalizer(enable_glossary=True)
            normalizer.load()
            print("  已加载真实 TextNormalizer")
        except Exception as exc:
            print(f"  !! 导入 indextts 失败，退回静态复刻: {exc}")

    if normalizer is not None:
        for label, text in [("原文", args.text), ("替换后", replaced),
                            ("官方用例", "最zhong4要的是：不要chong2蹈覆辙")]:
            try:
                out = normalizer.normalize(text)
            except Exception as exc:
                out = f"<异常 {exc}>"
            show(f"normalize({label})", out)
            if "CHONG" in text.upper() and "CHONG" not in str(out).upper():
                print("     !! 拼音被 normalizer 吃掉了")
    else:
        saved, lst = save_pinyin_tones(replaced)
        show("保护后", saved)
        show("还原后", restore_pinyin_tones(saved, lst))
        print("  （静态模式不做真实 normalizer 处理，保护/还原链路如上）")

    # ── 4. BPE 分词 ───────────────────────────────────────────────────────
    hr("4) 拼音有没有变成模型输入 token")
    if not bpe_path.exists():
        print(f"  !! 找不到词表: {bpe_path}")
        return
    try:
        from sentencepiece import SentencePieceProcessor  # noqa: PLC0415
        sp = SentencePieceProcessor(model_file=str(bpe_path))
    except Exception as exc:
        print(f"  !! 加载词表失败: {exc}")
        return

    print(f"  词表大小: {sp.GetPieceSize()}")
    for probe in ["CHONG2", "ZHONG4"]:
        pid = sp.PieceToId(probe)
        print(f"  词表中 {probe}: {'id=%d' % pid if pid != sp.unk_id() else '不存在'}")

    normalized = replaced
    if normalizer is not None:
        try:
            normalized = normalizer.normalize(replaced)
        except Exception:
            pass
    pre = tokenize_by_cjk_char(normalized)
    pieces = sp.Encode(pre, out_type=str)
    ids = sp.Encode(pre, out_type=int)
    print(f"\n  文本: {normalized!r}")
    print(f"  预处理: {pre!r}")
    print(f"  tokens: {pieces}")
    print(f"  含unk: {sp.unk_id() in ids}")

    pinyin_tokens = [p for p in pieces
                     if re.match(PINYIN_TONE_PATTERN, p, re.IGNORECASE)]
    hr("结论")
    if replaced == args.text and glossary_path is not None:
        print("  ● 术语表没替换到这段文本 —— 先修术语表（原词要能在这个句子里原样找到）。")
    elif pinyin_tokens:
        print(f"  ● 链路是通的：拼音以 token {pinyin_tokens} 进入模型。")
        print("    如果听感仍不对，那就是模型对这类标注的渲染问题，不是链路问题。")
    else:
        print("  ● 拼音没有变成独立 token —— 它在这台服务器上不可用。")
        print("    请把上面第 3、4 步的原始输出发回，据此再定改法。")


if __name__ == "__main__":
    main()
