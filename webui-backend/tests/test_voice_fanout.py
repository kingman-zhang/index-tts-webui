#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色广播（app/engines/voice_fanout.py）自测。

用法（在 webui-backend 下）：
    python tests/test_voice_fanout.py

## 锁的行为（2026-10-02，用户需求第 2/5 条）

上传/删除音色时要把池内**每一台在跑的** local tts-server 拉齐：

1. 只推 `provider=local` 的资源（云端引擎不落地文件，收不到也不需要）；
   `base_url` 去重、保序；
2. **一台失败不影响其它台**，更不影响上传本身 —— 广播是尽力而为的一致性优化，
   漏掉的那台由 `voice_sync` 在首次合成时按需补传；
3. 删除时 404 也算成功（目标状态本来就是这个）；
4. 上传时 409（服务器已有同名）也算成功（重复广播 / 之前补传过）；
5. 本地源文件读不到 → 每一台都记一条失败原因，**不抛异常**（调用方要能继续）。

全程假 client，不触网。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if not os.environ.get("DATA_DIR"):
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="test-voice-fanout-")

from app.engines import voice_fanout  # noqa: E402
from app.engines.base import ResourceConfig  # noqa: E402

PASS = FAIL = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         expect: {expected!r}\n         actual: {actual!r}")


def check_true(name: str, cond: bool) -> None:
    check(name, bool(cond), True)


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class Recorder:
    """按 URL 前置字符串分派结果的假 client。"""

    def __init__(self, plan: dict[str, object]):
        self.plan = plan
        self.calls: list[tuple[str, str]] = []

    def _pick(self, url: str):
        for prefix, outcome in self.plan.items():
            if url.startswith(prefix):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return FakeResponse(200)

    async def post(self, url, files=None, data=None, timeout=None):
        name = ""
        if files and "file" in files:
            f = files["file"]
            name = f[0] if isinstance(f, tuple) else getattr(f, "name", "")
        self.calls.append(("POST", f"{url}#{name}"))
        return self._pick(url)

    async def delete(self, url, timeout=None):
        self.calls.append(("DELETE", url))
        return self._pick(url)


def fake_registry(providers: list[tuple[str, str]]):
    """(provider, base_url) 列表 → 假的 registry 结构。"""
    resources = []
    for idx, (provider, url) in enumerate(providers):
        cfg = ResourceConfig(id=f"r{idx}", provider=provider, base_url=url,
                             api_key_env="K" if provider != "local" else None)
        resources.append((cfg, SimpleNamespace()))
    return SimpleNamespace(engines=[SimpleNamespace(resources=resources)])


def with_pool(providers):
    return patch("app.engines.factory.build_registry", lambda *a, **k: fake_registry(providers))


def main() -> int:
    src = Path(os.environ["DATA_DIR"]) / "voc_0123456789ab.wav"
    src.write_bytes(b"RIFF" + b"\x00" * 32)
    name = "u_abc_voc_0123456789ab.wav"

    print("── 1. 只挑 local、去重、保序 ──")
    with with_pool([
        ("local", "http://gpu-a:8000"),
        ("302ai", "https://api.302.ai"),
        ("local", "http://gpu-b:8000"),
        ("local", "http://gpu-a:8000/"),      # 尾斜杠 → 同一个
        ("art", "https://art.example.com"),
    ]):
        targets = asyncio.run(voice_fanout.local_targets())
    check("目标只有 local 且去重", targets, ["http://gpu-a:8000", "http://gpu-b:8000"])

    with patch("app.engines.factory.build_registry", side_effect=ValueError("配置写坏了")):
        check("池构建失败 → 空列表（不抛）", asyncio.run(voice_fanout.local_targets()), [])

    print("── 2. 广播上传 ──")
    with with_pool([("local", "http://gpu-a:8000"), ("local", "http://gpu-b:8000")]):
        client = Recorder({
            "http://gpu-a:8000": FakeResponse(200, '{"path":"/v/x.wav"}'),
            "http://gpu-b:8000": FakeResponse(409, "已存在"),
        })
        res = asyncio.run(voice_fanout.broadcast_upload(client, src, name))
    check("两台都算成功", res, {"http://gpu-a:8000": "ok", "http://gpu-b:8000": "ok(已存在)"})
    check("上传时把目标名作为文件名", client.calls[0], ("POST", f"http://gpu-a:8000/api/voices/upload#{name}"))

    print("── 3. 一台挂了不影响其它台 ──")
    import httpx

    with with_pool([("local", "http://gpu-a:8000"), ("local", "http://gpu-b:8000")]):
        client = Recorder({
            "http://gpu-a:8000": httpx.ConnectError("no route"),
            "http://gpu-b:8000": FakeResponse(200, "{}"),
        })
        res = asyncio.run(voice_fanout.broadcast_upload(client, src, name))
    check("gpu-b 成功", res.get("http://gpu-b:8000"), "ok")
    check_true("gpu-a 记了失败原因", res.get("http://gpu-a:8000", "").startswith("连接失败"))

    with with_pool([("local", "http://gpu-a:8000")]):
        client = Recorder({"http://gpu-a:8000": FakeResponse(500, "boom")})
        res = asyncio.run(voice_fanout.broadcast_upload(client, src, name))
    check_true("HTTP 500 记成失败而不是抛异常", "HTTP 500" in res["http://gpu-a:8000"])

    print("── 4. 本地源文件读不到 ──")
    with with_pool([("local", "http://gpu-a:8000"), ("local", "http://gpu-b:8000")]):
        client = Recorder({})
        res = asyncio.run(voice_fanout.broadcast_upload(client, Path("/nope/missing.wav"), name))
    check("两台都记失败原因", len(res), 2)
    check_true("一个请求都没发", client.calls == [])
    check_true("原因是读取失败", all("读取本地文件失败" in v for v in res.values()))

    print("── 5. 广播删除：200 / 404 / 405 都算成功 ──")
    with with_pool([("local", "http://gpu-a:8000"), ("local", "http://gpu-b:8000"), ("local", "http://gpu-c:8000")]):
        client = Recorder({
            "http://gpu-a:8000": FakeResponse(200, '{"deleted":"x"}'),
            "http://gpu-b:8000": FakeResponse(404, "不存在"),
            "http://gpu-c:8000": FakeResponse(405, "method not allowed"),
        })
        res = asyncio.run(voice_fanout.broadcast_delete(client, name))
    check("三台都算成功", set(res.values()), {"ok"})
    check("删除走 DELETE /api/voices/{name}", client.calls[0], ("DELETE", f"http://gpu-a:8000/api/voices/{name}"))

    print("── 6. 空池 = 不做任何事 ──")
    with with_pool([("302ai", "https://api.302.ai")]):
        client = Recorder({})
        res = asyncio.run(voice_fanout.broadcast_delete(client, name))
    check("没有 local → 空结果", res, {})
    check("没有发出任何请求", client.calls, [])

    print(f"\n{'=' * 46}\nTOTAL {PASS + FAIL}  PASS {PASS}  FAIL {FAIL}\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
