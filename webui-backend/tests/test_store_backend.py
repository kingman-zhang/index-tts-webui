#!/usr/bin/env python3
"""存储门面 / 连接层的接口契约测试（自跑脚本，项目惯例非 pytest）。

验证「按 `MEMBER_STORE` 选择后端」这套抽象是否成立：
  1. 门面暴露的接口与 `_REQUIRED_API` 一致、且旧私有 API 不再外泄；
  2. 整份读写行为不变（含新下沉的 `trim_point_logs`）；
  3. 非法后端**启动即报错**，不静默回退；
  4. 原子操作语义（条件扣费 / 幂等 / 抢单 / 签到 / 占坑）在两种后端下一致；
  5. `app/db.py` 的 key 前缀契约成立。

用法：
    python tests/test_store_backend.py                    # file 后端
    TEST_MEMBER_STORE=mongo python tests/test_store_backend.py   # mongo（测试库）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 后端由环境变量切换；默认 file（与生产默认一致）
TEST_BACKEND = (os.environ.get("TEST_MEMBER_STORE") or "file").strip().lower()

TMP = tempfile.mkdtemp(prefix="wb-store-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_STORE"] = TEST_BACKEND
if TEST_BACKEND == "mongo":
    # ⚠️ 绝不能用生产库名 —— 本测试会清空集合
    os.environ["MONGO_DB"] = os.environ.get("TEST_MONGO_DB", "podcast_test")
    if not os.environ.get("MONGO_URL"):
        # 本测试默认屏蔽 .env（保证完全离线）；mongo 模式需要连接串，所以在
        # import app.config **之前**把它读回来（config/db 是 import 期读环境变量的）。
        _env_file = Path(__file__).resolve().parents[1] / ".env"
        if _env_file.exists():
            for _line in _env_file.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                if _k.strip() == "MONGO_URL":
                    os.environ.setdefault("MONGO_URL", _v.strip().strip('"').strip("'"))
else:
    # 设成**空串**（而非 pop）：config._load_dotenv 是 setdefault 语义，
    # 空串占位可保证真实 .env / 环境里的连接串不会渗进来 → 完全离线。
    os.environ["MONGO_URL"] = ""
    os.environ["REDIS_URL"] = ""

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app import db  # noqa: E402
from app.membership import store  # noqa: E402

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


def _import_store_with(backend: str, code: str = "pass") -> subprocess.CompletedProcess:
    """在子进程里以指定 MEMBER_STORE 值 import 门面（后端在 import 期决定）。"""
    env = dict(os.environ)
    env["MEMBER_STORE"] = backend
    return subprocess.run(
        [sys.executable, "-c", f"from app.membership import store; {code}"],
        cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True,
    )


def _prepare_mongo() -> None:
    """mongo 模式开跑前：清空测试库 + 建索引（唯一索引正是本测试要验的东西）。"""
    from app.config import MONGO_DB
    if MONGO_DB == "podcast":
        raise SystemExit("拒绝在库 'podcast' 上跑测试（那是生产库）—— 请设置 TEST_MONGO_DB")
    from app.db import get_mongo_db
    from tools.init_indexes import ensure_indexes
    db = get_mongo_db()
    db.command("ping")
    # ⚠️ 没有 sessions —— 会话自 Gate 3 起归 session_store 管，账本后端不碰它
    for name in ("users", "point_logs", "redeem_codes",
                 "checkins", "orders", "email_codes"):
        db[name].drop()
    if ensure_indexes(db, verbose=False):
        raise SystemExit("测试库索引创建失败（见上方输出）")
    print(f"（后端 = mongo，库 = {MONGO_DB}：已清空并建好索引）\n")


def main():
    if TEST_BACKEND == "mongo":
        _prepare_mongo()
    print(f"── 门面契约（后端 = {store.BACKEND}）──")
    check("BACKEND 与环境选择一致", store.BACKEND == TEST_BACKEND, repr(store.BACKEND))
    check("实现模块与后端一致",
          store._impl.__name__.endswith(f"store_{TEST_BACKEND}"), store._impl.__name__)
    missing = [n for n in store._REQUIRED_API if not callable(getattr(store, n, None))]
    check("_REQUIRED_API 全部可调用", not missing, f"缺: {missing}")
    leaked = [n for n in ("_lock", "_read", "_write", "POINT_LOGS_FILE",
                          "USERS_FILE", "ORDERS_FILE") if hasattr(store, n)]
    check("旧私有 API 不再对外暴露", not leaked, f"外泄: {leaked}")

    print(f"── 整份读写行为（后端 = {store.BACKEND}，应与历史一致）──")
    store.save_users({"u_x": {"user_id": "u_x", "points": 7}})
    check("写 → 读 往返", store.load_users().get("u_x", {}).get("points") == 7)

    # 会话（Gate 3）已拆到 session_store —— 账本门面不该再有这两个接口。
    # 正反两面都断言：既确认新家存在，也确认旧家不再外泄。
    from app.membership import session_store  # noqa: PLC0415
    check("会话已移出账本门面（load/save_sessions 不再暴露）",
          not hasattr(store, "load_sessions") and not hasattr(store, "save_sessions"))
    check("会话门面可用（BACKEND 与 SESSION_STORE 一致）",
          session_store.BACKEND == (os.environ.get("SESSION_STORE") or "file"),
          repr(session_store.BACKEND))

    # trim_point_logs：原先 service._trim_logs 直接操作文件私有 API，Gate 1 下沉到 store。
    # 带上**递增的 created_at**：mongo 后端按它倒序裁剪，缺字段顺序就不确定了。
    for i in range(5):
        store.append_point_log({"id": f"pl_{i}", "user_id": "u_t", "delta": 1, "kind": "earn",
                                "created_at": f"2026-10-12T00:00:0{i}"})
    store.append_point_log({"id": "pl_other", "user_id": "u_z", "delta": 1, "kind": "earn",
                            "created_at": "2026-10-12T00:00:09"})
    check("流水头插（新在前）",
          store.load_point_logs()[0]["id"] == "pl_other", store.load_point_logs()[0])

    store.trim_point_logs("u_t", 3)
    mine = [e["id"] for e in store.load_point_logs() if e["user_id"] == "u_t"]
    check("裁剪到 keep=3 且保留最新三条", mine == ["pl_4", "pl_3", "pl_2"], str(mine))
    check("裁剪不影响其他用户",
          [e["id"] for e in store.load_point_logs() if e["user_id"] == "u_z"] == ["pl_other"])

    store.trim_point_logs("u_t", 10)  # 未超上限 → 不动
    mine = [e for e in store.load_point_logs() if e["user_id"] == "u_t"]
    check("未超上限时不动", len(mine) == 3, str(len(mine)))

    print("── 后端选择：能选、选错立刻失败（不静默回退）──")
    # mongo 后端在 Gate 2 已落地。import 期**不连库**，所以即使没有连接串也能导入 ——
    # 这是刻意的：后端选择只决定"用哪套实现"，连不上由首次真正操作时才暴露。
    r = _import_store_with("mongo", "print(store.BACKEND, store._impl.__name__)")
    check("MEMBER_STORE=mongo 可导入并绑定 store_mongo",
          r.returncode == 0 and "mongo app.membership.store_mongo" in r.stdout,
          (r.stdout + r.stderr)[-200:])
    r = _import_store_with("bogus")
    check("非法 MEMBER_STORE 被拦下",
          r.returncode != 0 and "不是受支持的后端" in r.stderr,
          (r.stderr or "")[-200:])
    r = _import_store_with("file")
    check("file 正常导入", r.returncode == 0, (r.stderr or "")[-200:])

    print(f"── 原子操作语义（Gate 2 新增接口；后端 = {store.BACKEND}）──")
    store.save_users({"u_m": {"user_id": "u_m", "username": "m", "points": 10}})

    # 条件扣费：余额不足由**原子判定**给出，且不留痕
    try:
        store.apply_point_delta({"id": "pl_1", "user_id": "u_m", "delta": -30,
                                 "kind": "spend", "reason": "t", "ref": "", "created_at": ""})
        check("余额不足被拒", False, "未抛 InsufficientPoints")
    except store.InsufficientPoints as e:
        check("余额不足被拒（原子判定）", e.balance == 10 and e.need == 30, str(e))
    check("扣费失败不留痕", store.get_user("u_m")["points"] == 10)

    # 幂等：refund/purchase 的 (user_id, ref, kind) 只能有一条流水
    base = {"id": "pl_r1", "user_id": "u_m", "delta": 5, "kind": "refund",
            "reason": "退", "ref": "t1:x", "created_at": ""}
    store.apply_point_delta(base)
    check("首次退款入账", store.get_user("u_m")["points"] == 15)
    try:
        store.apply_point_delta({**base, "id": "pl_r2"})
        check("重复退款被唯一规则挡下", False, "未抛 AlreadyApplied")
    except store.AlreadyApplied:
        check("重复退款被唯一规则挡下", True)
    check("重复退款不改余额", store.get_user("u_m")["points"] == 15)

    # 非幂等 kind（redeem）里同一个 ref 被不同用户用 —— 不能互相干扰
    store.save_users({**store.load_users(),
                      "u_n": {"user_id": "u_n", "username": "n", "points": 0}})
    for uid, lid in (("u_m", "pl_rd1"), ("u_n", "pl_rd2")):
        store.apply_point_delta({"id": lid, "user_id": uid, "delta": 7, "kind": "redeem",
                                 "reason": "兑", "ref": "SAME_CODE", "created_at": ""})
    check("redeem 同 ref 不同用户互不干扰",
          store.get_user("u_m")["points"] == 22 and store.get_user("u_n")["points"] == 7)

    # 签到：同一 (user, date) 只有第一次成功
    check("首次签到返回 True", store.insert_checkin_unique("u_m", "2026-10-12") is True)
    check("重复签到返回 False", store.insert_checkin_unique("u_m", "2026-10-12") is False)
    check("不同日期可再签", store.insert_checkin_unique("u_m", "2026-10-13") is True)

    # 抢单：CAS 只能抢到一次
    store.insert_order({"order_id": "ord_t", "user_id": "u_m", "status": "pending",
                        "amount_fen": 100, "points": 100, "pack_name": "p",
                        "out_trade_no": "ord_t"})
    check("首次抢单成功", store.claim_order("ord_t", "mock", "") is not None)
    check("重复抢单返回 None", store.claim_order("ord_t", "mock", "") is None)
    check("抢单后状态为 paid", store.get_order("ord_t")["status"] == "paid")

    # 优惠码占坑：单次码只能被兑走一次
    store.save_redeem_codes({"C1": {"code": "C1", "points": 9, "max_uses": 1,
                                    "used_count": 0, "used_by": [], "expires_ts": None,
                                    "disabled": False}})
    store.claim_redeem_code("C1", "u_m", "m")
    try:
        store.claim_redeem_code("C1", "u_n", "n")
        check("单次码第二人被拒", False, "未抛 CodeUnavailable")
    except store.CodeUnavailable as e:
        check("单次码第二人被拒", e.reason == store.CodeUnavailable.EXHAUSTED, e.reason)
    check("占坑计入 used_count", store.load_redeem_codes()["C1"]["used_count"] == 1)
    store.release_redeem_code("C1", "u_m")
    check("回滚占坑后 used_count 归零",
          store.load_redeem_codes()["C1"]["used_count"] == 0)

    print("── app/db.py 契约 ──")
    check("redis_key 强制统一前缀",
          db.redis_key("session", "abc") == "podcast:session:abc",
          db.redis_key("session", "abc"))
    probe = db.probe()
    check("probe() 返回结构且不抛错",
          set(probe) == {"mongo", "redis"} and all("ok" in v and "configured" in v
                                                   for v in probe.values()), str(probe))
    # configured 取决于是否拿到连接串：file 模式刻意清空（见文件头）⇒ False；
    # mongo 模式不屏蔽 .env ⇒ MONGO_URL/REDIS_URL 都会加载进来 ⇒ True。
    if TEST_BACKEND == "mongo":
        check("已配置连接串时 configured=True",
              probe["mongo"]["configured"] and probe["redis"]["configured"], str(probe))
    else:
        check("未配置连接串时 configured=False",
              not probe["mongo"]["configured"] and not probe["redis"]["configured"], str(probe))

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
