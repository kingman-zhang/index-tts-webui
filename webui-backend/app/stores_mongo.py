"""项目 / 术语表 / 收藏音色的 **MongoDB 后端**（二期 A/B，2026-10-13）。

上库动因 = **多实例部署**：这三类数据原先「本地 JSON 文件 + 进程内锁」，
多实例下用户在 A 实例存的东西 B 实例读不到。见 STORAGE_MIGRATION_PLAN.md §9。

集合（库名 = `MONGO_DB`，默认 `podcast`；靠独立 database 与同实例其他项目隔离）
──────────────────────────────────────────────────────────────
| 集合 | `_id` | 文档 |
|---|---|---|
| `projects`  | project_id（`proj_*`） | 项目全文（含 `owner_id`） |
| `glossary`  | `"GLOBAL"` 或 user_id | `{terms: [...]}` |
| `favorites` | `"GLOBAL"` 或 user_id | `{paths: [...]}` |

设计要点
- **`_id` 就是业务主键**：`project_id` 天然唯一；术语表 / 收藏用
  「`GLOBAL` 或 user_id」作主键 ⇒ 天然实现「一人一份」，且 upsert 幂等。
- 术语表的全局库固定 `_id="GLOBAL"`。user_id 一律是 `u_*` 形态
  （见 membership 的 `u_{12hex}`），**不会与 `GLOBAL` 撞**。
- 收藏沿用文件版的语义：**未登录 / 隔离未开启时共用 `GLOBAL` 那份**。
- 项目列表用 `$size` 现算行数（**不把 lines 正文拉回来**），并按 `updated_at` 排序。

⚠️ 与账本后端一致：**不做**「连不上就退回文件」的兜底 —— 静默切换会让用户
   看到「项目、词条全不见了」。连不上就报错（`DBUnavailable`）。
"""

from __future__ import annotations

from typing import Optional

from .db import get_mongo_db

# 全局（非按用户）文档的固定主键 —— 与 user_id（`u_*`）不可能冲突
_GLOBAL = "GLOBAL"


def _projects():
    return get_mongo_db()["projects"]


def _glossary():
    return get_mongo_db()["glossary"]


def _favorites():
    return get_mongo_db()["favorites"]


def _clean(doc: Optional[dict]) -> Optional[dict]:
    """去掉 Mongo 自己维护的 `_id`（业务字段里没有它，回给上层会污染模型）。"""
    if not doc:
        return None
    return {k: v for k, v in doc.items() if k != "_id"}


# ─── 项目 ───────────────────────────────────────────────────

def save_project(doc: dict) -> str:
    """整份覆盖（upsert）。项目是单文档，没有局部更新语义。"""
    project_id = doc["id"]
    _projects().replace_one({"_id": project_id}, {**doc, "_id": project_id}, upsert=True)
    return project_id


def load_project(project_id: str) -> Optional[dict]:
    return _clean(_projects().find_one({"_id": project_id}))


def list_projects(owner_id: Optional[str] = None) -> list[dict]:
    """项目摘要列表，按 `updated_at` 倒序。

    用 aggregation 让服务端算 `line_count`（`$size`）—— 投影 `lines` 再在
    Python 里数长度会把整篇文稿拉回本地，项目越大越浪费。
    """
    match = {} if owner_id is None else {"owner_id": owner_id}
    cursor = _projects().aggregate([
        {"$match": match},
        {"$project": {
            "id": 1,
            "name": 1,
            "updated_at": 1,
            "line_count": {"$size": {"$ifNull": ["$lines", []]}},
        }},
        {"$sort": {"updated_at": -1}},
    ])
    return [
        {
            "id": d.get("id"),
            "name": d.get("name", "未命名"),
            "updated_at": d.get("updated_at"),
            "line_count": d.get("line_count", 0),
        }
        for d in cursor
    ]


def delete_project(project_id: str) -> bool:
    return _projects().delete_one({"_id": project_id}).deleted_count > 0


# ─── 术语表 ─────────────────────────────────────────────────

def load_global_glossary() -> list:
    doc = _glossary().find_one({"_id": _GLOBAL}, {"terms": 1})
    return list(doc.get("terms") or []) if doc else []


def save_global_glossary(terms: list) -> None:
    _glossary().replace_one(
        {"_id": _GLOBAL}, {"_id": _GLOBAL, "terms": list(terms)}, upsert=True
    )


def load_user_glossary(user_id: Optional[str]) -> list:
    if not user_id:
        return []
    doc = _glossary().find_one({"_id": user_id}, {"terms": 1})
    return list(doc.get("terms") or []) if doc else []


def save_user_glossary(user_id: str, terms: list) -> None:
    _glossary().replace_one(
        {"_id": user_id}, {"_id": user_id, "terms": list(terms)}, upsert=True
    )


# ─── 收藏音色 ───────────────────────────────────────────────

def load_favorites(user_id: Optional[str] = None) -> list[str]:
    doc = _favorites().find_one({"_id": user_id or _GLOBAL}, {"paths": 1})
    paths = list(doc.get("paths") or []) if doc else []
    return list(dict.fromkeys(p for p in paths if isinstance(p, str)))


def save_favorites(paths: list, user_id: Optional[str] = None) -> list[str]:
    normalized = list(dict.fromkeys(p for p in paths if isinstance(p, str) and p.strip()))
    key = user_id or _GLOBAL
    _favorites().replace_one({"_id": key}, {"_id": key, "paths": normalized}, upsert=True)
    return normalized
