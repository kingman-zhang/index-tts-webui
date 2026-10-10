"""配音模式（mono）：导入文档提取 + 自动分章 + 结果音频服务。"""

from __future__ import annotations

import io
import os
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import queue_state as qs
from ..book_split import (
    DEFAULT_CHAPTER_MAX_CHARS,
    count_chars,
    split_into_chapters,
)

router = APIRouter()


def _env_int(name: str, default: int) -> int:
    """读整型环境变量；缺失或不可解析时回默认（不抛，避免起不来）。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ─── 文档导入限制（均可用环境变量放宽，见 命令大全.md） ──────
# 20MB 只对「带图 PDF / docx」构成约束：纯文本 20MB ≈ 1000 万字，永远碰不到。
# 真正生效的闸门是字数；单本 50 万字的书要求它至少到 100 万量级。
IMPORT_MAX_BYTES = _env_int("MONO_IMPORT_MAX_BYTES", 20 * 1024 * 1024)
IMPORT_MAX_CHARS = _env_int("MONO_IMPORT_MAX_CHARS", 2_000_000)
IMPORT_MAX_PDF_PAGES = _env_int("MONO_IMPORT_MAX_PDF_PAGES", 2000)
# 单章（= 单个合成任务）字数上限：导入时用它自动分章，确认页可临时改。
CHAPTER_MAX_CHARS = _env_int("MONO_CHAPTER_MAX_CHARS", DEFAULT_CHAPTER_MAX_CHARS)
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
    """导入文档提取纯文本 + 自动分章，供配音画布与「分段确认页」使用。

    限制：doc/docx/pdf/txt/md、≤IMPORT_MAX_BYTES、解析后 ≤IMPORT_MAX_CHARS、
    pdf ≤IMPORT_MAX_PDF_PAGES 页。旧版二进制 .doc 暂不支持（提示另存为 .docx）。

    返回 `chapters` 是按 `CHAPTER_MAX_CHARS` 预切的章节清单；**本端点只解析与
    切分，不产生任何合成任务**——是否生成由用户在确认页决定。
    """
    data = await file.read()
    if len(data) > IMPORT_MAX_BYTES:
        raise HTTPException(400, f"文件超过 {IMPORT_MAX_BYTES // (1024 * 1024)}MB 上限")
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

    # 统一换行符：分章返回的行号要能被前端用同一份 text 还原，两边必须同构。
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise HTTPException(400, "未能从文档中提取到文字（可能是扫描件/图片型 PDF）")
    chars = count_chars(text)
    if chars > IMPORT_MAX_CHARS:
        raise HTTPException(400, f"文档约 {chars} 字，超过 {IMPORT_MAX_CHARS} 字上限")
    return {
        "text": text,
        "chars": chars,
        "chapters": split_into_chapters(text, CHAPTER_MAX_CHARS),
        "chapter_max_chars": CHAPTER_MAX_CHARS,
    }


class SplitRequest(BaseModel):
    """确认页调「每章上限」重新切分时用（避免为了改一个数字重传整个文件）。"""

    text: str
    chapter_max_chars: int | None = None


@router.post("/api/mono/split")
async def mono_split(req: SplitRequest):
    """按给定「每章上限」重新切分已解析的文本。纯计算，不落盘、不建任务。"""
    limit = req.chapter_max_chars or CHAPTER_MAX_CHARS
    if limit < 0:
        raise HTTPException(400, "每章上限不能为负数")
    if len(req.text) > IMPORT_MAX_CHARS * 4:
        raise HTTPException(400, "文本过长，请重新导入文件")
    return {
        "chapters": split_into_chapters(req.text, limit),
        "chars": count_chars(req.text),
        "chapter_max_chars": limit,
    }


@router.get("/api/mono/audio/{task_id}")
async def mono_audio(task_id: str):
    """提供配音任务的合成结果音频（backend 本地落盘，不经 tts-server）。"""
    task = qs.queue_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    output_path = task.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(output_path, media_type="audio/wav",
                        filename=qs.audio_download_name(task, task_id, "mono"))
