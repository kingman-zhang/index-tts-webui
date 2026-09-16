"""文本分片器：把长文本切成满足各引擎单次上限的片段。

- autodl.art：单次 ≤2048 字符且按次计费 ⇒ 贪心切满片（切得越满越省）。
- 自建引擎无硬限制，但长文本按 `max_text_tokens_per_segment` 由模型侧分段，
  无需在此处理。
"""

from __future__ import annotations

import re

# autodl.art 单次提交上限（与 engines/indextts_art.py 保持一致）
ART_MAX_CHARS = 2048

# 句读优先级：句号/问叹号 > 分号冒号 > 逗号顿号 > 空格
_BREAK_PATTERN = re.compile(r"(?<=[。！？!?；;：:，,、\s])")


def _char_count(text: str) -> int:
    """计费口径的字符数：1 汉字 = 2 字符，其他 = 1 字符（与商用 API 口径一致）。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def split_for_art(text: str, max_chars: int = ART_MAX_CHARS) -> list[str]:
    """把文本贪心切分为每个计费字符数 ≤ max_chars 的片段。

    策略：尽量在句子边界切，单句超限时在句读处切，再超限则硬切。
    保证返回片段的 _char_count 均 ≤ max_chars（硬切时按 UTF-8 安全截断）。
    """
    if max_chars < 20:
        raise ValueError("max_chars 过小")
    if _char_count(text) <= max_chars:
        return [text] if text.strip() else []

    segments: list[str] = []
    remaining = text.strip()

    def _hard_cut(chunk: str) -> list[str]:
        out = []
        buf = ""
        for ch in chunk:
            if _char_count(buf) + (2 if ord(ch) > 0x2E7F else 1) > max_chars:
                out.append(buf)
                buf = ch
            else:
                buf += ch
        if buf:
            out.append(buf)
        return out

    while remaining:
        if _char_count(remaining) <= max_chars:
            segments.append(remaining)
            break

        # 先取一个上限内的候选窗口，再在窗口内找最后一个句读边界
        window = ""
        for i, ch in enumerate(remaining):
            cost = 2 if ord(ch) > 0x2E7F else 1
            if _char_count(window) + cost > max_chars:
                break
            window += ch
        cut_pos = len(window)

        # 从窗口尾部向前找句读边界
        best = None
        for m in _BREAK_PATTERN.finditer(window):
            best = m.end()
        if best and best > max_chars // 4:  # 边界太靠前会浪费片，宁可硬切
            cut_pos = best

        piece, remaining = remaining[:cut_pos].strip(), remaining[cut_pos:].strip()
        if _char_count(piece) > max_chars:  # 边界缺失时的兜底
            segments.extend(_hard_cut(piece))
        elif piece:
            segments.append(piece)

    return [s for s in segments if s.strip()]


def count_chars(text: str) -> int:
    """对外暴露的计费字符数（积分预扣用）。"""
    return _char_count(text)
