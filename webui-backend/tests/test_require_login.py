#!/usr/bin/env python3
"""MEMBER_REQUIRE_LOGIN=1（不扣费）强制登录单测（自跑脚本）。

用法：/Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_require_login.py
隔离：临时 DATA_DIR；REQUIRE_LOGIN=1 且 ENFORCE 未设（默认 0）。
验证：未登录提交 401；登录后提交放行且不扣积分。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-reqlogin-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_REQUIRE_LOGIN"] = "1"
os.environ["MEMBER_ENFORCE"] = "0"
os.environ["MEMBER_REG_BONUS"] = "100"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.membership import service, store  # noqa: E402

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


def make_lines(n_chars: int) -> list:
    text = "测" * n_chars
    return [{"speaker": "A", "text": text}]


def main():
    print("── REQUIRE_LOGIN=1 / ENFORCE=0 ──")
    check("开关状态正确", service.REQUIRE_LOGIN and not service.ENFORCE)

    print("── 未登录拒绝 ──")
    r = client.post("/api/queue/submit", json={
        "project_name": "anon", "kind": "mono", "lines": make_lines(100),
        "voices": {}, "silence": {}, "params": {},
    })
    check("未登录提交 401", r.status_code == 401, r.text)
    check("错误文案正确", "登录" in r.json().get("detail", ""), r.text)

    print("── 登录后放行且不扣费 ──")
    body = service.register("登录用户", "pass123")
    token = body["token"]
    points_before = body["user"]["points"]
    auth = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/queue/submit", json={
        "project_name": "authed", "kind": "mono", "lines": make_lines(3000),
        "voices": {}, "silence": {}, "params": {},
    }, headers=auth)
    check("登录后提交 200", r.status_code == 200, r.text)
    task_id = r.json().get("task_id", "")
    check("任务入队", bool(task_id))
    me = client.get("/api/auth/me", headers=auth).json()["user"]
    check("积分未被扣（仍为注册赠送）", me["points"] == points_before, f"{me['points']} vs {points_before}")
    check("无扣费流水", not any(e["user_id"] == body["user"]["user_id"] and e["kind"] == "spend"
                              for e in store.load_point_logs()))
    # 清理：取消任务避免残留
    client.post(f"/api/queue/{task_id}/cancel")

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
