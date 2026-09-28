"""项目与术语表的 JSON 文件存储（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import GLOSSARY_PATH, GLOSSARY_USERS_DIR, PROJECTS_DIR
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
    """合并后的生效词表。user_id 为空（未登录）时等于全局库。"""
    return merge_glossary(
        load_global_glossary(), load_user_glossary(user_id), with_source=with_source
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
