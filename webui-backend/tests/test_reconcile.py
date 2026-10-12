#!/usr/bin/env python3
"""`tools/reconcile.py` 的判据与 CLI 测试（自跑脚本，项目惯例非 pytest）。

覆盖两层：
  1. `build_report()` 纯函数 —— 各判据（balance_after / Σ delta / 链连续性）、
     订单互证、重复幂等流水、孤儿流水、裁剪豁免、focus 过滤；
  2. CLI 冒烟 —— 用 `file` 后端在临时 DATA_DIR 造数据，跑真进程断言退出码。

为什么不用 mongo 跑 CLI：对账的**判据**是后端无关的（都建立在 users/logs/orders
三份数据形态上），mongo 侧的读写正确性已由 `test_store_backend.py` 覆盖。
本测试只保证「判据对 + 接线对」，因此刻意保持离线可跑。

用法：
    python tests/test_reconcile.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-reconcile-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_STORE"] = "file"
os.environ["MEMBER_REG_BONUS"] = "0"   # CLI 冒烟造数据时不要注册礼包干扰

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.membership import store  # noqa: E402
from tools.reconcile import build_report  # noqa: E402

PASS = 0
FAIL = 0

NOW = datetime(2026, 10, 12, 12, 0, 0)
T0 = "2026-10-01T00:00:00"
T1 = "2026-10-02T00:00:00"
T2 = "2026-10-03T00:00:00"
T3 = "2026-10-04T00:00:00"


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


def _user(uid: str, points: int, username: str = "") -> dict:
    return {"user_id": uid, "username": username or uid, "points": points}


def _log(log_id: str, uid: str, delta: int, *, kind: str = "earn", ref: str = "",
         created_at: str = T0, balance_after=None, with_after: bool = True) -> dict:
    entry = {"id": log_id, "user_id": uid, "delta": delta, "kind": kind, "ref": ref,
             "reason": "", "created_at": created_at}
    if with_after:
        entry["balance_after"] = balance_after if balance_after is not None else 0
    return entry


def _types(report: dict) -> dict:
    """issue type → 出现次数。"""
    out: dict = {}
    for i in report["issues"]:
        out[i["type"]] = out.get(i["type"], 0) + 1
    return out


# ─── 1. 纯函数判据 ──────────────────────────────────────────

print("── 判据 1：balance_after（权威，裁剪无关）──")

r = build_report(
    {"u1": _user("u1", 100, "张三")},
    [_log("l1", "u1", 500, created_at=T0, balance_after=500),
     _log("l2", "u1", -400, kind="spend", ref="t1", created_at=T1, balance_after=100)],
    {},
)
check("自洽账本判 ok", r["ok"] is True, str(r["summary"]))
check("自洽账本 issues 为空", len(r["issues"]) == 0, str(_types(r)))
check("Σ delta 记录正确", r["users"][0]["sum_delta"] == 100)
check("最新 balance_after 记录正确", r["users"][0]["last_balance_after"] == 100)

# 「$inc 成功、写流水前崩溃」的形态：余额 100，但流水只到 500
r = build_report(
    {"u1": _user("u1", 100)},
    [_log("l1", "u1", 500, created_at=T0, balance_after=500)],
    {},
)
t = _types(r)
check("余额≠最新 balance_after 被抓出（balance_mismatch）", t.get("balance_mismatch") == 1, str(t))
check("同场景 Σ 也不符（sum_mismatch）", t.get("sum_mismatch") == 1, str(t))
check("该场景判 not ok", r["ok"] is False)

# 用户有余额但完全没有流水
r = build_report({"u1": _user("u1", 200)}, [], {})
check("无流水的余额被判 balance_mismatch",
      _types(r).get("balance_mismatch") == 1, str(_types(r)))
check("余额为 0 且无流水的用户不报错",
      build_report({"u2": _user("u2", 0)}, [], {})["ok"] is True)

print("── 判据 2：Σ delta（严格，裁剪时豁免）──")

# 未裁剪：Σ 必须等于余额（用链断裂构造 Σ==points 之外的场景不便，这里直接验裁剪）
r = build_report(
    {"u1": _user("u1", 1000)},
    [_log("l1", "u1", 500, created_at=T1, balance_after=500),
     _log("l2", "u1", -100, kind="spend", ref="t1", created_at=T2, balance_after=400),
     _log("l3", "u1", 600, kind="earn", created_at=T3, balance_after=1000)],
    {}, log_cap=3,
)
t = _types(r)
check("触到流水上限 ⇒ 标注 log_trimmed", t.get("log_trimmed") == 1, str(t))
check("裁剪时 Σ 判据不适用（sum_applicable=False）",
      r["users"][0]["sum_applicable"] is False)
check("裁剪但 balance_after 自洽 ⇒ 仍判 ok", r["ok"] is True, str(r["summary"]))
check("裁剪时 Σ 与余额不等也**不报** sum_mismatch", "sum_mismatch" not in t, str(t))

print("── 判据 3：balance_after 链连续性（定位漂移点）──")

# Σ 与最新 balance_after 都对，但中间断（模拟手工改过账）
r = build_report(
    {"u1": _user("u1", 100)},
    [_log("l1", "u1", 500, created_at=T0, balance_after=500),
     _log("l2", "u1", -300, kind="spend", ref="t1", created_at=T1, balance_after=300),
     _log("l3", "u1", -100, kind="spend", ref="t2", created_at=T2, balance_after=100)],
    {},
)
t = _types(r)
check("链断裂被抓出（chain_break）", t.get("chain_break") == 2, str(t))
check("链断裂时 Σ==余额（说明链判据有独立价值）",
      r["users"][0]["sum_delta"] == 100 and "sum_mismatch" not in t, str(t))
check("链断裂判 not ok", r["ok"] is False)

# 缺 balance_after ⇒ warn，且**不**误报链断裂
r = build_report(
    {"u1": _user("u1", 100)},
    [_log("l1", "u1", 500, created_at=T0, with_after=False),
     _log("l2", "u1", -400, kind="spend", ref="t1", created_at=T1, balance_after=100)],
    {},
)
t = _types(r)
check("缺 balance_after 报 warn", t.get("missing_balance_after") == 1, str(t))
check("缺字段时跳过链检查（不误报 chain_break）", "chain_break" not in t, str(t))

print("── 订单 × 流水互证 ──")

r = build_report(
    {"u1": _user("u1", 500)},
    [_log("l1", "u1", 500, kind="earn", created_at=T0, balance_after=500)],
    {"o1": {"order_id": "o1", "user_id": "u1", "status": "paid",
            "created_at": T0}},
)
t = _types(r)
check("paid 但无 purchase 流水 ⇒ order_paid_no_log",
      t.get("order_paid_no_log") == 1, str(t))

r = build_report(
    {"u1": _user("u1", 500)},
    [_log("l1", "u1", 500, kind="earn", created_at=T0, balance_after=500),
     _log("l2", "u1", 500, kind="purchase", ref="order:o1", created_at=T1,
          balance_after=1000)],
    {"o1": {"order_id": "o1", "user_id": "u1", "status": "pending",
            "created_at": T0}},
)
t = _types(r)
check("有 purchase 流水但订单非 paid ⇒ order_log_not_paid",
      t.get("order_log_not_paid") == 1, str(t))

r = build_report(
    {"u1": _user("u1", 0)},
    [],
    {"o1": {"order_id": "o1", "user_id": "u1", "status": "pending",
            "created_at": (NOW - timedelta(hours=30)).isoformat()}},
    now=NOW, pending_hours=24,
)
t = _types(r)
check("挂单超阈值 ⇒ order_pending_stale(warn)", t.get("order_pending_stale") == 1, str(t))
check("仅 warn 时仍判 ok", r["ok"] is True, str(r["summary"]))
check("摘要区分 error 与 warn", r["summary"] == {"errors": 0, "warnings": 1},
      str(r["summary"]))

r = build_report(
    {"u1": _user("u1", 0)}, [],
    {"o1": {"order_id": "o1", "user_id": "u9", "status": "pending",
            "created_at": NOW.isoformat()}},
    now=NOW,
)
check("订单 user_id 不存在 ⇒ order_orphan_user",
      _types(r).get("order_orphan_user") == 1, str(_types(r)))

print("── 重复幂等流水 / 孤儿流水 ──")

r = build_report(
    {"u1": _user("u1", 300)},
    [_log("l1", "u1", 300, kind="earn", created_at=T0, balance_after=300),
     _log("l2", "u1", 100, kind="refund", ref="t1:x", created_at=T1, balance_after=300),
     _log("l3", "u1", 100, kind="refund", ref="t1:x", created_at=T2, balance_after=300)],
    {},
)
check("重复的幂等流水 ⇒ duplicate_idempotent_log",
      _types(r).get("duplicate_idempotent_log") == 1, str(_types(r)))

# spend 不在 IDEMPOTENT_KINDS：同一 ref 的 spend 重复**不应**报（retry 复用 task_id）
r = build_report(
    {"u1": _user("u1", 0)},
    [_log("l1", "u1", -10, kind="spend", ref="task1", created_at=T0, balance_after=90),
     _log("l2", "u1", -20, kind="spend", ref="task1", created_at=T1, balance_after=70),
     _log("l3", "u1", -70, kind="spend", ref="task1", created_at=T2, balance_after=0)],
    {},
)
check("同 ref 的 spend 重复**不**判重复（retry 复用 task_id）",
      "duplicate_idempotent_log" not in _types(r), str(_types(r)))

r = build_report(
    {"u1": _user("u1", 0)},
    [_log("l1", "u9", 100, created_at=T0, balance_after=100)],
    {},
)
check("孤儿流水 ⇒ log_orphan_user", _types(r).get("log_orphan_user") == 1, str(_types(r)))

print("── focus 过滤 / 空数据 ──")

r_all = build_report(
    {"u1": _user("u1", 0), "u2": _user("u2", 50)},
    [_log("l1", "u1", 0, with_after=False)],  # u1 缺字段（warn）
    {}, now=NOW,
)
check("未过滤时 u2 的问题也在", any(
    i["detail"].get("user_id") == "u2" for i in r_all["issues"]), str(_types(r_all)))

r_focus = build_report(
    {"u1": _user("u1", 0), "u2": _user("u2", 50)},
    [_log("l1", "u1", 0, with_after=False)],
    {}, now=NOW, focus_ids={"u1"},
)
check("focus 后只剩该用户的行", [x["user_id"] for x in r_focus["users"]] == ["u1"],
      str(r_focus["users"]))
check("focus 后不含其他用户的 issue", all(
    i["detail"].get("user_id") == "u1" for i in r_focus["issues"]), str(r_focus["issues"]))

r = build_report({}, [], {}, now=NOW)
check("空数据判 ok", r["ok"] is True and r["counts"] == {"users": 0, "logs": 0, "orders": 0},
      str(r["counts"]))

# ─── 2. CLI 冒烟（真进程 + file 后端 + 临时 DATA_DIR）────────

print("── CLI 冒烟（file 后端）──")


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATA_DIR"] = TMP
    env["MEMBER_STORE"] = "file"
    env["MEMBER_REG_BONUS"] = "0"
    return subprocess.run(
        [sys.executable, str(BACKEND_ROOT / "tools" / "reconcile.py"), *args],
        cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True,
    )


store.save_users({"u_cli": {"user_id": "u_cli", "username": "cliuser", "points": 0}})
store.apply_point_delta({"id": "pl_cli1", "user_id": "u_cli", "delta": 100,
                         "kind": "earn", "reason": "cli 冒烟", "ref": "",
                         "created_at": "2026-10-12T10:00:00"})

p = _run_cli()
check("自洽账本 CLI 退出码 0", p.returncode == 0, f"rc={p.returncode}\n{p.stdout}{p.stderr}")
check("CLI 人读输出给出「账本自洽」", "账本自洽" in p.stdout, p.stdout[-300:])

p = _run_cli("--json")
try:
    import json as _json
    data = _json.loads(p.stdout)
    check("CLI --json 输出可解析", data["ok"] is True and data["counts"]["users"] == 1,
          str(data.get("counts")))
except Exception as e:   # noqa: BLE001
    check("CLI --json 输出可解析", False, f"{type(e).__name__}: {e}\n{p.stdout[:300]}")

# 制造「余额被改、流水没跟上」⇒ 必须退出码 1
store.save_users({"u_cli": {"user_id": "u_cli", "username": "cliuser", "points": 999}})
p = _run_cli()
check("账实不符时 CLI 退出码 1", p.returncode == 1, f"rc={p.returncode}\n{p.stdout[-300:]}")
check("CLI 报告 balance_mismatch", "balance_mismatch" in p.stdout, p.stdout[-400:])

p = _run_cli("--user", "nobody-here")
check("--user 找不到人 ⇒ 退出码 2", p.returncode == 2, f"rc={p.returncode}")

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
try:
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:  # noqa: BLE001
    pass
sys.exit(1 if FAIL else 0)
