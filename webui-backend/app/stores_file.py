"""项目 / 术语表 / 收藏音色的 **JSON 文件后端**（原 stores.py 的 IO 部分抽出）。

配合 `stores.py` 门面使用：门面按 `CONTENT_STORE` 在 file / mongo 之间分派，
本模块只负责「怎么落到磁盘」。读 / 原子写两个原语复用 `app/json_store.py`
（与账本、会话后端同一份实现，免得「文件损坏先备份」被抄成三份）。

文件布局（DATA_DIR 下）
  projects/<project_id>.json      项目全文（含 owner_id）
  glossary.json                   全局术语库（超管维护）
  glossary_users/<user_id>.json   用户术语库
  favorite-voices.json            收藏音色（共享，未登录 / 隔离未开启）
  favorites_<user_id>.json        收藏音色（按用户；隔离开启时用）

⚠️ 本模块**不带跨实例一致性**（本地磁盘 + 无跨进程锁）。多实例部署请用
   `CONTENT_STORE=mongo`，见 STORAGE_MIGRATION_PLAN.md §9。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .config import (
    FAVORITES_PATH,
    GLOSSARY_PATH,
    GLOSSARY_USERS_DIR,
    PROJECTS_DIR,
)
from .json_store import read_json as _read, write_json as _write


# ─── 项目 ───────────────────────────────────────────────────

def project_path(project_id: str) -> Path:
    return PROJECTS_DIR / f"{project_id}.json"


def save_project(doc: dict) -> str:
    """整份覆盖写（项目是单文档，没有局部更新语义）。"""
    _write(project_path(doc["id"]), doc)
    return doc["id"]


def load_project(project_id: str) -> Optional[dict]:
    data = _read(project_path(project_id), None)
    return data if isinstance(data, dict) else None


def list_projects(owner_id: Optional[str] = None) -> list[dict]:
    """项目摘要列表（不含 lines 正文），按 updated_at 倒序。

    `owner_id=None` ⇒ 返回全部（隔离未开启时用）；给定 ⇒ 只返回该用户的。
    """
    out: list[dict] = []
    for f in PROJECTS_DIR.glob("*.json"):
        data = _read(f, None)
        if not isinstance(data, dict):
            continue
        if owner_id is not None and data.get("owner_id") != owner_id:
            continue
        out.append({
            "id": data.get("id"),
            "name": data.get("name", "未命名"),
            "updated_at": data.get("updated_at"),
            "line_count": len(data.get("lines") or []),
        })
    # 按业务时间（updated_at）而非磁盘 mtime —— 后者会被外部 touch 干扰，
    # 且 mongo 后端没有 mtime，两个后端必须用同一判据才谈得上「行为等价」。
    out.sort(key=lambda p: (p.get("updated_at") or ""), reverse=True)
    return out


def delete_project(project_id: str) -> bool:
    try:
        project_path(project_id).unlink()
        return True
    except FileNotFoundError:
        return False


# ─── 术语表 ─────────────────────────────────────────────────
# 全局库（超管维护，对所有用户生效）+ 用户库（同名时优先），
# 合并规则是**后端无关的纯逻辑**，留在 stores.py 门面里。

_USER_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def glossary_user_path(user_id: str) -> Path:
    """用户库路径。user_id 过滤非法字符，杜绝路径穿越。"""
    safe = _USER_ID_SAFE.sub("", str(user_id or ""))
    if not safe:
        raise ValueError("user_id 不能为空")
    return GLOSSARY_USERS_DIR / f"{safe}.json"


def load_global_glossary() -> list:
    data = _read(GLOSSARY_PATH, [])
    return data if isinstance(data, list) else []


def save_global_glossary(terms: list) -> None:
    _write(GLOSSARY_PATH, terms)


def load_user_glossary(user_id: Optional[str]) -> list:
    if not user_id:
        return []
    try:
        path = glossary_user_path(user_id)
    except ValueError:
        return []
    data = _read(path, [])
    return data if isinstance(data, list) else []


def save_user_glossary(user_id: str, terms: list) -> None:
    path = glossary_user_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, terms)


# ─── 收藏音色 ───────────────────────────────────────────────
# 隔离未开启（或未登录）时用共享文件，与历史行为一致。

def favorites_path(user_id: Optional[str]) -> Path:
    if user_id:
        return FAVORITES_PATH.parent / f"favorites_{user_id}.json"
    return FAVORITES_PATH


def load_favorites(user_id: Optional[str] = None) -> list[str]:
    data = _read(favorites_path(user_id), [])
    if not isinstance(data, list):
        return []
    return list(dict.fromkeys(p for p in data if isinstance(p, str)))


def save_favorites(paths: list, user_id: Optional[str] = None) -> list[str]:
    normalized = list(dict.fromkeys(p for p in paths if isinstance(p, str) and p.strip()))
    _write(favorites_path(user_id), normalized)
    return normalized
