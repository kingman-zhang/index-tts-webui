#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""术语表 HTTP 层自测：用户视角只回「我的词条」 / 写操作需登录 / 超管接口。

⚠️ 内置词库（全局库）对用户**不可见**：`GET /api/glossary` 不再下发 global/terms，
只回 mine。内置词依然参与合成（叠加逻辑在 stores.merge_glossary），
所以「覆盖 / 停用内置词」的行为用 `/api/glossary/apply` 的结果来验证。

只挂载 glossary 路由，不启动主应用（避免拉起队列 worker）。
    cd webui-backend && python tests/test_glossary_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

_TMP = tempfile.mkdtemp(prefix="glossary_api_")
# ⚠️ 必须钉 file 后端：本机 .env 若为 CONTENT_STORE=mongo，词条会写进开发库，
# 第二遍跑就多出一条残留（「mine 仍为空」「count=1」两条断言必挂）。
# DATA_DIR 只对 file 后端有效，光设它不足以隔离。
os.environ["DATA_DIR"] = _TMP
os.environ["CONTENT_STORE"] = "file"
os.environ["MEMBER_STORE"] = "file"
os.environ["SESSION_STORE"] = "file"
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import stores  # noqa: E402
from app.membership import session_store  # noqa: E402
from app.membership import store as mstore  # noqa: E402
from app.routes import glossary as glossary_routes  # noqa: E402

app = FastAPI()
app.include_router(glossary_routes.router)
client = TestClient(app)

USER = {"user_id": "u_api", "username": "tester", "nickname": "T",
        "points": 0, "disabled": False}
mstore.save_users({USER["user_id"]: USER})
# 会话自 Gate 3 起归 session_store 管（本测试只借它造一个有效 token）
session_store.put_session("tok_api", {"user_id": USER["user_id"], "created_at": ""}, 3600)

AUTH = {"Authorization": "Bearer tok_api"}
ADMIN = {"X-Admin-Token": "test-admin-token"}
BAD_ADMIN = {"X-Admin-Token": "wrong"}

# 内置库里放两条词：它们对用户不可见，但必须继续参与合成
BUILTIN = [
    {"original": "说服", "replacement": "说福"},
    {"original": "游说", "replacement": "游睡"},
]

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def mine(resp) -> list:
    return [(t["original"], t["replacement"]) for t in resp.json()["mine"]]


def applied(text: str, headers: dict) -> str:
    """合成口径（内置库 + 我的词条叠加后）的替换结果。"""
    r = client.post("/api/glossary/apply", json={"text": text}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["text"]


def main() -> int:
    print(f"临时数据目录: {_TMP}\n")

    stores.save_global_glossary(list(BUILTIN))

    print("── 1. GET 只回「我的词条」，内置库不下发 ──")
    r = client.get("/api/glossary")
    body = r.json()
    check("未登录可读", r.status_code == 200, str(r.status_code))
    check("未登录 logged_in=False", body["logged_in"] is False)
    check("未登录 mine 为空", body["mine"] == [], str(body["mine"]))
    check("响应不再含内置库字段", not ({"global", "terms", "global_count"} & set(body)),
          str(sorted(body.keys())))
    check("内置词的字面值不出现在响应里",
          "说福" not in r.text and "游睡" not in r.text, r.text[:200])

    r = client.get("/api/glossary", headers=AUTH)
    check("登录后 logged_in=True", r.json()["logged_in"] is True)
    check("登录后 mine 仍为空（只有用户自己加的）", r.json()["mine"] == [])

    print("\n── 2. 写操作需登录 ──")
    for method, url, payload in [
        ("post", "/api/glossary", {"original": "x", "replacement": "y"}),
        ("put", "/api/glossary", {"terms": []}),
        ("delete", "/api/glossary/说服", None),
    ]:
        r = getattr(client, method)(url, headers={}, **({"json": payload} if payload else {}))
        check(f"{method.upper()} {url} 未登录 → 401", r.status_code == 401, str(r.status_code))

    print("\n── 3. 写「我的词条」，不动全局库 ──")
    r = client.post("/api/glossary", json={"original": "锚点", "replacement": "毛点"},
                    headers=AUTH)
    check("新增成功", r.status_code == 200, str(r.status_code))
    check("mine 含锚点", ("锚点", "毛点") in mine(r), str(mine(r)))
    check("count = 我的词条条数 = 1", r.json()["count"] == 1, str(r.json()["count"]))
    check("mine 条目只含 original/replacement（不再带 source）",
          all(set(t) == {"original", "replacement"} for t in r.json()["mine"]),
          str(r.json()["mine"]))
    check("全局库未被污染",
          [t["original"] for t in stores.load_global_glossary()] == ["说服", "游说"])

    print("\n── 4. 覆盖内置词：对用户不可见，但合成时生效 ──")
    r = client.post("/api/glossary", json={"original": "说服", "replacement": "说佛"},
                    headers=AUTH)
    check("mine 里出现同名覆盖项", ("说服", "说佛") in mine(r), str(mine(r)))
    check("合成时用的是我的值", applied("说服力", AUTH) == "说佛力", applied("说服力", AUTH))
    check("全局库仍是原值", stores.load_global_glossary()[0]["replacement"] == "说福")

    print("\n── 5. 停用内置词（replacement 为空）──")
    r = client.post("/api/glossary", json={"original": "游说", "replacement": ""},
                    headers=AUTH)
    check("停用项留在 mine 中（可恢复）", ("游说", "") in mine(r), str(mine(r)))
    check("停用后合成不再替换", applied("去游说他", AUTH) == "去游说他", applied("去游说他", AUTH))

    print("\n── 6. 删除我的词条 → 回到内置值 ──")
    client.delete("/api/glossary/游说", headers=AUTH)
    check("内置词重新生效", applied("去游说他", AUTH) == "去游睡他", applied("去游说他", AUTH))
    client.delete("/api/glossary/说服", headers=AUTH)
    check("覆盖项删除后回到内置值", applied("说服力", AUTH) == "说福力", applied("说服力", AUTH))

    print("\n── 7. PUT 全量覆盖我的词条 ──")
    r = client.put("/api/glossary", json={"terms": [
        {"original": "处理", "replacement": "楚理"},
        {"original": "处理", "replacement": "楚理2"},   # 重复 → 后者胜
        {"original": "", "replacement": "空"},          # 脏数据 → 丢弃
    ]}, headers=AUTH)
    check("重复项去重", r.json()["count"] == 1, str(r.json()["mine"]))
    check("脏数据被丢弃", all(t["original"] for t in r.json()["mine"]))
    check("去重后取后者", mine(r) == [("处理", "楚理2")], str(mine(r)))

    print("\n── 8. apply 按用户视角替换 ──")
    check("用户词条生效", applied("我们要处理这个问题", AUTH) == "我们要楚理2这个问题",
          applied("我们要处理这个问题", AUTH))
    check("未登录/无个人词条 ⇒ 只剩内置库",
          applied("我们要处理这个问题", {}) == "我们要处理这个问题",
          applied("我们要处理这个问题", {}))

    print("\n── 9. 超管接口鉴权 ──")
    check("无 token → 401", client.get("/api/admin/glossary").status_code in (401, 403))
    check("错 token → 401", client.get("/api/admin/glossary", headers=BAD_ADMIN).status_code == 401)
    r = client.get("/api/admin/glossary", headers=ADMIN)
    check("正确 token → 200", r.status_code == 200, str(r.status_code))
    check("返回全局库原始值", r.json()["count"] == 2, str(r.json()))

    print("\n── 10. 超管改内置库：用户看不到，但合成生效 ──")
    r = client.post("/api/admin/glossary",
                    json={"original": "尽管", "replacement": "紧管"}, headers=ADMIN)
    check("超管新增成功", r.status_code == 200 and r.json()["count"] == 3, str(r.json()))
    r = client.get("/api/glossary", headers=AUTH)
    check("用户视角看不到新增的内置词",
          "紧管" not in r.text and all(t["original"] != "尽管" for t in r.json()["mine"]),
          r.text[:200])
    check("但合成时生效", applied("尽管很难", AUTH) == "紧管很难", applied("尽管很难", AUTH))
    r = client.delete("/api/admin/glossary/尽管", headers=ADMIN)
    check("超管删除成功", r.json()["count"] == 2, str(r.json()))

    print("\n" + "=" * 60)
    print(f"结果：通过 {PASS} / 失败 {FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
