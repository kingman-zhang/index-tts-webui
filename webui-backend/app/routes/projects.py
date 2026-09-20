"""项目管理端点（原 server.py「项目管理」分区，行为不变）。"""

from __future__ import annotations

import json
import re

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import PROJECTS_DIR
from ..models import ProjectModel
from ..stores import load_project, project_path, save_project
from ..membership import get_optional_user
from ..membership import service as member_svc

router = APIRouter()


def _isolation_on() -> bool:
    """数据隔离开关：会员或登录体系开启时生效。"""
    return member_svc.ENFORCE or member_svc.REQUIRE_LOGIN


def _require_user(user: Optional[dict]) -> dict:
    if _isolation_on() and not user:
        raise HTTPException(401, "请先登录后使用项目存档")
    return user


def _owned(data: dict, user: Optional[dict]) -> bool:
    """项目归属判断；无主（owner_id 空）遗留项目仅在隔离未开启时可见。"""
    if not _isolation_on():
        return True
    return bool(user) and data.get("owner_id") == user["user_id"]


@router.get("/api/projects")
async def list_projects(user: Optional[dict] = Depends(get_optional_user)):
    """列出当前用户保存的项目。"""
    _require_user(user)
    projects = []
    for f in sorted(PROJECTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not _owned(data, user):
                continue
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
async def save_project_route(project: ProjectModel, user: Optional[dict] = Depends(get_optional_user)):
    """保存（或新建）项目；隔离开启时归属当前用户，不得覆盖他人同名存档。"""
    _require_user(user)
    if _isolation_on() and user:
        if project.id:
            existing = load_project(project.id)
            if existing and existing.owner_id and existing.owner_id != user["user_id"]:
                raise HTTPException(403, "不能覆盖其他用户的项目")
        project.owner_id = user["user_id"]
    project_id = save_project(project)
    return {"id": project_id, "name": project.name, "updated_at": project.updated_at}


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str, user: Optional[dict] = Depends(get_optional_user)):
    """加载项目。"""
    project = load_project(project_id)
    if project is None or not _owned(project.model_dump(), user):
        raise HTTPException(404, "项目不存在")
    return project


@router.put("/api/projects/{project_id}")
async def update_project(project_id: str, project: ProjectModel, user: Optional[dict] = Depends(get_optional_user)):
    """更新项目。"""
    existing = load_project(project_id)
    if existing is None or not _owned(existing.model_dump(), user):
        raise HTTPException(404, "项目不存在")
    project.id = project_id
    if _isolation_on() and user:
        project.owner_id = existing.owner_id or user["user_id"]
    save_project(project)
    return {"id": project_id, "name": project.name, "updated_at": project.updated_at}


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, user: Optional[dict] = Depends(get_optional_user)):
    """删除项目。"""
    existing = load_project(project_id)
    if existing is None or not _owned(existing.model_dump(), user):
        raise HTTPException(404, "项目不存在")
    project_path(project_id).unlink()
    return {"deleted": project_id}


@router.post("/api/projects/import")
async def import_project_from_text(request: Request):
    """从纯文本导入对话脚本。

    解析 "A: 文本" / "B: 文本" 格式，返回 lines 数组（不保存）。
    """
    body = await request.json()
    raw_text = body.get("text", "")
    speaker_a = (body.get("speaker_a_name") or "A").strip()
    speaker_b = (body.get("speaker_b_name") or "B").strip()

    lines = []
    for raw_line in raw_text.strip().split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        # 支持 "A: 文本" / "A：文本" / "角色名: 文本"；前缀限 12 字符防 "https://..." 误切
        m = re.match(r"^([^:：]{1,12})[:：]\s*(.*)$", raw_line)
        if m:
            prefix, text = m.group(1).strip(), m.group(2).strip()
            if prefix == speaker_a or prefix.upper() == "A":
                spk = "A"
            elif prefix == speaker_b or prefix.upper() == "B":
                spk = "B"
            else:
                # 未识别前缀，交替分配
                spk = "A" if len(lines) % 2 == 0 else "B"
        else:
            # 无前缀，交替分配
            spk = "A" if len(lines) % 2 == 0 else "B"
            text = raw_line
        lines.append({
            "speaker": spk,
            "text": text.strip(),
            "emotion": {"mode": 0, "vector": [0]*8, "weight": 0.65, "random": False},
        })
    return {"lines": lines, "count": len(lines)}
