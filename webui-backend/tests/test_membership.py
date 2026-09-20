#!/usr/bin/env python3
"""会员模块单测（自跑脚本，项目惯例非 pytest）。

用法：/Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_membership.py
隔离：临时 DATA_DIR，不污染真实数据。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-member-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"
os.environ["MEMBER_REG_BONUS"] = "100"
os.environ["MEMBER_CHECKIN_BONUS"] = "5"
os.environ["MEMBER_ENFORCE"] = "0"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.membership import service  # noqa: E402
from app.membership.service import MemberError  # noqa: E402

client = TestClient(app)
ADMIN = {"X-Admin-Token": "test-admin-token"}

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


def svc_register(username, password, nickname=""):
    """直接调 service 层注册（HTTP 用户名注册端点已移除，注册仅邮箱验证码）。"""
    try:
        return service.register(username, password, nickname), None
    except MemberError as e:
        return None, e


def main():
    print("── 注册（service 层） ──")
    body, e = svc_register("张三", "pass123", "三哥")
    check("注册成功", body is not None, str(e))
    token = (body or {}).get("token", "")
    user = (body or {}).get("user", {})
    check("注册赠送 100 分", user.get("points") == 100, str(user))
    check("昵称生效", user.get("nickname") == "三哥", str(user))
    check("token 非空", bool(token))

    _, e = svc_register("张三", "pass123")
    check("重复用户名被拒", e is not None and e.code == 400, str(e))
    _, e = svc_register("a", "pass123")
    check("过短用户名被拒", e is not None and e.code == 400, str(e))
    _, e = svc_register("李四", "123")
    check("过短密码被拒", e is not None and e.code == 400, str(e))

    print("── 登录 ──")
    r = client.post("/api/auth/login", json={"username": "张三", "password": "wrong!"})
    check("错密码 401", r.status_code == 401)
    r = client.post("/api/auth/login", json={"username": "张三", "password": "pass123"})
    check("登录成功", r.status_code == 200)
    token2 = r.json().get("token", "")
    check("last_login_at 更新", bool(r.json().get("user", {}).get("last_login_at")))

    print("── 会话 ──")
    r = client.get("/api/auth/me")
    check("无 token 401", r.status_code == 401)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    check("Bearer me 200", r.status_code == 200)
    r = client.get("/api/auth/me", headers={"X-Auth-Token": token2})
    check("X-Auth-Token 兼容", r.status_code == 200)
    r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token2}"})
    check("登出 200", r.status_code == 200)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token2}"})
    check("登出后 token 失效", r.status_code == 401)

    print("── 资料编辑 ──")
    r = client.patch("/api/users/me", json={"nickname": "三三来迟", "bio": "播客爱好者"},
                     headers={"Authorization": f"Bearer {token}"})
    check("资料更新成功", r.status_code == 200 and r.json()["user"]["nickname"] == "三三来迟", r.text)
    r = client.patch("/api/users/me", json={"bio": "x" * 201}, headers={"Authorization": f"Bearer {token}"})
    check("简介超长被拒", r.status_code == 400)
    r = client.patch("/api/users/me", json={"nickname": ""}, headers={"Authorization": f"Bearer {token}"})
    check("空昵称被拒", r.status_code == 400)

    print("── 改密码 ──")
    r = client.post("/api/users/me/password", json={"old_password": "bad", "new_password": "newpass1"},
                    headers={"Authorization": f"Bearer {token}"})
    check("旧密码错 400", r.status_code == 400)
    r = client.post("/api/users/me/password", json={"old_password": "pass123", "new_password": "newpass1"},
                    headers={"Authorization": f"Bearer {token}"})
    check("改密成功", r.status_code == 200)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    check("改密后旧会话吊销", r.status_code == 401)
    r = client.post("/api/auth/login", json={"username": "张三", "password": "newpass1"})
    check("新密码可登录", r.status_code == 200)
    token = r.json()["token"]

    print("── 积分流水与发放 ──")
    r = client.get("/api/points/balance", headers={"Authorization": f"Bearer {token}"})
    check("余额 100", r.json().get("points") == 100, r.text)
    r = client.get("/api/points/logs", headers={"Authorization": f"Bearer {token}"})
    check("流水含注册赠送", r.json()["total"] == 1 and r.json()["logs"][0]["kind"] == "earn", r.text)
    r = client.post("/api/admin/members/points/grant",
                    json={"user": "张三", "delta": 500, "reason": "测试发放"}, headers=ADMIN)
    check("管理员发放 500", r.status_code == 200 and r.json()["balance"] == 600, r.text)
    r = client.post("/api/admin/members/points/grant",
                    json={"user": "张三", "delta": -150, "reason": "测试扣减"}, headers=ADMIN)
    check("管理员扣减 -150", r.status_code == 200 and r.json()["balance"] == 450, r.text)
    r = client.post("/api/admin/members/points/grant",
                    json={"user": "不存在", "delta": 1}, headers=ADMIN)
    check("发放未知用户 404", r.status_code == 404)
    r = client.get("/api/points/logs", headers={"Authorization": f"Bearer {token}"})
    kinds = [e["kind"] for e in r.json()["logs"]]
    check("流水顺序（新在前）", kinds == ["grant", "grant", "earn"], str(kinds))

    print("── 优惠码 ──")
    r = client.post("/api/admin/members/codes",
                    json={"count": 3, "points": 50, "max_uses": 2, "note": "测试"}, headers=ADMIN)
    check("生成 3 码", r.status_code == 200 and len(r.json()["codes"]) == 3, r.text)
    code_batch = r.json()["codes"]
    code = code_batch[0]
    r = client.post("/api/admin/members/codes", json={"count": 1, "points": 0}, headers=ADMIN)
    check("面值 0 被拒", r.status_code == 400)

    svc_register("王五", "pass123")  # service 层预置
    w5 = client.post("/api/auth/login", json={"username": "王五", "password": "pass123"}).json()["token"]

    r = client.post("/api/points/redeem", json={"code": code.lower()}, headers={"Authorization": f"Bearer {w5}"})
    check("小写码也能兑", r.status_code == 200 and r.json()["added"] == 50, r.text)
    r = client.post("/api/points/redeem", json={"code": code}, headers={"Authorization": f"Bearer {w5}"})
    check("同用户重复兑被拒", r.status_code == 400)
    r = client.post("/api/points/redeem", json={"code": "NOPE123456"}, headers={"Authorization": f"Bearer {w5}"})
    check("不存在码被拒", r.status_code == 400)
    r = client.post("/api/points/redeem", json={"code": code}, headers={"Authorization": f"Bearer {token}"})
    check("第二人兑换成功", r.status_code == 200)
    r = client.post("/api/points/redeem", json={"code": code}, headers={"Authorization": f"Bearer {token}"})
    check("第三次被拒（max_uses=2 用尽）", r.status_code == 400)
    r = client.get("/api/admin/members/codes", headers=ADMIN)
    entry = next(c for c in r.json()["codes"] if c["code"] == code)
    check("使用计数 2", entry["used_count"] == 2, str(entry))

    print("── 签到 ──")
    r = client.get("/api/points/checkin", headers={"Authorization": f"Bearer {w5}"})
    check("签到状态查询", r.status_code == 200 and r.json()["checked_in_today"] is False)
    r = client.post("/api/points/checkin", headers={"Authorization": f"Bearer {w5}"})
    check("签到 +5", r.status_code == 200 and r.json()["added"] == 5, r.text)
    r = client.post("/api/points/checkin", headers={"Authorization": f"Bearer {w5}"})
    check("重复签到 409", r.status_code == 409)
    r = client.get("/api/points/checkin", headers={"Authorization": f"Bearer {w5}"})
    check("状态已变已签到", r.json()["checked_in_today"] is True and r.json()["total_days"] == 1)

    print("── 管理接口守卫 ──")
    r = client.post("/api/admin/members/codes", json={"count": 1, "points": 10})
    check("无令牌 401", r.status_code == 401)
    r = client.post("/api/admin/members/codes", json={"count": 1, "points": 10},
                    headers={"X-Admin-Token": "wrong"})
    check("错令牌 401", r.status_code == 401)
    r = client.get("/api/admin/members/users", headers=ADMIN)
    check("用户列表 2 人", r.status_code == 200 and len(r.json()["users"]) == 2, r.text)
    r = client.post("/api/admin/members/disable", json={"user": "王五", "disabled": True}, headers=ADMIN)
    check("禁用成功", r.status_code == 200)
    r = client.post("/api/auth/login", json={"username": "王五", "password": "pass123"})
    check("禁用后登录 403", r.status_code == 403)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {w5}"})
    check("禁用后会话失效", r.status_code == 401)
    client.post("/api/admin/members/disable", json={"user": "王五", "disabled": False}, headers=ADMIN)
    r = client.post("/api/auth/login", json={"username": "王五", "password": "pass123"})
    check("重新启用可登录", r.status_code == 200)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
