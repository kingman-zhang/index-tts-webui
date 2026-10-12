#!/usr/bin/env python3
"""会员模块单测（自跑脚本，项目惯例非 pytest）。

用法：
    # 文件后端（默认，与生产默认一致）
    python tests/test_membership.py
    # MongoDB 账本 —— 用**独立测试库** podcast_test，不碰生产库
    TEST_MEMBER_STORE=mongo python tests/test_membership.py
    # Redis 会话（账本仍可 file）—— 用独立 key 前缀，不碰生产/别人家的 key
    TEST_SESSION_STORE=redis python tests/test_membership.py
    # 两个一起（上线时的目标形态）
    TEST_MEMBER_STORE=mongo TEST_SESSION_STORE=redis python tests/test_membership.py

**同一套断言跑所有后端组合**，用来证明切换存储后行为等价（核心验收：原先靠三把
进程内锁保证的"并发只成功一次"，现在靠存储层的原子能力保证）。
账本后端（Gate 2）与会话后端（Gate 3）**互相独立**，所以是 2×2 组合。
隔离：临时 DATA_DIR；mongo 用独立库名、redis 用独立 key 前缀，开跑前清空。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

# 两个后端各自独立切换；默认都是 file（与生产默认一致）
TEST_BACKEND = (os.environ.get("TEST_MEMBER_STORE") or "file").strip().lower()
TEST_SESSION = (os.environ.get("TEST_SESSION_STORE") or "file").strip().lower()

TMP = tempfile.mkdtemp(prefix="wb-member-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_STORE"] = TEST_BACKEND
os.environ["SESSION_STORE"] = TEST_SESSION
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"
os.environ["MEMBER_REG_BONUS"] = "100"
os.environ.pop("MEMBER_CHECKIN_BONUS", None)
os.environ["MEMBER_ENFORCE"] = "0"
if TEST_SESSION == "redis":
    # 独立前缀：Redis 与别的项目共用实例，绝不能踩默认的 `podcast`
    os.environ["REDIS_KEY_PREFIX"] = os.environ.get("TEST_REDIS_KEY_PREFIX", "podcast-test-session")
if TEST_BACKEND == "mongo" or TEST_SESSION == "redis":
    if TEST_BACKEND == "mongo":
        # ⚠️ 绝不能用生产库名（podcast）—— 本测试会清空集合
        os.environ["MONGO_DB"] = os.environ.get("TEST_MONGO_DB", "podcast_test")
    # 本测试默认屏蔽 .env（保证"默认值回归"不被真实配置干扰）；需要连库时，
    # 只把**连接相关**的那几项在 import app.config **之前**补回来 ——
    # config / db 都是 import 时读环境变量的，之后再赋值就晚了。
    # （只挑这几项：整份加载 .env 会把 MEMBER_ENFORCE=1 之类的真实配置带进来。）
    _env_file = Path(__file__).resolve().parents[1] / ".env"
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            if _k.strip() in ("MONGO_URL", "REDIS_URL", "REDIS_KEY_PREFIX"):
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
# 默认值回归不得受真实 .env 干扰，也不得读取其中的密钥。
_path_exists = Path.exists
with patch.object(Path, "exists", lambda p: False if p.name == ".env" else _path_exists(p)):
    from app.main import app  # noqa: E402
from app.membership import service, store  # noqa: E402
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


def _prepare_mongo() -> None:
    """mongo 模式开跑前：清空测试库 + 建索引。

    索引必须与生产一致 —— **唯一索引正是本测试要验证的东西**
    （并发签到只成功一次、单次码只被兑走一次，靠的就是它）。
    """
    from app.config import MONGO_DB
    if MONGO_DB == "podcast":
        raise SystemExit("拒绝在库 'podcast' 上跑测试（那是生产库）—— 请设置 TEST_MONGO_DB")
    from app.db import get_mongo_db
    from tools.init_indexes import ensure_indexes
    db = get_mongo_db()
    db.command("ping")
    # ⚠️ 没有 sessions —— 会话自 Gate 3 起归 session_store 管（见下 _prepare_redis）
    for name in ("users", "point_logs", "redeem_codes",
                 "checkins", "orders", "email_codes"):
        db[name].drop()
    if ensure_indexes(db, verbose=False):
        raise SystemExit("测试库索引创建失败（见上方输出）")
    print(f"（账本 = mongo，库 = {MONGO_DB}：已清空并建好索引）")


def _cleanup_redis() -> int:
    """清掉测试前缀下的 key，返回条数（SCAN，不用 KEYS / 不用 FLUSHDB）。"""
    from app.config import REDIS_KEY_PREFIX
    from app.db import get_redis
    r = get_redis()
    n = 0
    for k in list(r.scan_iter(match=f"{REDIS_KEY_PREFIX}:*", count=200)):
        r.delete(k)
        n += 1
    return n


def _prepare_redis() -> None:
    """redis 会话模式开跑前：确认连通 + 清空测试前缀。"""
    from app.config import REDIS_KEY_PREFIX, REDIS_URL
    if not REDIS_URL:
        raise SystemExit("REDIS_URL 未配置 —— TEST_SESSION_STORE=redis 需要连接串")
    if REDIS_KEY_PREFIX in ("", "podcast"):
        raise SystemExit(
            f"拒绝用前缀 {REDIS_KEY_PREFIX!r} 跑测试（那是生产前缀）—— 请设置 TEST_REDIS_KEY_PREFIX")
    from app.db import get_redis
    get_redis().ping()
    n = _cleanup_redis()
    print(f"（会话 = redis，前缀 = {REDIS_KEY_PREFIX}：已清空 {n} 个测试 key）")


def main():
    if TEST_BACKEND == "mongo":
        _prepare_mongo()
    if TEST_SESSION == "redis":
        _prepare_redis()
    if TEST_BACKEND != "file" or TEST_SESSION != "file":
        print(f"（组合：账本 = {TEST_BACKEND}，会话 = {TEST_SESSION}）\n")
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
    check("签到状态显示 20", r.json()["bonus"] == 20)
    before_checkin = client.get("/api/auth/me", headers={"Authorization": f"Bearer {w5}"}).json()["user"]["points"]
    r = client.post("/api/points/checkin", headers={"Authorization": f"Bearer {w5}"})
    check("签到默认 +20", r.status_code == 200 and r.json()["added"] == 20, r.text)
    r = client.post("/api/points/checkin", headers={"Authorization": f"Bearer {w5}"})
    check("重复签到 409", r.status_code == 409)
    after_checkin = client.get("/api/auth/me", headers={"Authorization": f"Bearer {w5}"}).json()["user"]["points"]
    check("重复签到不重复发放积分", after_checkin == before_checkin + 20)
    r = client.get("/api/points/checkin", headers={"Authorization": f"Bearer {w5}"})
    check("状态已变已签到", r.json()["checked_in_today"] is True and r.json()["total_days"] == 1)

    print("── 并发原子性（2026-09-29 排期 P1） ──")
    # 并发签到：8 个线程用 Barrier 对齐后同时进 checkin，只能有 1 个成功。
    # 修前是「读签到表 → 判断 → 写回」非原子，多个线程会各发一次积分。
    body_c, e_c = svc_register("赵六", "pass123")
    check("并发测试用户注册", body_c is not None, str(e_c))
    u6 = (body_c or {}).get("user", {})
    bonus = service.CHECKIN_BONUS
    threads_n = 8
    ok, err = [], []
    barrier = threading.Barrier(threads_n)

    def _race_checkin():
        barrier.wait()
        try:
            ok.append(service.checkin(u6))
        except MemberError as exc:
            err.append(exc)

    workers = [threading.Thread(target=_race_checkin) for _ in range(threads_n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    balance6 = store.load_users()[u6["user_id"]]["points"]
    check(f"并发签到只成功 1 次（成功 {len(ok)} / 409 {len(err)}）", len(ok) == 1, f"成功 {len(ok)} 次")
    check("并发签到只发一次积分", balance6 == 100 + bonus, f"余额 {balance6}")
    check("签到流水只有一条", len([e for e in store.load_point_logs()
                                   if e["user_id"] == u6["user_id"] and e["kind"] == "checkin"]) == 1)

    # 并发兑换：单次码被 4 个不同用户同时抢，只能有 1 个占坑成功
    code_one = service.create_redeem_codes(1, 30, max_uses=1)[0]
    redeem_ok, redeem_err = [], []
    redeem_barrier = threading.Barrier(4)
    redeem_users = []
    for name in ("甲一", "乙二", "丙三", "丁四"):
        b, _ = svc_register(name, "pass123")
        redeem_users.append(b["user"])

    def _race_redeem(u):
        redeem_barrier.wait()
        try:
            redeem_ok.append(service.redeem(u, code_one))
        except MemberError as exc:
            redeem_err.append(exc)

    workers = [threading.Thread(target=_race_redeem, args=(u,)) for u in redeem_users]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    check("单次码并发只被兑走 1 次", len(redeem_ok) == 1, f"成功 {len(redeem_ok)} 次")
    check("单次码 used_count == 1",
          store.load_redeem_codes()[code_one]["used_count"] == 1)
    check("只有一位用户拿到 30 分",
          len([1 for u in redeem_users if store.load_users()[u["user_id"]]["points"] == 130]) == 1)

    print("── 管理接口守卫 ──")
    r = client.post("/api/admin/members/codes", json={"count": 1, "points": 10})
    check("无令牌 401", r.status_code == 401)
    r = client.post("/api/admin/members/codes", json={"count": 1, "points": 10},
                    headers={"X-Admin-Token": "wrong"})
    check("错令牌 401", r.status_code == 401)
    r = client.get("/api/admin/members/users", headers=ADMIN)
    check("用户列表 7 人（张三/王五/赵六/甲乙丙丁）",
          r.status_code == 200 and len(r.json()["users"]) == 7, r.text)
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
        if TEST_SESSION == "redis":
            # Redis 是与别的项目共用的实例，跑完把测试 key 清干净。
            # 清理失败不改变退出码（测试结论已经出来了）。
            try:
                _cleanup_redis()
            except Exception as e:  # noqa: BLE001
                print(f"（收尾清理 redis 测试 key 失败，可忽略：{e}）")
