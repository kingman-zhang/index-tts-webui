#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""术语表 HTTP 层自测：用户视角 / 写操作需登录 / 超管接口。

只挂载 glossary 路由，不启动主应用（避免拉起队列 worker）。
    cd webui-backend && python tests/test_glossary_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

_TMP = tempfile.mkdtemp(prefix="glossary_api_")
os.environ["DATA_DIR"] = _TMP
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import stores  # noqa: E402
from app.membership import store as mstore  # noqa: E402
from app.routes import glossary as glossary_routes  # noqa: E402

app = FastAPI()
app.include_router(glossary_routes.router)
client = TestClient(app)

USER = {"user_id": "u_api", "username": "tester", "nickname": "T",
        "points": 0, "disabled": False}
mstore.save_users({USER["user_id"]: USER})
mstore.save_sessions({"tok_api": {"user_id": USER["user_id"],
                                 "created_at": "", "expires_ts": time.time() + 3600}})

AUTH = {"Authorization": "Bearer tok_api"}
ADMIN = {"X-Admin-Token": "test-admin-token"}
BAD_ADMIN = {"X-Admin-Token": "wrong"}

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def effective(resp) -> list:
    return [(t["original"], t["replacement"]) for t in resp.json()["terms"]]


def main() -> int:
    print(f"临时数据目录: {_TMP}\n")

    stores.save_global_glossary([
        {"original": "说服", "replacement": "说福"},
        {"original": "游说", "replacement": "游睡"},
    ])

    print("── 1. 读取：未登录 vs 登录 ──")
    r = client.get("/api/glossary")
    check("未登录可读", r.status_code == 200, str(r.status_code))
    check("未登录 logged_in=False", r.json()["logged_in"] is False)
    check("未登录只有全局库", effective(r) == [("说服", "说福"), ("游说", "游睡")],
          str(effective(r)))

    r = client.get("/api/glossary", headers=AUTH)
    check("登录后 logged_in=True", r.json()["logged_in"] is True)

    print("\n── 2. 写操作需登录 ──")
    for method, url, body in [
        ("post", "/api/glossary", {"original": "x", "replacement": "y"}),
        ("put", "/api/glossary", {"terms": []}),
        ("delete", "/api/glossary/说服", None),
    ]:
        r = getattr(client, method)(url, headers={}, **({"json": body} if body else {}))
        check(f"{method.upper()} {url} 未登录 → 401", r.status_code == 401, str(r.status_code))

    print("\n── 3. 登录后写「我的词条」，不动全局库 ──")
    r = client.post("/api/glossary", json={"original": "锚点", "replacement": "毛点"},
                    headers=AUTH)
    check("新增成功", r.status_code == 200, str(r.status_code))
    check("我的词条含锚点", ("锚点", "毛点") in effective(r), str(effective(r)))
    check("全局库未被污染",
          [t["original"] for t in stores.load_global_glossary()] == ["说服", "游说"])
    check("锚点为用户独有（source=user）",
          [t["source"] for t in r.json()["terms"] if t["original"] == "锚点"] == ["user"])

    print("\n── 4. 用户同名覆盖内置 ──")
    r = client.post("/api/glossary", json={"original": "说服", "replacement": "说佛"},
                    headers=AUTH)
    check("覆盖生效", ("说服", "说佛") in effective(r), str(effective(r)))
    check("覆盖项标记 source=user",
          [t["source"] for t in r.json()["terms"] if t["original"] == "说服"] == ["user"])
    check("全局库仍是原值", stores.load_global_glossary()[0]["replacement"] == "说福")

    print("\n── 5. 停用内置词（replacement 为空）──")
    r = client.post("/api/glossary", json={"original": "游说", "replacement": ""},
                    headers=AUTH)
    check("停用后该词移出生效词表",
          "游说" not in [o for o, _ in effective(r)], str(effective(r)))
    check("停用项仍出现在 mine 中（可恢复）",
          any(t["original"] == "游说" for t in r.json()["mine"]))

    print("\n── 6. 删除我的词条 → 恢复内置 ──")
    r = client.delete("/api/glossary/游说", headers=AUTH)
    check("删除后内置重新生效", ("游说", "游睡") in effective(r), str(effective(r)))
    r = client.delete("/api/glossary/说服", headers=AUTH)
    check("删除覆盖后回到内置值", ("说服", "说福") in effective(r), str(effective(r)))

    print("\n── 7. PUT 全量覆盖我的词条 ──")
    r = client.put("/api/glossary", json={"terms": [
        {"original": "处理", "replacement": "楚理"},
        {"original": "处理", "replacement": "楚理2"},   # 重复 → 后者胜
        {"original": "", "replacement": "空"},          # 脏数据 → 丢弃
    ]}, headers=AUTH)
    check("重复项去重", r.json()["user_count"] == 1, str(r.json()["mine"]))
    check("脏数据被丢弃", all(t["original"] for t in r.json()["mine"]))

    print("\n── 8. apply 按用户视角替换 ──")
    r = client.post("/api/glossary/apply",
                    json={"text": "我们要处理这个问题，也要游说对方"},
                    headers=AUTH)
    check("用户词条生效", "楚理" in r.json()["text"], r.json()["text"])
    check("未覆盖的词仍走内置", "游睡" in r.json()["text"], r.json()["text"])
    r2 = client.post("/api/glossary/apply",
                     json={"text": "我们要处理这个问题"}, headers={})
    check("未登录只用全局库", "楚理" not in r2.json()["text"], r2.json()["text"])

    print("\n── 9. 超管接口鉴权 ──")
    check("无 token → 401", client.get("/api/admin/glossary").status_code in (401, 403))
    check("错 token → 401", client.get("/api/admin/glossary", headers=BAD_ADMIN).status_code == 401)
    r = client.get("/api/admin/glossary", headers=ADMIN)
    check("正确 token → 200", r.status_code == 200, str(r.status_code))
    check("返回全局库原始值", r.json()["count"] == 2, str(r.json()))

    print("\n── 10. 超管改全局库 → 用户视角立即可见 ──")
    r = client.post("/api/admin/glossary",
                    json={"original": "尽管", "replacement": "紧管"}, headers=ADMIN)
    check("超管新增成功", r.status_code == 200 and r.json()["count"] == 3, str(r.json()))
    r = client.get("/api/glossary")
    check("未登录用户也能看到新内置词", ("尽管", "紧管") in effective(r), str(effective(r)))
    r = client.delete("/api/admin/glossary/尽管", headers=ADMIN)
    check("超管删除成功", r.json()["count"] == 2, str(r.json()))

    print("\n" + "=" * 60)
    print(f"结果：通过 {PASS} / 失败 {FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
