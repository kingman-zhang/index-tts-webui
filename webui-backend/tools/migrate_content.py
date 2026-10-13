#!/usr/bin/env python3
"""把**文件后端**的内容数据灌进 MongoDB（二期 A/B 的一次性迁移）。

什么时候必须跑
--------------
`CONTENT_STORE` 从 `file` 切到 `mongo` 之前跑一次 —— 否则切换后系统读到的是
**空的** projects / glossary / favorites（用户表现为「项目、词条全不见了」）。
尤其**全局术语库**（`glossary.json`，超管维护、对所有用户生效）一定要迁。

迁什么
------
| 源（DATA_DIR 下）              | 目标集合    | `_id`        |
|---|---|---|
| `projects/*.json`              | `projects`  | `project_id` |
| `glossary.json`                | `glossary`  | `GLOBAL`     |
| `glossary_users/<uid>.json`    | `glossary`  | `<uid>`      |
| `favorite-voices.json`         | `favorites` | `GLOBAL`     |
| `favorites_<uid>.json`         | `favorites` | `<uid>`      |

语义
----
- **幂等**：全部走 upsert（`replace_one(..., upsert=True)`），可反复跑。
- **只加不减**：**不删**目标库里已有、源文件里没有的文档 —— 迁移只做「补齐」，
  不做「清空重灌」。真要清空重灌，直接 `drop` 掉对应集合后重跑本工具。
- 读文件用 `stores_file`（它不依赖 `CONTENT_STORE`），写库用 `stores_mongo`
  —— 两个实现直接被调用，**与当前开关取值无关**（迁移时开关通常还是 file）。

用法
----
    cd webui-backend
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \\
        python tools/migrate_content.py --dry-run    # 只统计，不写库
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \\
        python tools/migrate_content.py              # 真灌（幂等，可重复跑）

退出码：0 = 成功（含 dry-run）；2 = 连不上数据库。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app import stores_file as fsrc  # noqa: E402
from app import stores_mongo as fdst  # noqa: E402
from app.config import (  # noqa: E402
    DATA_DIR,
    FAVORITES_PATH,
    GLOSSARY_USERS_DIR,
    MONGO_DB,
    PROJECTS_DIR,
)
from app.db import DBUnavailable, get_mongo_db  # noqa: E402

_FAVORITES_PREFIX = "favorites_"


def collect() -> dict:
    """从文件后端读出全部内容（不依赖 `CONTENT_STORE` 开关）。"""
    # 项目：data/projects/*.json
    projects: list[dict] = []
    for p in sorted(PROJECTS_DIR.glob("*.json")):
        doc = fsrc.load_project(p.stem)
        if isinstance(doc, dict):
            projects.append(doc)

    # 全局术语库
    global_terms = fsrc.load_global_glossary()

    # 用户术语库：data/glossary_users/<uid>.json
    user_terms: dict[str, list] = {}
    for p in sorted(GLOSSARY_USERS_DIR.glob("*.json")):
        terms = fsrc.load_user_glossary(p.stem)
        if terms:
            user_terms[p.stem] = terms

    # 收藏音色：共享那份 + 每用户那份
    favorites: dict[str | None, list] = {}
    if FAVORITES_PATH.exists():
        favorites[None] = fsrc.load_favorites(None)
    for p in sorted(FAVORITES_PATH.parent.glob(f"{_FAVORITES_PREFIX}*.json")):
        uid = p.stem[len(_FAVORITES_PREFIX):]
        if uid:
            favorites[uid] = fsrc.load_favorites(uid)

    return {
        "projects": projects,
        "global_terms": global_terms,
        "user_terms": user_terms,
        "favorites": favorites,
    }


def summarize(data: dict) -> str:
    return (
        f"项目 {len(data['projects'])} 个；"
        f"全局词条 {len(data['global_terms'])} 条；"
        f"用户词库 {len(data['user_terms'])} 个；"
        f"收藏 {len(data['favorites'])} 份"
    )


def apply(data: dict) -> dict:
    """把收集到的内容 upsert 进 Mongo，返回各表写入计数。"""
    counts = {"projects": 0, "glossary_global": 0, "glossary_users": 0, "favorites": 0}

    for doc in data["projects"]:
        fdst.save_project(doc)
        counts["projects"] += 1

    if data["global_terms"]:
        fdst.save_global_glossary(data["global_terms"])
        counts["glossary_global"] = 1

    for uid, terms in data["user_terms"].items():
        fdst.save_user_glossary(uid, terms)
        counts["glossary_users"] += 1

    for uid, paths in data["favorites"].items():
        fdst.save_favorites(paths, uid)
        counts["favorites"] += 1

    return counts


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    print(f"内容迁移：文件（{DATA_DIR}） → MongoDB（库 = {MONGO_DB}）")
    data = collect()
    print(f"  源数据：{summarize(data)}")

    if dry_run:
        print("  （--dry-run：仅统计，未写库）")
        return 0

    try:
        get_mongo_db().command("ping")
    except DBUnavailable as e:
        print(f"✗ 连不上 MongoDB：{e}", file=sys.stderr)
        return 2

    counts = apply(data)
    print(
        "✓ 已写入："
        f"projects {counts['projects']}｜"
        f"glossary(GLOBAL) {counts['glossary_global']}｜"
        f"glossary(users) {counts['glossary_users']}｜"
        f"favorites {counts['favorites']}"
    )
    print("  注：默认只加不减；库里已有的、文件里没有的文档**保持不动**。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
