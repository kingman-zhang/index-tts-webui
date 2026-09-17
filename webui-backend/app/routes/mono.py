"""配音模式（mono）：导入文档提取 + 结果音频服务。"""

from __future__ import annotations

import io
import re
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import queue_state as qs

router = APIRouter()

# ─── 文档导入限制 ────────────────────────────────────────────
IMPORT_MAX_BYTES = 20 * 1024 * 1024  # 文件 ≤20MB
IMPORT_MAX_CHARS = 10_000            # 解析后字数 ≤1 万（不计空白）
IMPORT_MAX_PDF_PAGES = 50            # pdf 页数 ≤50
IMPORT_ALLOWED_EXTS = {".doc", ".docx", ".pdf", ".txt", ".md"}


def _decode_text(data: bytes) -> str:
    """txt/md：按常见编码尝试解码。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 后重试")


def _extract_docx(data: bytes) -> str:
    """docx：段落按行拼接；空段落丢弃。"""
    try:
        import docx  # python-docx
    except ImportError as e:
        raise RuntimeError("服务端缺少 python-docx，请安装后重启") from e
    document = docx.Document(io.BytesIO(data))
    lines = [p.text.strip() for p in document.paragraphs]
    return "\n".join(line for line in lines if line)


def _extract_pdf(data: bytes) -> str:
    """pdf：逐页提取文本，页数与内容长度由调用方校验。"""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("服务端缺少 pypdf，请安装后重启") from e
    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) > IMPORT_MAX_PDF_PAGES:
        raise HTTPException(400, f"PDF 共 {len(reader.pages)} 页，超过 {IMPORT_MAX_PDF_PAGES} 页上限")
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(p for p in pages if p)


@router.post("/api/mono/extract")
async def mono_extract(file: UploadFile = File(...)):
    """导入文档提取纯文本，供配音画布使用。

    限制：doc/docx/pdf/txt/md、≤20MB、解析后 ≤1 万字、pdf ≤50 页。
    旧版二进制 .doc 暂不支持（提示另存为 .docx）。
    """
    data = await file.read()
    if len(data) > IMPORT_MAX_BYTES:
        raise HTTPException(400, "文件超过 20MB 上限")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in IMPORT_ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的格式 {suffix or '(无后缀)'}，仅支持 doc/docx/pdf/txt/md")

    try:
        if suffix in (".txt", ".md"):
            text = _decode_text(data)
        elif suffix == ".docx":
            text = _extract_docx(data)
        elif suffix == ".pdf":
            text = _extract_pdf(data)
        else:  # .doc 旧版二进制格式
            raise HTTPException(400, "旧版 .doc 暂不支持，请在 Word/WPS 中另存为 .docx 后重试")
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(400, f"解析失败：{e}")

    chars = len(re.sub(r"\s", "", text))
    if chars > IMPORT_MAX_CHARS:
        raise HTTPException(400, f"文档约 {chars} 字，超过 1 万字上限")
    if not text.strip():
        raise HTTPException(400, "未能从文档中提取到文字（可能是扫描件/图片型 PDF）")
    return {"text": text.strip(), "chars": chars}


@router.get("/api/mono/audio/{task_id}")
async def mono_audio(task_id: str):
    """提供配音任务的合成结果音频（backend 本地落盘，不经 tts-server）。"""
    task = qs.queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    output_path = task.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(output_path, media_type="audio/wav", filename=f"mono_{task_id}.wav")
