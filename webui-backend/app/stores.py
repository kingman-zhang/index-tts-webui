"""项目与术语表的 JSON 文件存储（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import GLOSSARY_PATH, PROJECTS_DIR
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

def load_glossary() -> list:
    if GLOSSARY_PATH.exists():
        return json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    return []


def save_glossary(terms: list):
    GLOSSARY_PATH.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")
