"""术语词汇表端点（原 server.py「术语词汇表」分区，行为不变）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..models import GlossaryTerm
from ..stores import load_glossary, save_glossary

router = APIRouter()


@router.get("/api/glossary")
async def get_glossary():
    """获取术语词汇表。"""
    terms = load_glossary()
    return {"terms": terms, "count": len(terms)}


@router.post("/api/glossary")
async def add_glossary_term(term: GlossaryTerm):
    """添加术语。"""
    if not term.original.strip():
        raise HTTPException(400, "原词不能为空")
    terms = load_glossary()
    # 去重
    terms = [t for t in terms if t.get("original") != term.original]
    terms.append({"original": term.original, "replacement": term.replacement})
    save_glossary(terms)
    return {"terms": terms, "count": len(terms)}


@router.delete("/api/glossary/{original}")
async def delete_glossary_term(original: str):
    """删除术语。"""
    terms = load_glossary()
    terms = [t for t in terms if t.get("original") != original]
    save_glossary(terms)
    return {"terms": terms, "count": len(terms)}


@router.put("/api/glossary")
async def update_glossary(request: Request):
    """批量更新术语表。"""
    body = await request.json()
    terms = body.get("terms", [])
    save_glossary(terms)
    return {"terms": terms, "count": len(terms)}


@router.post("/api/glossary/apply")
async def apply_glossary(request: Request):
    """对文本应用术语替换，返回替换后的文本。"""
    body = await request.json()
    text = body.get("text", "")
    terms = load_glossary()
    for t in terms:
        text = text.replace(t["original"], t["replacement"])
    return {"text": text}
