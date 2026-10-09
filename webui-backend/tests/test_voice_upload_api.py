#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色管理 HTTP 层自测：上传 / 列表 scope / 改名 / 删除（2026-10-02 重构后）。

用法（在 webui-backend 下）：
    python tests/test_voice_upload_api.py

（本文件取代旧的 `test_voice_upload_local_copy.py`：那个文件锁的是「转发 tts 后
双写本地副本」——上传路径已改成「先写 backend 用户音色库、再广播到池内所有 local」，
`_save_local_copy` 这个函数连同它的前提一起不存在了。）

## 锁的行为（对应用户需求 1–5、7）

1. **上传**：落 `data/voices/<user_id>/<voice_id><ext>` + 该用户 `index.json`，
   并以 `{user_id}_{voice_id}{ext}` 广播到池内每一台 local；
2. **必须登录**：音色按用户归档，没有"匿名音色"这一档；
3. **列表分 scope**：tts-server 的音色标 `library`（仍可选用），backend 用户音色标
   `user` —— 「我的音色」只认后者（需求 4）；
4. **删除**：广播删服务器文件 → 删本地文件 → 索引标 `deleted_at`（需求 5）；
5. **改名只改元数据**：**一个 tts-server 请求都不发**（需求 7）。
   老结构音色改名时会同步迁移 `voices_meta.json` 的键（否则归属丢失、音色从列表消失）。

全程假 http_client + 临时 DATA_DIR：不触网、不碰仓库真实数据。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

_TMP = tempfile.mkdtemp(prefix="voice-api-")
os.environ["DATA_DIR"] = _TMP
os.environ["MEMBER_ENFORCE"] = "1"        # 打开数据隔离 ⇒ 音色按用户可见
os.environ["MEMBER_REQUIRE_LOGIN"] = "0"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import LOCAL_VOICES_DIR, PRESET_VOICES_DIR  # noqa: E402
from app.engines import voice_fanout, voice_sync  # noqa: E402
from app.membership import store as mstore  # noqa: E402
from app.routes import voices as voice_routes  # noqa: E402

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


# ── 用户 / 会话 ────────────────────────────────────────────────────────
USER = {"user_id": "u_api00000001", "username": "tester", "nickname": "T",
        "points": 0, "disabled": False}
OTHER = {"user_id": "u_api00000002", "username": "other", "nickname": "O",
         "points": 0, "disabled": False}
mstore.save_users({USER["user_id"]: USER, OTHER["user_id"]: OTHER})
mstore.save_sessions({
    "tok_api": {"user_id": USER["user_id"], "created_at": "", "expires_ts": time.time() + 3600},
    "tok_other": {"user_id": OTHER["user_id"], "created_at": "", "expires_ts": time.time() + 3600},
})
AUTH = {"Authorization": "Bearer tok_api"}
AUTH_OTHER = {"Authorization": "Bearer tok_other"}


# ── 假 http_client（广播 + 列表）────────────────────────────────────────
class Resp:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


class FakeTTS:
    """记录所有调用；可按前缀配置单台的行为。"""

    def __init__(self, listed=None):
        self.listed = listed or []
        self.calls: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()

    async def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return Resp(200, {"voices": self.listed, "count": len(self.listed)})

    async def post(self, url, files=None, data=None, timeout=None):
        name = ""
        if files and "file" in files:
            f = files["file"]
            name = f[0] if isinstance(f, tuple) else getattr(f, "name", "")
        self.calls.append(("POST", f"{url}#{name}"))
        for prefix in self.fail_on:
            if url.startswith(prefix):
                return Resp(503, None, "服务不可用")
        return Resp(200, {"name": name, "path": f"/server/voices/{name}"})

    async def delete(self, url, timeout=None):
        self.calls.append(("DELETE", url))
        for prefix in self.fail_on:
            if url.startswith(prefix):
                return Resp(503, None, "服务不可用")
        return Resp(200, {"deleted": url.rsplit("/", 1)[-1]})


FAKE_A = "http://gpu-a:8000"
FAKE_B = "http://gpu-b:8000"
tts = FakeTTS(listed=[{"name": "预设A.mp3", "path": "/s/voices/预设A.mp3", "source": "custom"},
                      {"name": "u_other_voc_zzz.wav", "path": "/s/voices/u_other_voc_zzz.wav"}])
voice_routes.http_client = tts


async def _targets():
    return [FAKE_A, FAKE_B]


voice_fanout.local_targets = _targets   # 不读真实资源池、不触网

app = FastAPI()
app.include_router(voice_routes.router)
client = TestClient(app)


def main() -> int:
    voice_sync.reset_cache()

    print("── 1. 未登录一律拒绝 ──")
    r = client.post("/api/voices/upload", files={"file": ("a.wav", b"RIFFxx", "audio/wav")})
    check("未登录上传 → 401", r.status_code, 401)
    r = client.post("/api/voices/rename", json={"voice_id": "voc_0123456789ab", "new_name": "x"})
    check("未登录改名 → 401", r.status_code, 401)
    r = client.delete("/api/voices/voc_0123456789ab")
    check("未登录删除 → 401", r.status_code, 401)

    print("── 2. 上传 ──")
    tts.calls.clear()
    r = client.post("/api/voices/upload",
                    files={"file": ("我的录音.wav", b"RIFF" + b"\x00" * 100, "audio/wav")},
                    data={"name": "我的音色A"}, headers=AUTH)
    check("上传 200", r.status_code, 200)
    body = r.json()
    vid = body["voice_id"]
    check_true("返回音色 id", vid.startswith("voc_"))
    check("显示名", body["name"], "我的音色A")
    check("服务器名 = {user_id}_{voice_id}.wav", body["server_name"], f"{USER['user_id']}_{vid}.wav")
    check_true("文件落在用户目录下", body["path"].startswith(str(Path(_TMP) / "voices" / USER["user_id"])))
    check_true("磁盘上真有文件", Path(body["path"]).is_file())
    check("广播到两台都成功", body["broadcast_failed"], {})

    posts = [c for c in tts.calls if c[0] == "POST"]
    check("向两台 local 各发一次上传", len(posts), 2)
    check_true("上传用的是服务器名（不是显示名）",
               all(c[1].endswith(f"#u_api00000001_{vid}.wav") for c in posts))

    index = json.loads((Path(_TMP) / "voices" / USER["user_id"] / "index.json").read_text(encoding="utf-8"))
    check("索引写了 1 条", len(index["voices"]), 1)
    check("索引里的显示名", index["voices"][0]["name"], "我的音色A")
    check("索引里的 id 与返回一致", index["voices"][0]["id"], vid)

    print("── 3. 校验分支 ──")
    r = client.post("/api/voices/upload", files={"file": ("x.wav", b"RIFF", "audio/wav")},
                    data={"name": "我的音色A"}, headers=AUTH)
    check("重名 → 409", r.status_code, 409)
    r = client.post("/api/voices/upload", files={"file": ("x.txt", b"hi", "text/plain")}, headers=AUTH)
    check("非法扩展名 → 400", r.status_code, 400)
    r = client.post("/api/voices/upload", files={"file": ("x.wav", b"", "audio/wav")}, headers=AUTH)
    check("空内容 → 400", r.status_code, 400)
    saved_limit = voice_routes.MAX_UPLOAD_BYTES
    voice_routes.MAX_UPLOAD_BYTES = 16
    try:
        r = client.post("/api/voices/upload", files={"file": ("big.wav", b"x" * 32, "audio/wav")},
                        data={"name": "超大"}, headers=AUTH)
        check("超过大小上限 → 413", r.status_code, 413)
    finally:
        voice_routes.MAX_UPLOAD_BYTES = saved_limit

    print("── 4. 列表：scope 区分「我的」与「共享池」 ──")
    r = client.get("/api/voices", headers=AUTH)
    check("列表 200", r.status_code, 200)
    items = r.json()["voices"]
    by_name = {v["name"]: v for v in items}
    check("tts-server 的音色标 library", by_name["预设A.mp3"]["scope"], "library")
    check_true("别的用户的历史音色也在 library", by_name["u_other_voc_zzz.wav"]["scope"] == "library")
    check("我上传的标 user", by_name["我的音色A"]["scope"], "user")
    check("我的音色带 voice_id", by_name["我的音色A"]["voice_id"], vid)
    check("我的音色带 preview_name（试听用文件名）", by_name["我的音色A"]["preview_name"], f"{vid}.wav")
    check("「我的音色」= 只有 scope=user 的",
          sorted(v["name"] for v in items if v["scope"] == "user"), ["我的音色A"])

    r = client.get("/api/voices", headers=AUTH_OTHER)
    check("另一个用户看不到我的音色",
          [v["name"] for v in r.json()["voices"] if v["scope"] == "user"], [])

    print("── 5. 改名 = 纯元数据（一个 tts 请求都不发）──")
    # 先传第二个音色，用来制造「与自己库里重名」的场景
    client.post("/api/voices/upload",
                files={"file": ("d.wav", b"RIFF" + b"\x00" * 6, "audio/wav")},
                data={"name": "第二个音色"}, headers=AUTH)
    tts.calls.clear()
    r = client.post("/api/voices/rename", json={"voice_id": vid, "new_name": "改过的名字"}, headers=AUTH)
    check("改名 200", r.status_code, 200)
    check("返回新名字", r.json()["name"], "改过的名字")
    check("改名没有碰 tts-server（需求 7 的核心）", tts.calls, [])
    check_true("本地文件没被改名", Path(body["path"]).is_file())
    r = client.post("/api/voices/rename", json={"voice_id": vid, "new_name": "第二个音色"}, headers=AUTH)
    check("与自己库里另一个音色重名 → 409", r.status_code, 409)
    r = client.post("/api/voices/rename", json={"voice_id": vid, "new_name": "改过的名字"}, headers=AUTH)
    check("改成当前名字是幂等的", r.status_code, 200)
    r = client.post("/api/voices/rename", json={"voice_id": "voc_ffffffffffff", "new_name": "x"}, headers=AUTH)
    check("改别人的/不存在的 id → 404", r.status_code, 404)
    r = client.get("/api/voices", headers=AUTH)
    check("改名后列表里是新名",
          {v["name"] for v in r.json()["voices"] if v["scope"] == "user"},
          {"改过的名字", "第二个音色"})
    check("改名过程只发生过列表查询（没有 POST/DELETE）",
          sorted({c[0] for c in tts.calls}), ["GET"])

    print("── 6. 端到端：上传完 voice_sync 能拿它当源文件 ──")
    voice_sync.reset_cache()
    loc = voice_sync.locate_voice(body["path"])
    check_true("locate_voice 命中本地文件", loc.local_file is not None)
    check("判为 user 类", loc.kind, voice_sync.USER)
    check("服务器名与上传时一致",
          voice_sync.target_server_name(Path(body["path"]).name, loc.kind, USER["user_id"],
                                        id_layout=voice_sync._is_id_layout(loc.local_file)),
          body["server_name"])

    print("── 7. 删除 = 广播 + 删本地 + 索引标记 ──")
    tts.calls.clear()
    r = client.delete(f"/api/voices/{vid}", headers=AUTH)
    check("删除 200", r.status_code, 200)
    check("广播了两台", len([c for c in tts.calls if c[0] == "DELETE"]), 2)
    check_true("删的是服务器名",
               all(c[1] == f"{FAKE_A}/api/voices/{body['server_name']}" or
                   c[1] == f"{FAKE_B}/api/voices/{body['server_name']}"
                   for c in tts.calls if c[0] == "DELETE"))
    check("本地文件已删", Path(body["path"]).exists(), False)
    idx = json.loads((Path(_TMP) / "voices" / USER["user_id"] / "index.json").read_text(encoding="utf-8"))
    check_true("索引标了 deleted_at", idx["voices"][0]["deleted_at"] is not None)
    r = client.get("/api/voices", headers=AUTH)
    check("列表里已消失", [v["name"] for v in r.json()["voices"] if v.get("voice_id") == vid], [])
    r = client.delete(f"/api/voices/{vid}", headers=AUTH)
    check("重复删除 → 404", r.status_code, 404)

    print("── 8. 一台 tts 挂了不影响上传/删除结论 ──")
    tts.fail_on = {FAKE_B}
    r = client.post("/api/voices/upload",
                    files={"file": ("b.wav", b"RIFF" + b"\x00" * 10, "audio/wav")},
                    data={"name": "只有 A 成功的"}, headers=AUTH)
    check("上传仍 200", r.status_code, 200)
    check_true("失败明细里有 gpu-b", FAKE_B in r.json()["broadcast_failed"])
    check_true("失败明细里没有 gpu-a", FAKE_A not in r.json()["broadcast_failed"])
    tts.fail_on = set()

    print("── 9. 老结构（平铺文件）仍然可用 ──")
    LOCAL_VOICES_DIR.mkdir(parents=True, exist_ok=True)
    old_file = LOCAL_VOICES_DIR / "老音色.wav"
    old_file.write_bytes(b"RIFF" + b"\x00" * 8)
    meta_path = Path(_TMP) / "voices_meta.json"
    meta_path.write_text(json.dumps({"老音色.wav": {"owner_id": USER["user_id"], "uploaded_at": "x"}},
                                    ensure_ascii=False), encoding="utf-8")

    r = client.get("/api/voices", headers=AUTH)
    check_true("老结构音色出现在「我的音色」",
               "老音色.wav" in {v["name"] for v in r.json()["voices"] if v["scope"] == "user"})
    check_true("老结构音色没有 voice_id（前端要用文件名当 key）",
               all(v.get("voice_id") is None
                   for v in r.json()["voices"] if v["name"] == "老音色.wav"))

    tts.calls.clear()
    r = client.post("/api/voices/rename", json={"old_name": "老音色.wav", "new_name": "老音色改"},
                    headers=AUTH)
    check("老结构改名 200", r.status_code, 200)
    check("返回新文件名", r.json()["name"], "老音色改.wav")
    check("改名不碰 tts-server（老任务路径才不会失效）", tts.calls, [])
    check("文件已重命名", (LOCAL_VOICES_DIR / "老音色改.wav").is_file(), True)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    check("归属记录的键跟着迁移（否则音色会从列表消失）", list(meta), ["老音色改.wav"])

    tts.calls.clear()
    r = client.delete("/api/voices/老音色改.wav", headers=AUTH)
    check("老结构删除 200", r.status_code, 200)
    check("广播名用老规则 {名}__{owner}",
          [c[1] for c in tts.calls if c[0] == "DELETE"],
          [f"{FAKE_A}/api/voices/老音色改__{USER['user_id']}.wav",
           f"{FAKE_B}/api/voices/老音色改__{USER['user_id']}.wav"])
    check("本地文件已删", (LOCAL_VOICES_DIR / "老音色改.wav").exists(), False)
    check("归属记录已清", json.loads(meta_path.read_text(encoding="utf-8")), {})

    print("── 10. 预设音色不能删 ──")
    PRESET_VOICES_DIR.mkdir(parents=True, exist_ok=True)
    (PRESET_VOICES_DIR / "内置.mp3").write_bytes(b"RIFF")
    r = client.delete("/api/voices/内置.mp3", headers=AUTH)
    check("删预设 → 400", r.status_code, 400)

    print("── 11. 试听：只读 backend 本地，不代理 tts-server ──")
    r = client.post("/api/voices/upload",
                    files={"file": ("c.wav", b"RIFF" + b"\x00" * 20, "audio/wav")},
                    data={"name": "试听用"}, headers=AUTH)
    preview_name = r.json()["voice_id"] + ".wav"
    tts.calls.clear()
    r = client.get(f"/api/audio/{preview_name}")
    check("backend 本地取到（data/voices/<uid>/ 子目录）", r.status_code, 200)
    check_true("返回的是音频字节", r.content.startswith(b"RIFF"))
    check("试听未打 tts-server", [c for c in tts.calls if "/api/audio/" in c[1]], [])

    # 2026-10-09（用户决定）：以 backend 为准，**去掉壳兜底**。
    # 壳的音色表里有 `u_other_voc_zzz.wav`（见 FakeTTS.listed），backend 本地没有
    # ⇒ 必须 404，且**不去代理**。旧实现会先打壳并把它代理回来。
    r = client.get("/api/audio/u_other_voc_zzz.wav")
    check("壳上有、backend 没有 → 404（不再回退代理）", r.status_code, 404)
    check("404 时也没有打 tts-server", [c for c in tts.calls if "/api/audio/" in c[1]], [])

    print(f"\n{'=' * 46}\nTOTAL {PASS + FAIL}  PASS {PASS}  FAIL {FAIL}\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
