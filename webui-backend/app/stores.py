"""项目与术语表的 JSON 文件存储（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Optional

# 注意 import 顺序：config 必须排在 name_punct 之前 —— name_punct 在模块级读取
# .env（NAME_PUNCT_TARGET / NAME_PUNCT_NORMALIZE），而 .env 是由 config 载入的。
from .config import GLOSSARY_PATH, GLOSSARY_USERS_DIR, PROJECTS_DIR, logger
from . import name_punct
from .models import ProjectModel


# ─── 项目存储 ───────────────────────────────────────────────

def project_path(project_id: str) -> Path:
    return PROJECTS_DIR / f"{project_id}.json"


def save_project(project: ProjectModel) -> str:
    if project.id is None:
        project.id = f"proj_{uuid.uuid4().hex[:10]}"
        project.created_at = datetime.now().isoformat()
    project.updated_at = datetime.now().isoformat()
    project_path(project.id).write_text(
        project.model_dump_json(indent=2), encoding="utf-8"
    )
    return project.id


def load_project(project_id: str) -> Optional[ProjectModel]:
    path = project_path(project_id)
    if not path.exists():
        return None
    return ProjectModel(**json.loads(path.read_text(encoding="utf-8")))


# ─── 术语词汇表 ─────────────────────────────────────────────
# 两层结构：
#   data/glossary.json                  全局库（超管维护，对所有用户生效）
#   data/glossary_users/<user_id>.json  用户库（用户自维护，同名时优先）
#
# 合并规则：以全局库的顺序为骨架，用户库同 original 的条目覆盖其值；
#           用户库独有的条目追加在末尾。
#           用户条目 replacement 为空串 = 停用该词（从合并结果中剔除），
#           用于关掉某条内置词而不影响全局库。
#
# 顺序敏感：合成时是按序 str.replace，合并保持「全局骨架顺序 + 用户新增」，
# 因此用户覆盖不会改变全局条目的相对执行顺序。

_USER_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def glossary_user_path(user_id: str) -> Path:
    """用户库路径。user_id 过滤非法字符，杜绝路径穿越。"""
    safe = _USER_ID_SAFE.sub("", str(user_id or ""))
    if not safe:
        raise ValueError("user_id 不能为空")
    return GLOSSARY_USERS_DIR / f"{safe}.json"


def _read_terms(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_terms(path: Path, terms: list) -> None:
    """原子写入（tmp + replace），与 membership/store.py 同策略。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_global_glossary() -> list:
    """全局库原始列表（超管视角，不过滤/不合并）。"""
    return _read_terms(GLOSSARY_PATH)


def save_global_glossary(terms: list) -> None:
    _write_terms(GLOSSARY_PATH, terms)


def load_user_glossary(user_id: Optional[str]) -> list:
    """用户库原始列表；未登录或 user_id 非法时返回空。"""
    if not user_id:
        return []
    try:
        path = glossary_user_path(user_id)
    except ValueError:
        return []
    return _read_terms(path)


def save_user_glossary(user_id: str, terms: list) -> None:
    _write_terms(glossary_user_path(user_id), terms)


def merge_glossary(global_terms: list, user_terms: list,
                   with_source: bool = False) -> list:
    """全局骨架 + 用户覆盖/追加。with_source=True 时每项带 source 标记。"""
    user_map: dict = {}
    for t in user_terms:
        o = str(t.get("original") or "").strip()
        if o:
            user_map[o] = t

    merged: list = []
    seen: set = set()

    for t in global_terms:
        o = str(t.get("original") or "").strip()
        if not o or o in seen:
            continue
        seen.add(o)
        if o in user_map:
            rep = user_map[o].get("replacement") or ""
            if rep == "":
                continue  # 用户停用了这条内置词
            src = "user"
        else:
            rep = t.get("replacement") or ""
            src = "global"
        item = {"original": o, "replacement": rep}
        if with_source:
            item["source"] = src
        merged.append(item)

    for t in user_terms:
        o = str(t.get("original") or "").strip()
        if not o or o in seen:
            continue
        seen.add(o)
        rep = t.get("replacement") or ""
        if rep == "":
            continue
        item = {"original": o, "replacement": rep}
        if with_source:
            item["source"] = "user"
        merged.append(item)

    return merged


def load_glossary(user_id: Optional[str] = None, *, with_source: bool = False) -> list:
    """合并后的生效词表。user_id 为空（未登录）时等于全局库。

    **这是「管理视角」**：逐条如实返回词条，不做变体展开 —— 前端「我的词条」
    面板与超管后台要看到的就是用户真正写下的那些行。合成请用
    `load_glossary_for_synthesis()`。
    """
    return merge_glossary(
        load_global_glossary(), load_user_glossary(user_id), with_source=with_source
    )


# ─── 词条 key 的「中点等价类」展开 ───────────────────────────
# 问题：中译外国人名的间隔号有 10 个 Unicode 变体（见 name_punct.NAME_SEPARATORS），
# 而词条匹配是逐字符精确的。用户按某一种写法配了词条，原文换成另一种写法就
# **静默失效**。实例（2026-09-28）：词条 `9・11→九幺幺`（U+30FB），原文写成
# `9·11`（U+00B7）时不命中，而 `9·11` 两侧是数字、name_punct 也不介入，
# 于是原样送进引擎变成 unk 怪音。
#
# 解法：合成前把含分隔号的 key 就地展开成全部变体 —— 一条词条覆盖所有写法。
# 只在内存展开，**不落盘、不改真源**，管理界面看到的仍是用户写的那一条。
#
# 顺序语义不受影响：展开紧跟在原条目之后（apply_glossary 是按序 str.replace），
# 且已被显式定义的 key（无论来自全局还是用户库）优先保留、不被变体覆盖。

# 缺省开启：与 name_punct 同理，修的是「同一视觉符号不同码位导致静默失效」的确证缺陷。
GLOSSARY_SEP_VARIANTS = os.environ.get("GLOSSARY_SEP_VARIANTS", "1").strip().lower() not in (
    "", "0", "false", "no", "off",
)

# 单条词条的变体数上限：n 个分隔号 → 10^n。超限则跳过该条并告警，
# 避免 `A・B・C` 这种 key 直接炸成 100 条。
def _env_positive_int(name: str, default: int) -> int:
    """读正整数环境变量；非法值回落默认，不让手滑的 .env 把服务打挂。"""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


MAX_SEP_VARIANTS = _env_positive_int("GLOSSARY_SEP_VARIANTS_MAX", 64)


def separator_variants(key: str, *, limit: int | None = None) -> list:
    """列出 key 的全部中点变体（**不含 key 自身**）；无分隔号或超出上限时返回空。"""
    positions = [i for i, ch in enumerate(key) if ch in name_punct.NAME_SEPARATORS]
    if not positions:
        return []
    cap = MAX_SEP_VARIANTS if limit is None else limit
    if len(name_punct.NAME_SEPARATORS) ** len(positions) > cap:
        logger.warning(
            "词条 %r 含 %d 个分隔号，变体数超上限 %d，已跳过展开（原文需与词条码位一致）",
            key, len(positions), cap,
        )
        return []
    out = []
    for combo in product(name_punct.NAME_SEPARATORS, repeat=len(positions)):
        chars = list(key)
        for pos, ch in zip(positions, combo):
            chars[pos] = ch
        cand = "".join(chars)
        if cand != key:
            out.append(cand)
    return out


def expand_separator_variants(terms: list, *, limit: int | None = None) -> list:
    """把含中点的词条展开成全部码位变体，返回新列表（不修改入参）。

    - 变体条目紧跟原条目，保持相对顺序（apply_glossary 顺序敏感）
    - 已被词表里显式定义的 key 不被覆盖（用户手写的那条优先）
    - 空 replacement（停用）、key == replacement 的条目跳过
    """
    if not terms or not GLOSSARY_SEP_VARIANTS:
        return terms

    seen = {str(t.get("original") or "") for t in terms if isinstance(t, dict)}
    out: list = []
    for t in terms:
        out.append(t)
        if not isinstance(t, dict):
            continue
        original = str(t.get("original") or "")
        replacement = t.get("replacement")
        if not original or not replacement or replacement == original:
            continue
        for cand in separator_variants(original, limit=limit):
            if cand in seen:
                continue
            seen.add(cand)
            item = {"original": cand, "replacement": replacement}
            if "source" in t:
                item["source"] = t["source"]
            out.append(item)
    return out


def load_glossary_for_synthesis(user_id: Optional[str] = None, *,
                                with_source: bool = False) -> list:
    """**合成/预览视角**的生效词表 = 合并 + 中点等价类展开。

    与 load_glossary 的唯一区别是含中点的词条被展开成全部码位变体。
    """
    return expand_separator_variants(
        load_glossary(user_id, with_source=with_source)
    )


def save_glossary(terms: list) -> None:
    """兼容旧调用：写入全局库。"""
    save_global_glossary(terms)


def apply_glossary(lines: list, terms: list) -> list:
    """返回「应用术语替换后」的新 lines，**不修改入参**（合成前调用）。

    为什么必须返回新列表而不是就地改：lines 与 task["lines"] 是同一对象，
    就地改会把替换后文本写进任务详情/磁盘存档，用户看到的不再是自己输入的
    原文，重试扣费也会按替换后字数计算。合成只该拿到这份临时文本。

    逐字同音替换：按 terms 顺序对每行 text 做 str.replace（顺序敏感），
    词表顺序由 merge_glossary 保证（全局骨架 + 用户覆盖）。
    """
    if not terms:
        return lines
    out: list = []
    for line in lines:
        if not isinstance(line, dict):
            out.append(line)
            continue
        new_line = dict(line)  # 浅拷贝：只换 text，其余字段（emotion/silence 等）沿用
        text = new_line.get("text")
        if isinstance(text, str) and text:
            for t in terms:
                text = text.replace(t["original"], t["replacement"])
            new_line["text"] = text
        out.append(new_line)
    return out
