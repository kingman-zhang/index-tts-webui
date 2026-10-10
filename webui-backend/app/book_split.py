"""书本导入的自动分章：只按「行边界」切分，绝不切进行内。

为什么这条约束是硬的
------------------
配音画布的唯一真源是**按行解析**的标记文本（`textToMonoLines` 用
`text.split(/\\r?\\n/)` 切行）。行内的 `[pause:N]` 停顿芯片与 `【情绪】` 作用域都
**属于某一行**：只要切点落在行与行之间，行内结构就不可能被切断。

于是本模块只需要回答一个问题——「哪些行属于同一章」——而完全不必理解
行内语法。这是现有模型白送的红利，也是分章正确的充分条件。

切分顺序
--------
1. 结构化标题行：Markdown `#` / `第X章·回·节·卷·篇·部·集` / `Chapter N`、
   `Part N`、`Book N` / 独立成行的「序言·前言·后记·附录」类关键词。
2. 一个标题都没有 ⇒ 整篇按字数贪心切，切点仍只落在行边界。
3. 标题章仍超过单章上限 ⇒ 在该章内部再按行累计切，标题加「（k/n）」后缀。

返回的行号是 `text.split("\\n")` 的下标；前端拿同一份 text 用行号还原章节正文，
因此预览与真正送进合成的文本**逐字符一致**。

已知局限
--------
若整篇文本是**没有任何换行的单一长块**（个别 PDF 抽取结果会这样），行边界
不存在 ⇒ 只能作为一整章交给下游 `engines/chunker` 按引擎上限切。
这是数据形态问题，不在本层用句子切分去兜 —— 那会引入「行内切分」，
把上面那条硬约束打破。
"""

from __future__ import annotations

import re

# 标题行长度上限：超过它就不可能是标题，而是「第一章讲述了……」这种正文。
MAX_TITLE_LEN = 50

# 单章字数上限默认值（可用环境变量 MONO_CHAPTER_MAX_CHARS 覆盖）。
# 2026-10-10 由 1 万放宽到 2 万：这个数原本是给「整章音频全量驻留内存」设的保险
# （单章 1 万字 ≈ 100 MB 音频、峰值 300~400 MB）。G4 流式落盘之后内存已与章节大小
# 解耦，上限因此**退化为纯粹的「任务/交付粒度」旋钮**，不再受内存约束 ⇒ 放宽。
DEFAULT_CHAPTER_MAX_CHARS = 20_000

_CN_NUM = "0-9零一二三四五六七八九十百千两"

_MD_HEAD_RE = re.compile(r"^#{1,6}\s+\S")                       # Markdown 标题
_CN_HEAD_RE = re.compile(
    r"^第\s*[" + _CN_NUM + r"]{1,10}\s*[章回节卷篇部集]"          # 第 1 章 / 第十二回
)
_EN_HEAD_RE = re.compile(
    r"^(?:chapter|part|book)\s+[0-9ivxlc]+", re.IGNORECASE        # Chapter 3 / PART II
)

# 编号后允许紧跟的分隔符；紧跟的不是这些、且整行是句子 ⇒ 判为正文。
_TAIL_OK = " \t、：:·.-—～"

# 独立成行的篇章关键词（「序言」「后记」这类没有编号的标题）
_STANDALONE_HEADS = (
    "序言", "自序", "代序", "前言", "引子", "楔子", "后记", "尾声",
    "结语", "附录", "导读", "序", "跋",
)

_WHITESPACE_RE = re.compile(r"\s")
# 正文句末标点：出现即说明该行是正文而非标题
_SENTENCE_MARKS = "。！？；"


def count_chars(text: str) -> int:
    """计数字数（不计空白），与 `routes/mono.py` 的导入口径一致。"""
    return len(_WHITESPACE_RE.sub("", text))


def _is_heading(line: str) -> bool:
    """判断一行是否为章节标题。

    两道具象闸门，都是为了挡住「长得像标题的正文」：
      - **长度**：`第一章讲述了他的一生，从故乡出发……` 长句直接出局；
      - **标记后紧跟文字且整行是句子**：`第一章是我最喜欢的。` 命中「第X章」
        正则却是正文 —— 编号后若直接跟实词且行内有句末标点，就不认它是标题。
        反过来 `第一章 出发`（空格）、`第一章：出发`（冒号）都正常识别。
    """
    s = line.strip()
    if not s or len(s) > MAX_TITLE_LEN:
        return False
    if _MD_HEAD_RE.match(s):
        return True
    for rx in (_CN_HEAD_RE, _EN_HEAD_RE):
        m = rx.match(s)
        if m is None:
            continue
        rest = s[m.end():]
        if rest and rest[0] not in _TAIL_OK and any(c in s for c in _SENTENCE_MARKS):
            continue  # 标记后直接续正文且是完整句子 ⇒ 不是标题
        return True
    if len(s) <= 20 and not any(c in s for c in _SENTENCE_MARKS):
        return any(s.startswith(kw) for kw in _STANDALONE_HEADS)
    return False


def _guess_title(lines: list[str], start: int, end: int) -> str:
    """无标题块：拿首个非空行的前 24 字当标题，方便用户辨认。"""
    for i in range(start, end):
        s = lines[i].strip()
        if s:
            return s[:24]
    return "（空）"


def _split_chunk(lines: list[str], start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """把 [start, end) 按行累计切成若干 ≤ max_chars 的块（切点都在行边界）。

    单行自身就超上限时不做任何事 —— 它自己占一块，交给下游 chunker。
    """
    if max_chars <= 0:
        return [(start, end)]
    segs: list[tuple[int, int]] = []
    seg_start, acc = start, 0
    for i in range(start, end):
        n = count_chars(lines[i])
        if acc and acc + n > max_chars:
            segs.append((seg_start, i))
            seg_start, acc = i, n
        else:
            acc += n
    segs.append((seg_start, end))
    # 丢掉纯空白块（如两个标题之间的空行区）
    return [(a, b) for a, b in segs if any(lines[x].strip() for x in range(a, b))]


def split_into_chapters(
    text: str, max_chars: int = DEFAULT_CHAPTER_MAX_CHARS
) -> list[dict]:
    """把整篇文本切成章节列表。

    返回 `[{index, title, start_line, end_line, chars}]`；`start_line`/`end_line`
    是 `text.split("\\n")` 的半开区间下标，正文 = `"\\n".join(lines[start:end])`。

    `max_chars <= 0` 表示只在标题处切、不做字数兜底。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    heads = [i for i, ln in enumerate(lines) if _is_heading(ln)]

    if heads:
        chunks: list[tuple[str | None, int, int]] = []
        if heads[0] > 0:
            chunks.append(("卷首", 0, heads[0]))  # 第一个标题之前的内容（序/前言/引子）
        for k, h in enumerate(heads):
            nxt = heads[k + 1] if k + 1 < len(heads) else len(lines)
            chunks.append((lines[h].strip(), h, nxt))
    else:
        chunks = [(None, 0, len(lines))]

    result: list[dict] = []
    for title, start, end in chunks:
        segs = _split_chunk(lines, start, end, max_chars)
        if len(segs) <= 1:
            pairs: list[tuple[str | None, int, int]] = [(title, start, end)]
        elif title:
            pairs = [
                (f"{title}（{k + 1}/{len(segs)}）", a, b)
                for k, (a, b) in enumerate(segs)
            ]
        else:
            pairs = [
                (f"分段 {k + 1}/{len(segs)}", a, b)
                for k, (a, b) in enumerate(segs)
            ]
        for t, a, b in pairs:
            chars = count_chars("\n".join(lines[a:b]))
            if chars == 0:
                continue
            result.append(
                {
                    "index": len(result),
                    "title": (t or _guess_title(lines, a, b)).strip(),
                    "start_line": a,
                    "end_line": b,
                    "chars": chars,
                }
            )
    return result
