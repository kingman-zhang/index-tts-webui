"""音色预设端点（原 server.py「音色预设」分区，行为不变）。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..config import VOICE_PRESETS_DIR, logger
from ..models import VoicePresetModel

router = APIRouter()


@router.get("/api/voice-presets")
async def list_voice_presets():
    """列出所有音色预设。"""
    presets = []
    for f in sorted(VOICE_PRESETS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            presets.append({
                "id": data.get("id"),
                "name": data.get("name"),
                "voice_name": data.get("voice_name"),
                "speed": data.get("role_speed", data.get("speed", 1.0)),
                "is_preset": data.get("is_preset", False),
                "created_at": data.get("created_at"),
            })
        except Exception:
            continue
    return {"presets": presets, "count": len(presets)}


@router.post("/api/voice-presets")
async def save_voice_preset(preset: VoicePresetModel):
    """保存音色预设。"""
    if preset.id is None:
        preset.id = f"vp_{uuid.uuid4().hex[:10]}"
        preset.created_at = datetime.now().isoformat()
    path = VOICE_PRESETS_DIR / f"{preset.id}.json"
    path.write_text(preset.model_dump_json(indent=2), encoding="utf-8")
    return {"id": preset.id, "name": preset.name, "created_at": preset.created_at}


@router.put("/api/voice-presets/{preset_id}")
async def rename_voice_preset(preset_id: str, request: Request):
    """重命名用户保存的角色预设。"""
    path = VOICE_PRESETS_DIR / f"{Path(preset_id).name}.json"
    if not path.exists():
        raise HTTPException(404, "预设不存在")
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "预设名称不能为空")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["name"] = name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": preset_id, "name": name}


@router.get("/api/voice-presets/{preset_id}")
async def get_voice_preset(preset_id: str):
    """加载音色预设详情。"""
    path = VOICE_PRESETS_DIR / f"{preset_id}.json"
    if not path.exists():
        raise HTTPException(404, "预设不存在")
    return json.loads(path.read_text(encoding="utf-8"))


@router.delete("/api/voice-presets/{preset_id}")
async def delete_voice_preset(preset_id: str):
    """删除音色预设。"""
    path = VOICE_PRESETS_DIR / f"{preset_id}.json"
    if not path.exists():
        raise HTTPException(404, "预设不存在")
    path.unlink()
    return {"deleted": preset_id}
