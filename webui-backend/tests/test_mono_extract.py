#!/usr/bin/env python3
"""mono 文档导入（/api/mono/extract + /api/mono/split）单测。

运行：webui-backend 目录下
  /Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_mono_extract.py

本文件锁两层行为：
  1. **解析层**：txt/docx/pdf 的提取与各上限机制（上限可配，测机制不测魔数）；
  2. **接口层**：extract 返回可供「分段确认页」使用的 chapters，且**只解析不建任务**。

刻意不断言上限的具体数值：那属于运维配置（`MONO_IMPORT_MAX_CHARS` 等），
写死数字会让调参就挂测试。这里断言的是「机制正确 + 默认值足以放下一本书」。
"""
import io
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="wb-mono-extract-test-"))
os.environ.setdefault("TTS_URL", "http://127.0.0.1:59999")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.routes import mono as mono_routes
from app.routes.mono import (
    CHAPTER_MAX_CHARS,
    IMPORT_MAX_CHARS,
    IMPORT_MAX_PDF_PAGES,
    _decode_text,
    _env_int,
    _extract_docx,
    _extract_pdf,
)

PASS = 0
FAIL = 0


def check(cond, msg, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL += 1
        print(f"  ✗ {msg}  {extra}".rstrip())


def expect_http_error(fn, status, msg):
    global PASS, FAIL
    try:
        fn()
        FAIL += 1
        print(f"  ✗ {msg}（未抛出 HTTPException）")
    except HTTPException as e:
        if e.status_code == status:
            PASS += 1
            print(f"  ✓ {msg}")
        else:
            FAIL += 1
            print(f"  ✗ {msg}（status={e.status_code}）")
    except Exception as e:
        FAIL += 1
        print(f"  ✗ {msg}（异常类型 {type(e).__name__}: {e}）")


def main():
    global PASS, FAIL

    print("── 1. txt/md 解码 ──")
    check(_decode_text("用来充实我们的大脑".encode("utf-8")) == "用来充实我们的大脑", "txt utf-8")
    check(_decode_text("恐惧情绪测试".encode("gb18030")) == "恐惧情绪测试", "txt gb18030")
    check(_decode_text(b"\xef\xbb\xbfBOM text") == "BOM text", "txt utf-8-sig BOM")

    print("\n── 2. docx 提取 ──")
    try:
        import docx as docx_mod

        doc = docx_mod.Document()
        doc.add_paragraph("第一段：读书。")
        doc.add_paragraph("")  # 空段应被丢弃
        doc.add_paragraph("第二段：看电影；")
        buf = io.BytesIO()
        doc.save(buf)
        text = _extract_docx(buf.getvalue())
        check(text == "第一段：读书。\n第二段：看电影；", f"docx 段落提取（空段丢弃）: {text!r}")
    except ImportError:
        print("  SKIP docx 测试（python-docx 未安装）")

    print("\n── 3. pdf 页数上限（测机制，不依赖具体数值）──")
    try:
        from pypdf import PdfWriter

        def make_pdf(pages: int) -> bytes:
            w = PdfWriter()
            for _ in range(pages):
                w.add_blank_page(width=100, height=100)
            b = io.BytesIO()
            w.write(b)
            return b.getvalue()

        orig = mono_routes.IMPORT_MAX_PDF_PAGES
        try:
            mono_routes.IMPORT_MAX_PDF_PAGES = 50
            expect_http_error(lambda: _extract_pdf(make_pdf(51)), 400, "超过页数上限 → 400")
            try:
                _extract_pdf(make_pdf(50))
                check(True, "恰好等于上限 → 通过")
            except Exception as e:  # noqa: BLE001
                check(False, f"恰好等于上限 → 通过（实际 {type(e).__name__}: {e}）")
        finally:
            mono_routes.IMPORT_MAX_PDF_PAGES = orig
    except ImportError:
        print("  SKIP pdf 测试（pypdf 未安装）")

    print("\n── 4. 上限可配（环境变量驱动）──")
    os.environ["WB_TEST_INT"] = "123"
    check(_env_int("WB_TEST_INT", 9) == 123, "_env_int 读环境变量")
    os.environ["WB_TEST_INT"] = "abc"
    check(_env_int("WB_TEST_INT", 9) == 9, "_env_int 非法值回落默认（不抛）")
    os.environ["WB_TEST_INT"] = "  "
    check(_env_int("WB_TEST_INT", 9) == 9, "_env_int 空白回落默认")
    os.environ.pop("WB_TEST_INT", None)
    check(_env_int("WB_TEST_INT", 9) == 9, "_env_int 缺失回落默认")
    check(
        IMPORT_MAX_CHARS >= 500_000,
        f"默认字数上限足以放下一本 50 万字的书（当前 {IMPORT_MAX_CHARS}）",
    )
    check(IMPORT_MAX_PDF_PAGES >= 200, f"默认 PDF 页数上限适合成书（当前 {IMPORT_MAX_PDF_PAGES}）")
    check(CHAPTER_MAX_CHARS > 0, f"单章上限为正（当前 {CHAPTER_MAX_CHARS}）")

    print("\n── 5. 接口层：extract 返回可确认的 chapters ──")
    from app.main import app  # noqa: E402  延迟导入，避免解析层测试受 app 初始化影响

    client = TestClient(app)

    small = "第一章 起点\n他背起行囊[pause:0.5]走出了门。\n第二章 抵达\n他终于回到家。\n"
    r = client.post(
        "/api/mono/extract",
        files={"file": ("demo.txt", small.encode("utf-8"), "text/plain")},
    )
    check(r.status_code == 200, f"小文件 extract 200（{r.status_code}）")
    data = r.json()
    check(
        {"text", "chars", "chapters", "chapter_max_chars"} <= set(data),
        "返回 text / chars / chapters / chapter_max_chars",
        f"keys={sorted(data)}",
    )
    check(len(data["chapters"]) == 2, "切出 2 章", f"got {len(data['chapters'])}")
    check(
        data["chapters"][0]["title"] == "第一章 起点"
        and "他背起行囊[pause:0.5]走出了门。" in data["text"],
        "章标题与正文一致",
        f"title={data['chapters'][0]['title']!r}",
    )
    check(
        sum(c["chars"] for c in data["chapters"]) == data["chars"],
        "章节字数之和 = 全文计费字数",
        f"{sum(c['chars'] for c in data['chapters'])} vs {data['chars']}",
    )
    check(
        all("task" not in str(k).lower() for k in data),
        "extract 不产生任何合成任务",
    )

    print("\n── 6. 接口层：一本书（约 50 万字）能进来并被正确分章 ──")
    # 每章段数由**单章上限反推**，使「每章都在上限内」这个前提不依赖上限的具体数值
    # （否则调一次默认上限就得改这里）。压在上限内是刻意的：超了会触发「标题优先 +
    # 上限夹逼」的章内二次切分，那是另一个用例，见第 9 节。
    para = "字" * 50
    paras_per_chapter = max(1, int(CHAPTER_MAX_CHARS * 0.8) // len(para))
    BOOK_CHAPTERS = 51
    chapters_src = [
        f"第{i + 1}章 测试章节\n" + "\n".join([para] * paras_per_chapter)
        for i in range(BOOK_CHAPTERS)
    ]
    book = "\n".join(chapters_src)
    book_chars = len(re.sub(r"\s", "", book))
    check(book_chars >= 500_000, f"构造的书确实 ≥50 万字（{book_chars}）")
    r = client.post(
        "/api/mono/extract",
        files={"file": ("book.txt", book.encode("utf-8"), "text/plain")},
    )
    check(r.status_code == 200, f"50 万字书籍不再被拒（{r.status_code}）")
    data = r.json()
    check(data["chars"] == book_chars, "字数与构造一致", f"{data['chars']} vs {book_chars}")
    check(
        len(data["chapters"]) == BOOK_CHAPTERS,
        f"每章都在上限内 ⇒ 恰好切成 {BOOK_CHAPTERS} 章",
        f"got {len(data['chapters'])}",
    )
    check(
        all(c["chars"] <= data["chapter_max_chars"] for c in data["chapters"]),
        "每章字数 ≤ 单章上限",
    )
    check(
        sum(c["chars"] for c in data["chapters"]) == book_chars,
        "字数守恒",
    )
    check(
        data["chapters"][0]["title"] == "第1章 测试章节",
        "首章标题正确",
        f"got {data['chapters'][0]['title']!r}",
    )

    # 按行号还原首章，必须与原始构造逐字符一致（前端就是这么还原的）
    lines = data["text"].split("\n")
    ch0 = data["chapters"][0]
    body0 = "\n".join(lines[ch0["start_line"]:ch0["end_line"]])
    check(body0 == chapters_src[0], "按行号还原的首章正文与原文一致")

    print("\n── 7. 接口层：/api/mono/split 重新切分 ──")
    # 语义要点：每章上限是「不得超过」，**不是「凑满」** —— 调大它不会把已有的
    # 标题章合并回去（那会改变用户看到的章节结构），只会让原本被切碎的长章不再被切。
    r = client.post("/api/mono/split", json={"text": book, "chapter_max_chars": 50_000})
    check(r.status_code == 200, f"split 200（{r.status_code}）")
    d2 = r.json()
    check(
        len(d2["chapters"]) == BOOK_CHAPTERS,
        "上限调大不合并已有标题章（上限是上限，不是目标）",
        f"got {len(d2['chapters'])}",
    )
    check(d2["chapter_max_chars"] == 50_000, "新上限被回显", f"got {d2['chapter_max_chars']}")
    check(sum(c["chars"] for c in d2["chapters"]) == book_chars, "字数守恒")

    r = client.post("/api/mono/split", json={"text": book, "chapter_max_chars": 3_000})
    d3 = r.json()
    check(len(d3["chapters"]) > BOOK_CHAPTERS, "上限调小 ⇒ 每章被再切碎", f"got {len(d3['chapters'])}")
    check(all(c["chars"] <= 3_000 for c in d3["chapters"]), "每块 ≤ 新上限")
    check(sum(c["chars"] for c in d3["chapters"]) == book_chars, "切碎后字数仍守恒")

    r = client.post("/api/mono/split", json={"text": "第一章\n正文", "chapter_max_chars": -1})
    check(r.status_code == 400, f"负数上限被拒（{r.status_code}）")
    r = client.post("/api/mono/split", json={"text": "第一章\n正文"})
    check(r.status_code == 200, "缺省上限可用")

    print("\n── 8. 旧行为不回归：不支持 .doc / 空文本 ──")
    r = client.post(
        "/api/mono/extract",
        files={"file": ("old.doc", b"\xd0\xcf\x11\xe0", "application/msword")},
    )
    check(r.status_code == 400 and "docx" in r.json()["detail"], f".doc 提示另存为 .docx（{r.status_code}）")
    r = client.post(
        "/api/mono/extract",
        files={"file": ("weird.xyz", b"abc", "application/octet-stream")},
    )
    check(r.status_code == 400, f"不支持的扩展名被拒（{r.status_code}）")
    r = client.post(
        "/api/mono/extract",
        files={"file": ("blank.txt", "   \n\n  ".encode("utf-8"), "text/plain")},
    )
    check(r.status_code == 400, f"纯空白文本被拒（{r.status_code}）")

    print("\n── 9. 单个标题章超上限 ⇒ 章内二次切分（标题带 k/n）──")
    # 段数同样由上限反推，但这次**刻意超过**它 —— 这才是本用例要触发的路径。
    over = "第一章 长夜\n" + "\n".join([para] * (CHAPTER_MAX_CHARS // len(para) + 20))
    r = client.post(
        "/api/mono/extract",
        files={"file": ("over.txt", over.encode("utf-8"), "text/plain")},
    )
    check(r.status_code == 200, f"超长单章可正常导入（{r.status_code}）")
    d4 = r.json()
    check(len(d4["chapters"]) > 1, "被二次切成多块", f"got {len(d4['chapters'])}")
    check(
        "（1/" in d4["chapters"][0]["title"],
        "标题带（1/n）后缀",
        f"got {d4['chapters'][0]['title']!r}",
    )
    check(
        all(c["chars"] <= d4["chapter_max_chars"] for c in d4["chapters"]),
        "二次切分后每块仍 ≤ 单章上限",
    )

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
