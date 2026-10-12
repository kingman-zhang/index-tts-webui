#!/usr/bin/env python3
"""存储门面 / 连接层的接口契约测试（自跑脚本，项目惯例非 pytest）。

Gate 1（2026-10-12）的验收：`membership/store.py` 从「JSON 实现」变成
「按 `MEMBER_STORE` 选择后端的门面」之后 ——
  1. 门面暴露的接口与 `_REQUIRED_API` 一致、且旧私有 API 不再外泄；
  2. `file` 后端行为**零变化**（含新下沉的 `trim_point_logs`）；
  3. 非法 / 未实现的后端**启动即报错**，不静默回退；
  4. `app/db.py` 的 key 前缀契约成立。

用法：/Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_store_backend.py
隔离：临时 DATA_DIR；不连任何数据库（清空 MONGO_URL/REDIS_URL）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-store-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_STORE"] = "file"
# 设成**空串**（而非 pop）：config._load_dotenv 是 setdefault 语义，
# 空串占位可保证真实 .env / 环境里的连接串不会渗进来 → 本测试完全离线。
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


def _import_store_with(backend: str) -> subprocess.CompletedProcess:
    """在子进程里以指定 MEMBER_STORE 值 import 门面（后端在 import 期决定）。"""
    env = dict(os.environ)
    env["MEMBER_STORE"] = backend
    return subprocess.run(
        [sys.executable, "-c", "from app.membership import store"],
        cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True,
    )


def main():
    print("── 门面契约 ──")
    check("默认后端 = file", store.BACKEND == "file", repr(store.BACKEND))
    check("实现模块 = store_file",
          store._impl.__name__.endswith("store_file"), store._impl.__name__)
    missing = [n for n in store._REQUIRED_API if not callable(getattr(store, n, None))]
    check("_REQUIRED_API 全部可调用", not missing, f"缺: {missing}")
    leaked = [n for n in ("_lock", "_read", "_write", "POINT_LOGS_FILE",
                          "USERS_FILE", "ORDERS_FILE") if hasattr(store, n)]
    check("旧私有 API 不再对外暴露", not leaked, f"外泄: {leaked}")

    print("── file 后端行为（与历史一致）──")
    store.save_users({"u_x": {"user_id": "u_x", "points": 7}})
    check("写 → 读 往返", store.load_users().get("u_x", {}).get("points") == 7)

    store.save_sessions({"tok": {"user_id": "u_x"}})
    check("会话写 → 读 往返", store.load_sessions()["tok"]["user_id"] == "u_x")

    # trim_point_logs：原先 service._trim_logs 直接操作文件私有 API，Gate 1 下沉到 store
    for i in range(5):
        store.append_point_log({"id": f"pl_{i}", "user_id": "u_t", "delta": 1, "kind": "earn"})
    store.append_point_log({"id": "pl_other", "user_id": "u_z", "delta": 1, "kind": "earn"})
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

    print("── 后端选择：非法值必须启动即失败（不静默回退）──")
    r = _import_store_with("mongo")
    check("mongo（尚未实现）报明确错误",
          r.returncode != 0 and "Mongo 后端尚未就绪" in r.stderr,
          (r.stderr or "")[-200:])
    r = _import_store_with("bogus")
    check("非法 MEMBER_STORE 被拦下",
          r.returncode != 0 and "不是受支持的后端" in r.stderr,
          (r.stderr or "")[-200:])
    r = _import_store_with("file")
    check("file 正常导入", r.returncode == 0, (r.stderr or "")[-200:])

    print("── app/db.py 契约 ──")
    check("redis_key 强制统一前缀",
          db.redis_key("session", "abc") == "podcast:session:abc",
          db.redis_key("session", "abc"))
    probe = db.probe()
    check("probe() 返回结构且不抛错",
          set(probe) == {"mongo", "redis"} and all("ok" in v and "configured" in v
                                                   for v in probe.values()), str(probe))
    check("未配置连接串时 configured=False",
          not probe["mongo"]["configured"] and not probe["redis"]["configured"], str(probe))

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
