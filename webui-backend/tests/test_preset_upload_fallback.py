#!/usr/bin/env python3
"""预设音色预热端点的降级行为单测（2026-10-08）。

背景：线上两台 AutoDL tts-server **关机**后，端口代理对任意路径返回 `HTTP 404` +
一张 HTML 报错页（`<title>Not Found</title>` / 含 autodl.com/docs/port 链接）。
TCP 是通的、代理在应答 ⇒ **不是 ConnectError**。

旧实现只对「异常」降级、对「非 200 响应」直接 `raise HTTPException`，于是那张 HTML
原文被当成错误详情回给了前端：

    {"detail": "上传到 TTS 服务失败: <!DOCTYPE html>..."}

更严重的是前端 `selectPreset` 的 `onChange` 写在 `try` 里 ⇒ 抛错时它根本不执行,
**该预设音色完全无法选中**（不只是弹个提示）。

本文件锁的行为：
  1. 上游非 200（404 HTML / 500 / 502 …）⇒ 200 + `local_only=true` + 本地路径，
     且**绝不把上游正文回给前端**。
  2. 音色表非 200 时**短路**：不再发起上传请求（不白等一次 60s 超时）。
  3. 连不上（ConnectError）⇒ 同样降级（既有行为，回归保护）。
  4. 正常路径不被破坏：音色表 200 命中同名 ⇒ 服务器路径；未命中 ⇒ 上传；上传 200
     ⇒ 用返回值；上传 409 ⇒ 视为成功。这四条都必须仍是 `local_only=false`。
  5. 输入错误**不能**被降级吞掉：缺 name ⇒ 400；本地无该文件 ⇒ 404。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-preset-fallback-test-")
os.environ["DATA_DIR"] = TMP
os.environ["TTS_URL"] = "http://127.0.0.1:59999"
os.environ["MEMBER_ENFORCE"] = "0"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routes import presets  # noqa: E402

client = TestClient(app)
PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


# ── 造一个本地预设音色（端点会先确认它在本地存在） ──────────────────────────
NAME = "男-播客1.mp3"
PRESET_DIR = Path(TMP) / "preset-voices"
PRESET_DIR.mkdir(parents=True, exist_ok=True)
(PRESET_DIR / NAME).write_bytes(b"\xff\xfb" + b"\x00" * 2048)
LOCAL_PATH = str(PRESET_DIR / NAME)

AUTODL_404 = (
    "<!DOCTYPE html>\n<html>\n<head>\n<title>Not Found</title>\n<style>\n"
    "    body {\n        width: 35em;\n        margin: 0 auto;\n    }\n</style>\n</head>\n"
    '<body>\n<h1>404 Not Found</h1>\n<p><a href="https://www.autodl.com/docs/port/">查看FAQ文档</a>.</p>\n'
    "</body>\n</html>\n"
)


class FakeResp:
    def __init__(self, status_code: int, text: str = "", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """替换 presets.http_client：记录调用序列，按需返回响应或抛异常。"""

    def __init__(self, get_resp=None, post_resp=None, get_exc=None, post_exc=None):
        self.get_resp, self.post_resp = get_resp, post_resp
        self.get_exc, self.post_exc = get_exc, post_exc
        self.calls: list[tuple[str, str]] = []

    async def get(self, url, **kw):
        self.calls.append(("GET", url))
        if self.get_exc:
            raise self.get_exc
        return self.get_resp

    async def post(self, url, **kw):
        self.calls.append(("POST", url))
        if self.post_exc:
            raise self.post_exc
        return self.post_resp


_ORIG_HTTP_CLIENT = presets.http_client


def use(fc: FakeClient):
    presets.http_client = fc
    return fc


def call(name: str = NAME):
    return client.post("/api/preset-voices/upload-to-tts", json={"name": name})


def not_fallback(d: dict) -> bool:
    """「未降级」的判据是 `local_only is not True`，不是 `is False`。

    因为**新鲜上传成功**那条分支是 `return resp.json()`（原样透传上游响应体），
    而 tts-server 的 `/api/voices/upload` 返回体里没有 `local_only` 字段 ⇒ 那里是
    `None`。只有「命中已有」「409 并发」两条分支才显式写 `local_only: False`。
    （前端 `client.ts` 里该字段本就是可选的，也从不读它。）
    """
    return d.get("local_only") is not True


def assert_fallback(tag: str, r, fc: FakeClient):
    """降级的统一判据：200 / local_only=true / 本地真实路径 / 不含上游正文。"""
    check(f"{tag} HTTP 200（不再报错）", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    d = r.json() if r.status_code == 200 else {}
    check(f"{tag} local_only=true", d.get("local_only") is True, str(d)[:160])
    check(f"{tag} path 是本地真实文件", Path(d.get("path", "")).is_file(), str(d.get("path")))
    check(f"{tag} path 就是后端本地路径", d.get("path") == LOCAL_PATH, str(d.get("path")))
    check(f"{tag} size_kb 为真实体积（非 0）", d.get("size_kb", 0) > 0, str(d.get("size_kb")))
    check(f"{tag} 响应不含上游 HTML 正文",
          "<!DOCTYPE" not in r.text and "Not Found" not in r.text, r.text[:120])


try:
    # ── ① 音色表 404（AutoDL 关机的真实场景） ────────────────────────────
    print("① 音色表返回 404 + AutoDL HTML（本次线上故障的原始场景）")
    fc = use(FakeClient(get_resp=FakeResp(404, text=AUTODL_404)))
    r = call()
    assert_fallback("①", r, fc)
    check("① 非 200 时短路：只发了 GET、没白试上传",
          [c[0] for c in fc.calls] == ["GET"], str(fc.calls))

    # ── ② 音色表 200（无同名）但上传被 404 ───────────────────────────────
    print("② 音色表 200 但无同名 → 上传拿到 404（同上故障，只是故障点在后一步）")
    fc = use(FakeClient(get_resp=FakeResp(200, payload={"voices": []}),
                        post_resp=FakeResp(404, text=AUTODL_404)))
    assert_fallback("②", call(), fc)
    check("② 确实尝试过上传（GET→POST）",
          [c[0] for c in fc.calls] == ["GET", "POST"], str(fc.calls))

    # ── ③ 其它上游错误码 ────────────────────────────────────────────────
    print("③ 上游 500 / 502 / 503 同样降级")
    for code in (500, 502, 503):
        fc = use(FakeClient(get_resp=FakeResp(code, text=f"upstream {code}")))
        assert_fallback(f"③[{code}]", call(), fc)

    # ── ④ 连不上（既有行为，回归保护） ──────────────────────────────────
    # ⚠️ 异常路径与非 200 路径**故意不同**：连不上时仍会尝试上传（「列表挂了但上传能通」
    # 是可能的），只有拿到非 200 才短路。两条都要锁住，否则以后会被"统一"掉。
    print("④a 音色表与上传都连不上 → 降级")
    for exc in (httpx.ConnectError("conn refused"),
                httpx.ConnectTimeout("timeout"),
                httpx.RemoteProtocolError("bad proto")):
        fc = use(FakeClient(get_exc=exc, post_exc=exc))
        assert_fallback(f"④a[{type(exc).__name__}]", call(), fc)
        check(f"④a[{type(exc).__name__}] 异常路径仍尝试上传（GET→POST）",
              [c[0] for c in fc.calls] == ["GET", "POST"], str(fc.calls))

    print("④b 音色表连不上、但上传成功 → 用服务器返回值（不短路）")
    srv = "/root/autodl-tmp/index-tts/voices/" + NAME
    fc = use(FakeClient(get_exc=httpx.ConnectError("conn refused"),
                        post_resp=FakeResp(200, payload={"name": NAME, "path": srv, "size_kb": 2.0})))
    r = call()
    d = r.json()
    check("④b HTTP 200", r.status_code == 200, r.text[:160])
    check("④b 未降级（上传成功）", not_fallback(d), str(d))
    check("④b path 是服务器路径", d.get("path") == srv, str(d.get("path")))

    # ── ⑤ 音色表命中已有同名 → 服务器路径 ────────────────────────────────
    print("⑤ 正常路径：音色表 200 且已有同名 → 用服务器路径、不上传")
    fc = use(FakeClient(get_resp=FakeResp(200, payload={
        "voices": [{"name": NAME, "path": srv, "size_kb": 2.0}]})))
    r = call()
    d = r.json()
    check("⑤ HTTP 200", r.status_code == 200, r.text[:160])
    check("⑤ local_only=false", d.get("local_only") is False, str(d))
    check("⑤ path 是服务器路径", d.get("path") == srv, str(d.get("path")))
    check("⑤ 没有发上传请求", [c[0] for c in fc.calls] == ["GET"], str(fc.calls))

    # ── ⑥ 音色表 200 + 未命中 → 上传 200 ─────────────────────────────────
    print("⑥ 音色表 200 未命中 → 上传 200 → 用服务器返回值")
    fc = use(FakeClient(
        get_resp=FakeResp(200, payload={"voices": []}),
        post_resp=FakeResp(200, payload={"name": NAME, "path": srv, "size_kb": 2.0}),
    ))
    r = call()
    d = r.json()
    check("⑥ HTTP 200", r.status_code == 200, r.text[:160])
    check("⑥ 未降级（上传成功）", not_fallback(d), str(d))
    check("⑥ path 是服务器路径", d.get("path") == srv, str(d.get("path")))
    check("⑥ 新鲜上传成功时原样透传上游响应体（不注入字段）",
          set(d.keys()) == {"name", "path", "size_kb"}, str(sorted(d.keys())))

    # ── ⑦ 上传 409（并发已存在）→ 视为成功 ───────────────────────────────
    print("⑦ 上传 409（并发创建）→ 视为成功，local_only=false")
    fc = use(FakeClient(
        get_resp=FakeResp(200, payload={"voices": []}),
        post_resp=FakeResp(409, payload={"path": srv, "size_kb": 2.0}),
    ))
    r = call()
    d = r.json()
    check("⑦ HTTP 200", r.status_code == 200, r.text[:160])
    check("⑦ local_only=false（409 不是降级）", d.get("local_only") is False, str(d))
    check("⑦ path 来自 409 响应体", d.get("path") == srv, str(d.get("path")))

    # ── ⑧ 输入错误不能被降级吞掉 ────────────────────────────────────────
    print("⑧ 输入错误仍按原样报错（降级只针对「服务器不可用」）")
    use(FakeClient(get_resp=FakeResp(404, text=AUTODL_404)))
    r = client.post("/api/preset-voices/upload-to-tts", json={})
    check("⑧ 缺 name → 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    r = call("不存在的音色.mp3")
    check("⑧ 本地无该文件 → 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")

finally:
    presets.http_client = _ORIG_HTTP_CLIENT
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
