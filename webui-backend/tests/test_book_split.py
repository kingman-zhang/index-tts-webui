#!/usr/bin/env python3
"""书本自动分章算法单测（纯函数，不启服务、不连 TTS、不装依赖）。

锁定 `app/book_split.split_into_chapters()` 的核心承诺：**只在行边界切分**。
这条不是审美偏好 —— 配音画布按行解析标记文本，行内的 `[pause:N]` 与
`【情绪】` 作用域都从属于某一行。一旦切点落进行内，停顿标记会被截断成
半截（`[pause:` 变成正文念出来）、情绪作用域会跨章串味。

因此最关键的一组断言是「按行号还原出的章节正文 = 原文的连续切片」，
它把「只切行边界」变成可回归的硬约束，而不是代码注释里的口头承诺。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.book_split import (  # noqa: E402
    DEFAULT_CHAPTER_MAX_CHARS,
    count_chars,
    split_into_chapters,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


def body(text: str, ch: dict) -> str:
    """按行号还原章节正文（与前端 chapterTextOf 同一算法）。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(lines[ch["start_line"]:ch["end_line"]])


def para(n: int, prefix: str = "字") -> str:
    return prefix * n


def main():
    global PASS, FAIL

    print("── 计数口径与空输入 ──")
    check("空文本 → 无章节", split_into_chapters("") == [])
    check("纯空白 → 无章节", split_into_chapters("\n\n   \n\t\n") == [])
    check("count_chars 不计空白", count_chars(" 你 好\n世界 ") == 4)
    check("CRLF 归一后可切分", len(split_into_chapters("第一章\r\n正文\r\n第二章\r\n正文")) == 2)

    print("\n── 标题识别：三种形态 ──")
    ch = split_into_chapters("# 第一章\n正文甲\n## 第二节\n正文乙")
    check("Markdown 标题切出 2 章", len(ch) == 2, f"got {len(ch)}")
    check("Markdown 标题取标题文本", ch[0]["title"] == "# 第一章", f"got {ch[0]['title']!r}")

    ch = split_into_chapters("第一章 童年\n正文甲\n\n第十二回 归来\n正文乙\n\n第三卷 冬\n正文丙")
    check("第X章/回/卷 切出 3 章", len(ch) == 3, f"got {len(ch)}")
    check("中文数字标题可用", ch[1]["title"] == "第十二回 归来", f"got {ch[1]['title']!r}")

    ch = split_into_chapters("Chapter 1\nbody one\n\nChapter 2\nbody two")
    check("Chapter N 切出 2 章", len(ch) == 2, f"got {len(ch)}")
    check("PART II 形态可用", len(split_into_chapters("PART I\na\n\nPART II\nb")) == 2)

    print("\n── 标题误判防护（长句 / 句子形态的正文）──")
    long_body = "第一章讲述了一个少年从故乡出发，历经磨难终于回到母亲身边的故事。"
    check(
        "长句不误判为标题（长度闸门）",
        len(split_into_chapters(long_body, max_chars=0)) == 1,
        f"got {len(split_into_chapters(long_body, max_chars=0))}",
    )
    short_ok = para(60) + "\n" + "第一章是我最喜欢的" + "\n" + para(60)
    check(
        "短行以「第X章」开头 ⇒ 仍认作标题（不做过度猜测）",
        len(split_into_chapters(short_ok, max_chars=0)) == 2,
        f"got {len(split_into_chapters(short_ok, max_chars=0))}",
    )
    short_no = para(60) + "\n" + "第一章是我最喜欢的。" + "\n" + para(60)
    check(
        "同句加句号 ⇒ 判为正文而非标题",
        len(split_into_chapters(short_no, max_chars=0)) == 1,
        f"got {len(split_into_chapters(short_no, max_chars=0))}",
    )

    print("\n── 卷首内容独立成章 ──")
    ch = split_into_chapters("序言\n这是作者的话。\n\n第一章 起点\n正文")
    check("「序言」独立成章", ch[0]["title"] == "序言", f"got {ch[0]['title']!r}")
    check("卷首字数正确", ch[0]["chars"] == count_chars("序言这是作者的话。"), f"got {ch[0]['chars']}")
    check("其后为第一章", ch[1]["title"] == "第一章 起点", f"got {ch[1]['title']!r}")

    ch = split_into_chapters("写在前面的话\n\n第一章 起点\n正文")
    check("无语义卷首标为「卷首」", ch[0]["title"] == "卷首", f"got {ch[0]['title']!r}")

    print("\n── 无标题 ⇒ 按字数切，切点落在行边界 ──")
    plain = "\n".join(para(300, "甲") for _ in range(10))  # 10 行 × 300 字 = 3000 字
    ch = split_into_chapters(plain, max_chars=1000)
    check("贪心切满：每块 3 行、最后 1 行", [c["end_line"] - c["start_line"] for c in ch] == [3, 3, 3, 1],
          f"got {[c['end_line'] - c['start_line'] for c in ch]}")
    check("每块字数 ≤ 上限", all(c["chars"] <= 1000 for c in ch), f"{[c['chars'] for c in ch]}")
    check("块标题带 k/n 后缀", ch[1]["title"] == "分段 2/4", f"got {ch[1]['title']!r}")
    check(
        "还原后与按行切片一致",
        body(plain, ch[0]) == "\n".join([para(300, "甲")] * 3)
        and body(plain, ch[3]) == para(300, "甲"),
    )

    print("\n── 标题章超上限 ⇒ 章内再切 ──")
    big = "第一章 长夜\n" + "\n".join(para(400, "乙") for _ in range(10))
    ch = split_into_chapters(big, max_chars=1000)
    check("超大标题章被再切", len(ch) > 1, f"got {len(ch)}")
    check("标题带（k/n）后缀", "（1/" in ch[0]["title"], f"got {ch[0]['title']!r}")
    check("后缀保留原标题", ch[0]["title"].startswith("第一章 长夜"), f"got {ch[0]['title']!r}")
    check("再切后每块 ≤ 上限", all(c["chars"] <= 1000 for c in ch), f"{[c['chars'] for c in ch]}")

    print("\n── max_chars<=0 ⇒ 只在标题处切 ──")
    only_heads = "第一章\n" + para(20000, "甲") + "\n第二章\n" + para(20000, "乙")
    ch = split_into_chapters(only_heads, max_chars=0)
    check("不按字数兜底，只切 2 章", len(ch) == 2, f"got {len(ch)}")
    check("章内可超默认上限", ch[0]["chars"] > DEFAULT_CHAPTER_MAX_CHARS, f"got {ch[0]['chars']}")

    print("\n── 🔒 行边界不被破坏（本文件的核心承诺）──")
    tricky = "\n".join([
        "第一章 起",
        "你好[pause:0.5]世界【喜悦】今天真好【/】。",
        "第二段没有标记。",
        "第二章 承",
        "他又说：[pause: 1]「走吧」【平静】好。",
    ])
    ch = split_into_chapters(tricky, max_chars=12)  # 故意切得极碎
    joined = [body(tricky, c) for c in ch]
    check("碎切后仍成多章", len(ch) > 2, f"got {len(ch)}")
    check("每章正文是原文的连续切片", all(b in tricky for b in joined), f"joined={joined}")
    check(
        "带标记的整行完整归属某一章（无截断）",
        any("你好[pause:0.5]世界【喜悦】今天真好【/】。" in b for b in joined),
    )
    check(
        "[pause] 标记只出现一次、未跨章复制",
        sum(b.count("[pause:0.5]") for b in joined) == 1,
        f"got {sum(b.count('[pause:0.5]') for b in joined)}",
    )
    check("情绪标记同样只归属一章", sum(b.count("【喜悦】") for b in joined) == 1)
    check("行内停顿的冒号空格原样保留", any("[pause: 1]" in b for b in joined))

    print("\n── 行覆盖完整性不变式 ──")
    lines = tricky.split("\n")
    covered = set()
    for c in ch:
        covered.update(range(c["start_line"], c["end_line"]))
    check(
        "每一行要么属于某章、要么是空行（无行被静默丢弃）",
        all(i in covered or not lines[i].strip() for i in range(len(lines))),
    )
    check("章节按行号递增且不重叠", all(ch[i]["end_line"] <= ch[i + 1]["start_line"] for i in range(len(ch) - 1)))
    check("index 连续从 0 开始", [c["index"] for c in ch] == list(range(len(ch))))

    print("\n── 还原一致性：与原文逐字符对齐 ──")
    book = "\n".join([
        "前言",
        "这是前言内容，说明写作缘起。",
        "",
        "第一章 出发",
        "他背起行囊[pause:0.5]走出了门。",
        para(200, "丙"),
        "",
        "第二章 抵达",
        para(200, "丁"),
    ])
    ch = split_into_chapters(book, max_chars=100)
    rebuilt = "\n".join(body(book, c) for c in ch)
    check(
        "拼接后覆盖原文全部非空行（字数相等）",
        count_chars(rebuilt) == count_chars(book),
        f"{count_chars(rebuilt)} vs {count_chars(book)}",
    )

    print("\n── 真实规模抽样（不依赖外部文件）──")
    fake_book = "\n".join(
        f"第{i + 1}章 第{i + 1}节\n" + "\n".join(para(120, "文") for _ in range(40))
        for i in range(30)
    )
    ch = split_into_chapters(fake_book, max_chars=10_000)
    check("30 章 / 每章约 4800 字 ⇒ 切成 30 章", len(ch) == 30, f"got {len(ch)}")
    check("总字数守恒", sum(c["chars"] for c in ch) == count_chars(fake_book),
          f"{sum(c['chars'] for c in ch)} vs {count_chars(fake_book)}")
    check("每章都带原标题", ch[5]["title"].startswith("第6章 第6节"), f"got {ch[5]['title']!r}")

    print(f"\n{'=' * 56}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 56}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
