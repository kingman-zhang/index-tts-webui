"""术语词汇表端点：全局库（超管维护）与用户库（登录用户维护）两层。

职责划分：
  - 全局库 `data/glossary.json`            超管维护，对所有用户生效
  - 用户库 `data/glossary_users/<uid>.json` 用户自维护，**同名时优先于全局库**

接口：
  GET    /api/glossary            当前用户视角：生效词表 + 全局库 + 我的词条
  POST   /api/glossary            新增/覆盖「我的词条」（需登录）
  PUT    /api/glossary            全量覆盖「我的词条」（需登录）
  DELETE /api/glossary/{original} 删除「我的词条」（需登录）
  POST   /api/glossary/apply      按当前用户视角替换文本（预览用）

  GET    /api/admin/glossary            全局库（超管，X-Admin-Token）
  POST   /api/admin/glossary            新增/覆盖全局条目
  PUT    /api/admin/glossary            全量覆盖全局库
  DELETE /api/admin/glossary/{original} 删除全局条目

空 replacement 的语义：在**用户库**中表示「停用该词」（从生效词表剔除），
用于关掉某条内置词而不影响其他用户；在全局库中沿用旧语义（替换为空串）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ..membership import get_current_user, get_optional_user
from ..membership.routes import require_admin
from ..models import GlossaryTerm
from ..stores import (
    load_global_glossary,
    load_glossary_for_synthesis,
    load_user_glossary,
    merge_glossary,
    save_global_glossary,
    save_user_glossary,
)

router = APIRouter()


def _view(user: Optional[dict]) -> dict:
    """当前用户视角的完整视图：生效词表 + 全局库原始 + 我的词条原始。"""
    uid = (user or {}).get("user_id")
    global_terms = load_global_glossary()
    user_terms = load_user_glossary(uid)
    terms = merge_glossary(global_terms, user_terms, with_source=True)
    return {
        "terms": terms,               # 合并后真正生效的词表（带 source）
        "count": len(terms),
        "global": global_terms,       # 全局库原始（内置区渲染用）
        "mine": user_terms,           # 我的词条原始（含停用项）
        "global_count": len(global_terms),
        "user_count": len(user_terms),
        "logged_in": bool(uid),
    }


def _normalize_terms(terms: list) -> list:
    """规整条目：仅保留 original 非空项，按 original 去重（后者胜出）。"""
    out: list = []
    index: dict = {}
    for t in terms:
        if not isinstance(t, dict):
            continue
        o = str(t.get("original") or "").strip()
        if not o:
            continue
        item = {"original": o, "replacement": str(t.get("replacement") or "")}
        if o in index:
            out[index[o]] = item
        else:
            index[o] = len(out)
            out.append(item)
    return out


# ─── 用户视角 ───────────────────────────────────────────────

@router.get("/api/glossary")
async def get_glossary(user: Optional[dict] = Depends(get_optional_user)):
    """生效词表。未登录时只有全局库；写操作需登录。"""
    return _view(user)


@router.post("/api/glossary")
async def add_glossary_term(term: GlossaryTerm, user: dict = Depends(get_current_user)):
    """新增/覆盖我的词条。replacement 留空表示停用同名的内置词。"""
    original = term.original.strip()
    if not original:
        raise HTTPException(400, "原词不能为空")
    uid = user["user_id"]
    terms = [t for t in load_user_glossary(uid) if t.get("original") != original]
    terms.append({"original": original, "replacement": term.replacement})
    save_user_glossary(uid, _normalize_terms(terms))
    return _view(user)


@router.put("/api/glossary")
async def update_glossary(request: Request, user: dict = Depends(get_current_user)):
    """全量覆盖我的词条。"""
    body = await request.json()
    save_user_glossary(user["user_id"], _normalize_terms(body.get("terms", [])))
    return _view(user)


@router.delete("/api/glossary/{original}")
async def delete_glossary_term(original: str, user: dict = Depends(get_current_user)):
    """删除我的词条。若它只是对内置词的覆盖，删除后内置词重新生效。"""
    uid = user["user_id"]
    terms = [t for t in load_user_glossary(uid) if t.get("original") != original]
    save_user_glossary(uid, terms)
    return _view(user)


@router.post("/api/glossary/apply")
async def apply_glossary(request: Request,
                         user: Optional[dict] = Depends(get_optional_user)):
    """对文本应用当前用户的生效词表，返回替换后的文本。

    用 _for_synthesis 版本，保证预览结果与真实合成一致（含中点变体展开）。
    """
    body = await request.json()
    text = body.get("text", "")
    uid = (user or {}).get("user_id")
    for t in load_glossary_for_synthesis(uid):
        text = text.replace(t["original"], t["replacement"])
    return {"text": text}


# ─── 超管：全局库（X-Admin-Token）────────────────────────────

@router.get("/api/admin/glossary")
async def admin_get_glossary(_: None = Depends(require_admin)):
    terms = load_global_glossary()
    return {"terms": terms, "count": len(terms)}


@router.post("/api/admin/glossary")
async def admin_add_glossary_term(term: GlossaryTerm,
                                  _: None = Depends(require_admin)):
    original = term.original.strip()
    if not original:
        raise HTTPException(400, "原词不能为空")
    terms = [t for t in load_global_glossary() if t.get("original") != original]
    terms.append({"original": original, "replacement": term.replacement})
    save_global_glossary(_normalize_terms(terms))
    final = load_global_glossary()
    return {"terms": final, "count": len(final)}


@router.put("/api/admin/glossary")
async def admin_update_glossary(request: Request,
                                _: None = Depends(require_admin)):
    body = await request.json()
    save_global_glossary(_normalize_terms(body.get("terms", [])))
    terms = load_global_glossary()
    return {"terms": terms, "count": len(terms)}


@router.delete("/api/admin/glossary/{original}")
async def admin_delete_glossary_term(original: str,
                                     _: None = Depends(require_admin)):
    terms = [t for t in load_global_glossary() if t.get("original") != original]
    save_global_glossary(terms)
    return {"terms": terms, "count": len(terms)}
