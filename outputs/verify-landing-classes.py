#!/usr/bin/env python3
"""落地页「死类」检查：找出写了但 Tailwind 根本没生成的类名。

为什么要这个脚本
---------------
Tailwind 是**按需扫描源码生成 CSS**的：类名写错一个字符（`h-4.5`、`border-white/12`）
不会报任何错，只是这条样式凭空消失 —— 页面上表现为「这里怎么没边框/图标怎么变大了」，
而且**本地看着也大概率正常**（因为旁边别的类还在撑着）。
项目里已经有实例：`Header.tsx` 的 `w-4.5 h-4.5` 从来没生效过。

用法（在 webui-frontend/ 下）：
    ~/.workbuddy/binaries/python/envs/default/bin/python ../../outputs/verify-landing-classes.py

做法：先让 Tailwind CLI 针对落地页产出一份 CSS，再把源码里出现的每个类名
逐个到 CSS 里查（按 Tailwind 的转义规则还原），查不到的就报出来。
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FE = HERE.parent / "webui-frontend"
if not FE.exists():  # 脚本被挪到别处时的兜底
    FE = Path.cwd()

TARGETS = [FE / "src" / "pages" / "LandingPage.tsx"]
# 伪类/状态选择器在 CSS 里的写法不同（:hover 会被转义），这些前缀直接跳过
SKIP_PREFIX = ("group-", "peer-", "data-", "aria-", "[&", "dark:")
# 误报：这不是类名，是 import 的模块路径（抽取器只看「像类名列表的字符串」）
IGNORE = {"lucide-react"}


def escape(cls: str) -> str:
    """按 Tailwind 的规则转义类名，用于在生成的 CSS 里查找。

    ⚠️ 逗号不是 `\\,` 而是 `\\2c `（CSS 十六进制转义 + 一个空格）——
    这条是实测出来的：`shadow-[0_0_40px_-8px_rgba(16,185,129,0.75)]`
    在产物里写作 `shadow-\\[0_0_40px_-8px_rgba\\(16\\2c 185\\2c 129\\2c 0\\.75\\)\\]`。
    """
    out = []
    for ch in cls:
        if ch == ",":
            out.append("\\2c ")
        elif ch in ".:/[]()%#!*+~'\"<>=":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def collect_classes(tsx_text: str) -> set[str]:
    """粗略抽取 JSX 里的类名：取所有「像类名列表」的字符串字面量再按空白切分。"""
    tokens: set[str] = set()
    for literal in re.findall(r'"([^"\n]*)"|\'([^\'\n]*)\'', tsx_text):
        s = literal[0] or literal[1]
        if not s or len(s) > 400:
            continue
        if not re.fullmatch(r"[A-Za-z0-9\-\[\]\/\.\:\(\)%,_\s#!$&>='\"~+]+", s):
            continue
        for t in s.split():
            # 只要「看起来像工具类」的：至少有一个 - 或 : 或 [ ，或 8 个常见单词之一
            if t in {"flex", "block", "hidden", "grid", "relative", "absolute", "sticky",
                     "fixed", "inline-flex", "contents", "truncate", "antialiased",
                     "overflow-hidden", "antialiased"}:
                tokens.add(t)
            elif re.search(r"[-:\[]", t) and not t.startswith(("http", "/", "{", "$")):
                tokens.add(t)
    return tokens


def main() -> int:
    out_css = Path(tempfile.mkdtemp()) / "tw.css"
    # ⚠️ `--content` 只能给一个；给两个会**互相覆盖**（踩过：只剩最后一个，
    #    于是整份 CSS 几乎是空的、364 个类名报 352 个缺失）。
    cmd = (f"{FE}/node_modules/.bin/tailwindcss -i src/index.css -o {out_css} "
           f"--content './src/pages/LandingPage.tsx'")
    r = subprocess.run(cmd, cwd=FE, shell=True, capture_output=True, text=True)
    if r.returncode != 0 or not out_css.exists():
        print("Tailwind CLI 执行失败：\n" + (r.stderr or r.stdout))
        return 2
    css = out_css.read_text(encoding="utf-8")

    tokens: set[str] = set()
    for p in TARGETS:
        tokens |= collect_classes(p.read_text(encoding="utf-8"))

    missing = []
    for t in sorted(tokens):
        if t.startswith(SKIP_PREFIX) or t in IGNORE:
            continue
        # 变体（响应式/状态）在 CSS 里以完整形式出现，直接整体查
        probe = escape(t)
        if f".{probe}" in css or f".{probe}:" in css or f".{probe}," in css or f"{probe}{{" in css:
            continue
        # 任意值里的点/斜杠转义方式可能与上面不同，兜底再查一次原始串
        if t in css:
            continue
        missing.append(t)

    print(f"检查 {len(tokens)} 个类名，未生成 {len(missing)} 个")
    for m in missing:
        print("  ✗", m)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
