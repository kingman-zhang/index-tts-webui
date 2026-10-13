"""项目 / 术语表 / 收藏音色的存储**门面** + 术语表纯逻辑。

门面（按 `CONTENT_STORE` 分派）
──────────────────────────────
import 期一次性选定后端（`stores_file` / `stores_mongo`），之后**只做转发**，
不含任何后端分支。取值域 `file`（默认）| `mongo`；非法值**启动即报错**、
不静默回退 —— 两个后端的存储形态不同，静默切换会让用户看到「项目、词条全没了」。

为什么与账本分开关
──────────────────
账本（`MEMBER_STORE`）、会话（`SESSION_STORE`）、内容（`CONTENT_STORE`）
互相独立、可任意组合。三者的迁移节奏与风险不同：账本是钱、会话是登录态、
内容是可重建的用户素材 —— 一次只动一处才好定位、好回滚。

纯逻辑（后端无关，两个后端共用同一套语义）
──────────────────────────────────────────
术语表的「全局骨架 + 用户覆盖」合并、中点码位变体展开、按序 `str.replace` 替换。

⚠️ 上库动因＝**多实例部署**（文件版在 A 实例存的东西 B 实例读不到）。
   见 STORAGE_MIGRATION_PLAN.md §9。
"""

from __future__ import annotations

import os
from datetime import datetime
from itertools import product
from typing import Optional
from uuid import uuid4

# 注意 import 顺序：config 必须排在 name_punct 之前 —— name_punct 在模块级读取
# .env（NAME_PUNCT_TARGET / NAME_PUNCT_NORMALIZE），而 .env 是由 config 载入的。
from .config import CONTENT_STORE, logger
from . import name_punct
from .models import ProjectModel


# ─── 后端分派（import 期一次决定）────────────────────────────

if CONTENT_STORE == "file":
    from . import stores_file as _impl
elif CONTENT_STORE == "mongo":
    from . import stores_mongo as _impl
else:
    raise RuntimeError(
        f"未知的 CONTENT_STORE={CONTENT_STORE!r}；取值域 file | mongo。"
        "（两后端存储形态不同，不支持静默回退 —— 请检查 .env）"
    )

# 门面必须完整转发的接口；缺一个就在 import 期炸，而不是等到某个接口 500。
_REQUIRED_API = (
    # 项目
    "save_project", "load_project", "list_projects", "delete_project",
    # 术语表
    "load_global_glossary", "save_global_glossary",
    "load_user_glossary", "save_user_glossary",
    # 收藏
    "load_favorites", "save_favorites",
)
_missing = [n for n in _REQUIRED_API if not hasattr(_impl, n)]
if _missing:
    raise RuntimeError(
        f"内容存储后端 {CONTENT_STORE!r} 缺少接口：{', '.join(_missing)}"
    )

BACKEND = CONTENT_STORE   # 自检用（/api/version 报它）


# ─── 项目（门面）─────────────────────────────────────────────

def save_project(project: ProjectModel) -> str:
    """保存项目：补齐 id / created_at / updated_at 后交给后端整份覆盖。

    id 与时间戳在这里生成（而非后端），保证 file / mongo 两后端行为一致。
    """
    if project.id is None:
        project.id = f"proj_{uuid4().hex[:10]}"
        project.created_at = datetime.now().isoformat()
    project.updated_at = datetime.now().isoformat()
    _impl.save_project(project.model_dump())
    return project.id


def load_project(project_id: str) -> Optional[ProjectModel]:
    doc = _impl.load_project(project_id)
    return ProjectModel(**doc) if doc else None


def list_projects(owner_id: Optional[str] = None) -> list[dict]:
    """项目摘要列表；`owner_id=None` ⇒ 全部（隔离未开启时用）。"""
    return _impl.list_projects(owner_id)


def delete_project(project_id: str) -> bool:
    return _impl.delete_project(project_id)


# ─── 术语表（门面）──────────────────────────────────────────

def load_global_glossary() -> list:
    return _impl.load_global_glossary()


def save_global_glossary(terms: list) -> None:
    _impl.save_global_glossary(terms)


def load_user_glossary(user_id: Optional[str]) -> list:
    return _impl.load_user_glossary(user_id)


def save_user_glossary(user_id: str, terms: list) -> None:
    _impl.save_user_glossary(user_id, terms)


# ─── 收藏音色（门面）────────────────────────────────────────
# user_id=None ⇒ 共享那份（未登录 / 隔离未开启），与文件版语义一致。

def load_favorites(user_id: Optional[str] = None) -> list[str]:
    return _impl.load_favorites(user_id)


def save_favorites(paths: list, user_id: Optional[str] = None) -> list[str]:
    return _impl.save_favorites(paths, user_id)


# ─── 术语表：合并（后端无关纯逻辑）────────────────────────────
# 两层结构：
#   <全局库>  超管维护，对所有用户生效
#   <用户库>  用户自维护，同名时优先
#
# 合并规则：以全局库的顺序为骨架，用户库同 original 的条目覆盖其值；
#           用户库独有的条目追加在末尾。
#           用户条目 replacement 为空串 = 停用该词（从合并结果中剔除），
#           用于关掉某条内置词而不影响全局库。
#
# 顺序敏感：合成时是按序 str.replace，合并保持「全局骨架顺序 + 用户新增」，
# 因此用户覆盖不会改变全局条目的相对执行顺序。

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
