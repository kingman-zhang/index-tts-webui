"""BreezeBlue 音色库端点。

数据源：data/breezeblue/voices.json（310 条中文音色元数据，导入自 breezeblue.ai）
        data/breezeblue/audio/voc_*.wav（24kHz 参考音频）

设计要点：
  - 元数据启动/首次访问时整表读入内存（310 条 <200KB），筛选/搜索全内存完成。
  - 与合成链路解耦：audio 文件路径可直接作为 VoiceRef.local_path / voice_path
    使用，mono/podcast runner 与各引擎适配器零改动。
  - 共享资源：不属于任何用户，不做 voices_meta 归属过滤（与预设同级别）。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import DATA_DIR, logger

router = APIRouter()

BB_DIR = DATA_DIR / "breezeblue"
VOICES_PATH = BB_DIR / "voices.json"
AUDIO_DIR = BB_DIR / "audio"

# 内存缓存：[(voice_dict, search_blob)]，search_blob 为小写拼接字段，加速模糊搜索
_cache: list[tuple[dict, str]] | None = None


def _load_cache() -> list[tuple[dict, str]]:
    global _cache
    if _cache is None:
        if not VOICES_PATH.exists():
            _cache = []
            return _cache
        try:
            items = json.loads(VOICES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[breezeblue] voices.json 读取失败: %s", e)
            items = []
        _cache = []
        for v in items:
            if not isinstance(v, dict) or not v.get("id"):
                continue
            blob = " ".join(
                filter(None, [
                    v.get("name"), v.get("description"),
                    v.get("category_zh"), " ".join(v.get("tones") or []),
                    " ".join(v.get("tones_zh") or []),
                ]),
            ).lower()
            _cache.append((v, blob))
        logger.info("[breezeblue] loaded %d voices", len(_cache))
    return _cache


def _facets(cache: list[tuple[dict, str]]) -> dict:
    cats: dict[str, dict] = {}
    genders: dict[str, int] = {}
    ages: dict[str, int] = {}
    for v, _ in cache:
        code = v.get("category") or ""
        if code:
            entry = cats.setdefault(code, {"code": code, "name": v.get("category_zh") or code, "count": 0})
            entry["count"] += 1
        if v.get("gender"):
            genders[v["gender"]] = genders.get(v["gender"], 0) + 1
        if v.get("age"):
            ages[v["age"]] = ages.get(v["age"], 0) + 1
    return {
        "categories": sorted(cats.values(), key=lambda c: -c["count"]),
        "genders": genders,
        "ages": ages,
    }


@router.get("/api/breezeblue/voices")
async def list_breezeblue_voices(
    search: str = "",
    category: str = "",
    gender: str = "",
    age: str = "",
    page: int = 1,
    page_size: int = 30,
):
    """分页列出 BreezeBlue 音色（search/category/gender/age 过滤 + facets）。"""
    cache = _load_cache()
    kw = (search or "").strip().lower()
    matched = [
        (v, blob) for v, blob in cache
        if (not kw or kw in blob)
        and (not category or v.get("category") == category)
        and (not gender or v.get("gender") == gender)
        and (not age or v.get("age") == age)
    ]
    total = len(matched)
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    start = (page - 1) * page_size
    page_items = []
    for v, _ in matched[start:start + page_size]:
        item = dict(v)
        filename = Path(v.get("audio") or "").name
        item["filename"] = filename
        item["path"] = str(AUDIO_DIR / filename)  # 服务端绝对路径，可直接作 VoiceRef.local_path / voice_path
        page_items.append(item)
    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": start + page_size < total,
        "facets": _facets(cache),
    }


@router.get("/api/breezeblue/audio/{filename}")
async def get_breezeblue_audio(filename: str):
    """提供音色库参考音频（试听与合成共用）。"""
    safe = Path(filename).name  # 取纯文件名，拒绝路径穿越
    path = AUDIO_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(path, media_type="audio/wav", filename=safe)
