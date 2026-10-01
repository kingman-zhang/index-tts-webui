#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色上传「双写本地副本」自测（app/routes/voices.py 的 upload_voice / _save_local_copy）。

用法（在 webui-backend 下）：
    python tests/test_voice_upload_local_copy.py

## 为什么锁这条行为（2026-10-01 实测）

前端「上传音色」→ `POST /api/voices/upload` → 只转发到 `{TTS_URL}/api/voices/upload`
（`TTS_URL` = 池内**第一个** local）。而合成时 `voice_sync.ensure_voice_on_server()`
补传**别的** tts-server 时，源文件**只从 backend 本地取**（`locate_voice` 在
preset-voices / breezeblue/audio / voices 四处按 basename 找）。

于是「转发成功但不留本地副本」= 该音色成为**单点**：某段一旦被调度到池内其它
tts-server，补传无原料 ⇒ `VoiceUnavailable` ⇒ 被池包成 `NonRetryableSynthesisError`
⇒ **整单失败**（已付费的其它段一起作废）。

对照：BreezeBlue 与预设音色 backend 本来就有副本 ⇒ 缺了能自愈；UI 上传的不能。

## 覆盖

1. 转发成功（200）后**必须**留本地副本，并写归属；返回仍带 tts 的 path
2. 本地已有同名文件 → **不覆盖**、不报错
3. 本地副本写入失败（OSError）→ **仍返回 tts 结果**（不让上传假装失败）
4. 未登录（user=None）→ 照样留副本，但不写归属
5. tts 不可达 → 回退本地（原行为不变）
6. 端到端：`voice_sync.locate_voice()` 能把这个副本当源文件（这才是留副本的目的）

全程假 httpx client：不触网、不依赖 tts-server、不读真实音色表。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ⚠️ 必须在 import app.* 之前决定 DATA_DIR：LOCAL_VOICES_DIR 是在 config 导入时就定下来的。
# 没给就自建临时目录，保证**绝不写进仓库真实的 data/voices/**。
if not os.environ.get("DATA_DIR"):
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="test-voice-upload-")

from starlette.datastructures import UploadFile  # noqa: E402

from app.routes import voices  # noqa: E402

PASS = FAIL = SKIP = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        print(f"  \u2717 {name}\n      期望: {expected!r}\n      实际: {actual!r}")


# ── 假的 httpx.AsyncClient ──────────────────────────────────────────────


class FakeResp:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, resp: FakeResp | None = None, raise_exc: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._resp = resp if resp is not None else FakeResp()
        self._raise = raise_exc

    async def post(self, url: str, **kw):
        self.calls.append((url, kw))
        if self._raise is not None:
            raise self._raise
        return self._resp


def make_file(name: str = "voice.wav", data: bytes = b"FAKE-AUDIO") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def call_upload(client, *, filename: str = "voice.wav", name: str | None = None,
                user=None, data: bytes = b"FAKE-AUDIO"):
    """直接调端点函数（不经过 FastAPI 路由），只把 http_client 换成假的。"""
    with patch.object(voices, "http_client", client):
        return asyncio.run(
            voices.upload_voice(file=make_file(filename, data), name=name, user=user)
        )


def read_meta() -> dict:
    p = voices.VOICES_META_PATH
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# ── 六个部分 ────────────────────────────────────────────────────────────


def part1_keeps_local_copy() -> None:
    print("\n1. 转发成功（200）后必须留本地副本")
    client = FakeClient(FakeResp(200, {
        "name": "voice", "path": "/tts/voices/我的音色.wav", "size_kb": 1.0,
    }))
    res = call_upload(client, filename="voice.wav", name="我的音色",
                      user={"user_id": "u_1"})

    dest = voices.LOCAL_VOICES_DIR / "我的音色.wav"
    check("转发目标就是 TTS_URL", client.calls[0][0], f"{voices.TTS_URL}/api/voices/upload")
    check("自定义名生效（原名+扩展名）", dest.name, "我的音色.wav")
    check("本地副本已写入", dest.is_file(), True)
    check("本地副本内容与上传一致", dest.read_bytes(), b"FAKE-AUDIO")
    check("返回仍用 tts 的 path（前端行为不变）",
          res["path"], "/tts/voices/我的音色.wav")
    check("返回里额外带 local_path", res.get("local_path"), str(dest))
    check("归属已记录", (read_meta().get("我的音色.wav") or {}).get("owner_id"), "u_1")


def part2_existing_not_overwritten() -> None:
    print("\n2. 本地已有同名文件 → 不覆盖、不报错")
    dest = voices.LOCAL_VOICES_DIR / "dup.wav"
    dest.write_bytes(b"OLD")
    client = FakeClient(FakeResp(200, {"name": "dup.wav", "path": "/tts/voices/dup.wav"}))
    res = call_upload(client, filename="dup.wav", data=b"NEW", user={"user_id": "u_2"})

    check("仍返回 tts 的 path", res["path"], "/tts/voices/dup.wav")
    check("本地内容没被覆盖（可能属于别人）", dest.read_bytes(), b"OLD")
    check("不带 local_path（没新写）", "local_path" in res, False)


def part3_local_write_failure_still_ok() -> None:
    print("\n3. 本地副本写入失败 → 仍返回 tts 结果（上传本身是成功的）")
    client = FakeClient(FakeResp(200, {"name": "boom.wav", "path": "/tts/voices/boom.wav"}))
    real_write = Path.write_bytes

    def boom(self: Path, data: bytes):
        if self.name == "boom.wav":
            raise OSError("No space left on device")
        return real_write(self, data)

    with patch.object(Path, "write_bytes", boom):
        res = call_upload(client, filename="boom.wav")

    check("上传仍算成功", res["path"], "/tts/voices/boom.wav")
    check("没有抛异常、也没有 local_path", res.get("local_path"), None)
    check("本地确实没写成功", (voices.LOCAL_VOICES_DIR / "boom.wav").exists(), False)


def part4_anonymous() -> None:
    print("\n4. 未登录（user=None）→ 照样留副本，但不写归属")
    client = FakeClient(FakeResp(200, {"name": "anon.wav", "path": "/tts/voices/anon.wav"}))
    call_upload(client, filename="anon.wav", user=None)

    check("副本已写入", (voices.LOCAL_VOICES_DIR / "anon.wav").is_file(), True)
    check("meta 里没有它", "anon.wav" in read_meta(), False)


def part5_tts_unreachable_fallback() -> None:
    print("\n5. tts 不可达 → 回退本地（原有行为不变）")
    import httpx

    client = FakeClient(raise_exc=httpx.ConnectError("boom"))
    res = call_upload(client, filename="fb.wav")
    dest = voices.LOCAL_VOICES_DIR / "fb.wav"

    check("本地已保存", dest.read_bytes(), b"FAKE-AUDIO")
    check("返回本地路径", res["path"], str(dest))


def part6_voice_sync_can_use_it() -> None:
    print("\n6. 端到端：留副本的目的就是让 voice_sync 能把它当源文件")
    from app.engines import voice_sync

    dest = voices.LOCAL_VOICES_DIR / "chain.wav"
    dest.write_bytes(b"X")
    loc = voice_sync.locate_voice(str(dest))

    check("被识别为用户独有（不是「找不到」）", loc.kind, voice_sync.USER)
    check("源文件就位", loc.local_file, dest)
    check("目标机上的名字带隔离后缀",
          voice_sync.target_server_name(dest.name, loc.kind, "u_79b7cdf26bad"),
          "chain__u_79b7cdf26bad.wav")


def main() -> int:
    print("=" * 62)
    print("音色上传「双写本地副本」自测")
    print("=" * 62)
    print(f"DATA_DIR = {os.environ['DATA_DIR']}")
    part1_keeps_local_copy()
    part2_existing_not_overwritten()
    part3_local_write_failure_still_ok()
    part4_anonymous()
    part5_tts_unreachable_fallback()
    part6_voice_sync_can_use_it()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项，跳过 {SKIP} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
