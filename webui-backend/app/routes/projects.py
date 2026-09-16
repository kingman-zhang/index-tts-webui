"""项目管理端点（原 server.py「项目管理」分区，行为不变）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from ..config import PROJECTS_DIR
from ..models import ProjectModel
from ..stores import load_project, project_path, save_project

router = APIRouter()


@router.get("/api/projects")
async def list_projects():
    """列出所有保存的项目。"""
    projects = []
    for f in sorted(PROJECTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            projects.append({
                "id": data.get("id"),
                "name": data.get("name", "未命名"),
                "updated_at": data.get("updated_at"),
                "line_count": len(data.get("lines", [])),
            })
        except Exception:
            continue
    return {"projects": projects, "count": len(projects)}


@router.post("/api/projects")
async def save_project_route(project: ProjectModel):
    """保存（或新建）项目。"""
    project_id = save_project(project)
    return {"id": project_id, "name": project.name, "updated_at": project.updated_at}


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    """加载项目。"""
    project = load_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@router.put("/api/projects/{project_id}")
async def update_project(project_id: str, project: ProjectModel):
    """更新项目。"""
    if not project_path(project_id).exists():
        raise HTTPException(404, "项目不存在")
    project.id = project_id
    save_project(project)
    return {"id": project_id, "name": project.name, "updated_at": project.updated_at}


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目。"""
    path = project_path(project_id)
    if not path.exists():
        raise HTTPException(404, "项目不存在")
    path.unlink()
    return {"deleted": project_id}


@router.post("/api/projects/import")
async def import_project_from_text(request: Request):
    """从纯文本导入对话脚本。

    解析 "A: 文本" / "B: 文本" 格式，返回 lines 数组（不保存）。
    """
    body = await request.json()
    raw_text = body.get("text", "")
    speaker_a = body.get("speaker_a_name", "A")
    speaker_b = body.get("speaker_b_name", "B")

    lines = []
    for raw_line in raw_text.strip().split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        # 支持 "A: 文本" / "A：文本" / "A 文本" 等格式
        if ":" in raw_line:
            spk, text = raw_line.split(":", 1)
        elif "：" in raw_line:
            spk, text = raw_line.split("：", 1)
        else:
            # 无前缀，交替分配
            spk = "A" if len(lines) % 2 == 0 else "B"
            text = raw_line
        spk = spk.strip().upper()
        if spk not in ("A", "B"):
            spk = "A" if len(lines) % 2 == 0 else "B"
        lines.append({
            "speaker": spk,
            "text": text.strip(),
            "emotion": {"mode": 0, "vector": [0]*8, "weight": 0.65, "random": False},
        })
    return {"lines": lines, "count": len(lines)}
