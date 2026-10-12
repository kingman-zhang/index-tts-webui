#!/usr/bin/env python3
"""账本对账（Gate 4）：校验「用户余额」与「积分流水」是否自洽。

为什么需要它
------------
Mongo 单节点**没有多文档事务**（Gate 0 实测 `setName=None`），所以下列两步操作
在物理上是分开的 —— 进程在中间崩溃就会留下"半截"状态：

  · `store.apply_point_delta`：先 `$inc` 改余额、再 `insert_one` 写流水
      ⇒ 崩在中间 = **余额变了、流水没写**
  · `settle_order`：先 CAS 把订单抢成 `paid`、再入账发积分
      ⇒ 崩在中间 = **订单 paid、却没有 purchase 流水**

这些账**刻意不静默修**（自动改账比留痕更危险），所以需要一个工具把它们**报出来**
交人工处置。本脚本就是那个工具 —— 它是 `tools/init_indexes.py` 之外的
第二个「上线前自检」入口。

判据（按可靠性排序）
--------------------
1. **`balance_after` 判据（权威，裁剪无关）**
   每条流水都记录了"记完这笔之后的余额"。取该用户 `created_at` **最新**的一条，
   其 `balance_after` 必须等于 `users.points`。流水被裁剪也不影响 —— 被裁掉的是
   **旧的**，最新的那条永远在。（`balance_after` 由 `apply_point_delta` 统一写入，
   而它是**所有**积分变动的唯一出口，所以该字段全覆盖，注册礼包也有。）

2. **`Σ delta` 判据（严格，仅在流水未裁剪时适用）**
   `sum(point_logs.delta) == users.points`。⚠️ 流水有**每用户上限**
   （`service.MAX_POINT_LOGS_PER_USER`，默认 500，超限丢最旧的）⇒ 一旦该用户流水
   触到上限，Σ 天然小于余额，**这不是错误**。此时本判据跳过并标注。

3. **`balance_after` 链连续性（定位用）**
   按时间升序，相邻两条的 `balance_after + delta == 后一条 balance_after`。
   断裂点直接指向**漂移开始的那一条**（比"合计不符"更有用）。
   仅在该用户**所有**流水都有 `balance_after` 时检查（缺字段本身即异常）。

其余检查：订单与流水互证、重复的幂等流水（唯一索引本应挡住，此处交叉验证）、
孤儿流水（user_id 已不存在的流水）。

用法
----
    cd webui-backend
    python tools/reconcile.py                     # 人读报告
    python tools/reconcile.py --json              # 机器可读（供监控 / 工单）
    python tools/reconcile.py --user 张三          # 只看一个用户
    python tools/reconcile.py --pending-hours 6    # 挂单告警阈值（默认 24h）
    python tools/reconcile.py --verbose            # 打印每个用户的逐项明细

退出码：0 = 自洽（warn 不算失败）；1 = 发现 error；2 = 读不了数据。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 让 `import app` 生效（tools/ 与 webui-backend/ 同级）
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.membership import store  # noqa: E402
from app.membership.service import MAX_POINT_LOGS_PER_USER  # noqa: E402
from app.membership.store_contract import (  # noqa: E402
    IDEMPOTENT_KINDS,
    ORDER_PAID,
    ORDER_PENDING,
)

ORDER_REF_PREFIX = "order:"   # purchase 流水的 ref 约定：f"order:{order_id}"


# ─── 纯函数：报告构建（不读库、不打印、不退出 ⇒ 可离线单测）──────────

def _text(value) -> str:
    """把任意值收敛成可比较的字符串（None / 缺失 → ""）。"""
    return value.strip() if isinstance(value, str) else ""


def _ts(value) -> str:
    """取 `created_at` 的排序键。"""
    return _text(value)


def _log_key(log: dict) -> tuple[str, str]:
    """流水的时间序键：(created_at, id)。加 id 是为了让同秒的多条**顺序稳定**。"""
    return (_ts(log.get("created_at")), str(log.get("id") or ""))


def _parse_iso(value) -> datetime | None:
    try:
        return datetime.fromisoformat(_text(value))
    except (TypeError, ValueError):
        return None


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_report(
    users: dict,
    logs: list,
    orders: dict,
    *,
    log_cap: int = MAX_POINT_LOGS_PER_USER,
    pending_hours: float = 24.0,
    now: datetime | None = None,
    focus_ids: set[str] | None = None,
) -> dict:
    """把三份数据算成一份对账报告。

    `users`：`{user_id: user}`（`store.load_users()` 的形态）
    `logs` ：`list[point_log]`（`store.load_point_logs()` 的形态）
    `orders`：`{order_id: order}`（`store.load_orders()` 的形态）
    `focus_ids`：非空时只报告这些 user_id 相关的内容（流水 / 订单仍全量扫描）
    """
    now = now or datetime.now()
    issues: list[dict] = []

    def issue(kind: str, severity: str, message: str, /, **detail) -> None:
        # `/` 让前三个参数只能按位置传 —— 否则 detail 里的 `kind`（流水的 kind）
        # 会与这里的参数名撞车（TypeError: multiple values for argument 'kind'）。
        issues.append({"type": kind, "severity": severity, "message": message,
                       "detail": detail})

    # 流水按 user_id 归集（未指定 user_id 的流水归到 "" → 会被判孤儿）
    by_user: dict[str, list[dict]] = {}
    for log in logs:
        by_user.setdefault(str(log.get("user_id") or ""), []).append(log)

    # ── 逐用户：余额 vs 流水 ────────────────────────────────
    user_rows: list[dict] = []
    for uid, user in users.items():
        mine = sorted(by_user.get(uid, []), key=_log_key)
        points = _int(user.get("points"))
        sum_delta = sum(_int(l.get("delta")) for l in mine)
        trimmed = log_cap > 0 and len(mine) >= log_cap
        missing_after = sum(1 for l in mine if l.get("balance_after") is None)

        # 判据 1：最新一条流水的 balance_after
        last_after: int | None = None
        ambiguous = False
        if mine:
            max_ts = _log_key(mine[-1])[0]
            latest = [l for l in mine if _log_key(l)[0] == max_ts]
            have = [l for l in latest if l.get("balance_after") is not None]
            ambiguous = len(latest) > 1
            if ambiguous:
                # 同一时刻多条（并发写入）：判据降级为"其中有一条对上即算自洽"
                hit = next((_int(l["balance_after"]) for l in have
                            if _int(l["balance_after"]) == points), None)
                last_after = hit if hit is not None else (
                    _int(have[-1]["balance_after"]) if have else None)
            elif have:
                last_after = _int(have[-1]["balance_after"])

        # 判据 2：Σ delta（仅在未裁剪且无缺字段时才有意义）
        sum_applicable = (not trimmed) and missing_after == 0

        ok = True
        if not mine:
            ok = points == 0
            if not ok:
                issue("balance_mismatch", "error",
                      f"用户 {uid} 余额 {points} 但**没有任何流水**（余额来路不明）",
                      user_id=uid, points=points, sum_delta=0, last_balance_after=None)
        elif last_after is None:
            ok = False
            issue("balance_unverifiable", "error",
                  f"用户 {uid} 的流水全部缺少 balance_after，无法校验余额",
                  user_id=uid, points=points)
        elif last_after != points:
            ok = False
            issue("balance_mismatch", "error",
                  f"用户 {uid} 余额 {points} ≠ 最新流水 balance_after {last_after}"
                  f"（差 {points - last_after}）—— 疑似「改了余额没写流水」",
                  user_id=uid, points=points, last_balance_after=last_after,
                  diff=points - last_after)

        if sum_applicable and sum_delta != points:
            ok = False
            issue("sum_mismatch", "error",
                  f"用户 {uid} 余额 {points} ≠ 流水合计 {sum_delta}（差 {points - sum_delta}）",
                  user_id=uid, points=points, sum_delta=sum_delta)

        if trimmed:
            issue("log_trimmed", "info",
                  f"用户 {uid} 流水已达上限 {log_cap} 条（Σ delta 判据不适用，"
                  f"以 balance_after 为准）",
                  user_id=uid, count=len(mine), log_cap=log_cap)

        if missing_after:
            issue("missing_balance_after", "warn",
                  f"用户 {uid} 有 {missing_after} 条流水缺 balance_after 字段"
                  f"（链连续性检查已跳过）",
                  user_id=uid, count=missing_after)

        # 判据 3：balance_after 链连续性（缺字段时跳过，避免误报）
        chain_breaks: list[dict] = []
        if missing_after == 0:
            prev_ba: int | None = None
            for log in mine:
                if log.get("balance_after") is None:
                    continue
                ba = _int(log["balance_after"])
                if prev_ba is not None:
                    expect = prev_ba + _int(log.get("delta"))
                    if expect != ba:
                        chain_breaks.append({
                            "log_id": log.get("id"), "created_at": log.get("created_at"),
                            "kind": log.get("kind"), "delta": _int(log.get("delta")),
                            "expect": expect, "actual": ba,
                        })
                prev_ba = ba
        for brk in chain_breaks:
            ok = False
            issue("chain_break", "error",
                  f"用户 {uid} 流水链断裂 @ {brk['log_id']}"
                  f"（应为 {brk['expect']}，实为 {brk['actual']}）",
                  user_id=uid, **brk)

        user_rows.append({
            "user_id": uid,
            "username": user.get("username") or "",
            "points": points,
            "log_count": len(mine),
            "sum_delta": sum_delta,
            "last_balance_after": last_after,
            "trimmed": trimmed,
            "sum_applicable": sum_applicable,
            "missing_balance_after": missing_after,
            "ambiguous_latest": ambiguous,
            "chain_breaks": len(chain_breaks),
            "ok": ok,
        })

    # ── 孤儿流水：user_id 已不存在的流水 ────────────────────
    for uid, mine in sorted(by_user.items()):
        if uid and uid not in users:
            issue("log_orphan_user", "error",
                  f"{len(mine)} 条流水的 user_id={uid!r} 在用户表里不存在（用户被删？）",
                  user_id=uid, count=len(mine))

    # ── 重复的幂等流水（唯一索引本应挡住 ⇒ 出现即索引缺失或后端不支持）──
    seen: dict[tuple[str, str, str], list[dict]] = {}
    for log in logs:
        kind, ref = _text(log.get("kind")), _text(log.get("ref"))
        if not ref or kind not in IDEMPOTENT_KINDS:
            continue
        seen.setdefault((str(log.get("user_id") or ""), ref, kind), []).append(log)
    for (uid, ref, kind), group in sorted(seen.items()):
        if len(group) > 1:
            issue("duplicate_idempotent_log", "error",
                  f"幂等流水重复 {len(group)} 条（user={uid} kind={kind} ref={ref!r}）"
                  f"—— 幂等唯一索引未生效！",
                  user_id=uid, ref=ref, kind=kind,
                  log_ids=[l.get("id") for l in group])

    # ── 订单 × 流水互证 ─────────────────────────────────────
    purchase_by_order: dict[str, list[dict]] = {}
    for log in logs:
        if _text(log.get("kind")) != "purchase":
            continue
        ref = _text(log.get("ref"))
        if ref.startswith(ORDER_REF_PREFIX):
            purchase_by_order.setdefault(ref[len(ORDER_REF_PREFIX):], []).append(log)

    for oid, order in orders.items():
        uid = str(order.get("user_id") or "")
        status = _text(order.get("status")) or ORDER_PENDING
        credited = oid in purchase_by_order

        if uid not in users:
            issue("order_orphan_user", "error",
                  f"订单 {oid} 的 user_id={uid!r} 在用户表里不存在",
                  order_id=oid, user_id=uid)
        if status == ORDER_PAID and not credited:
            issue("order_paid_no_log", "error",
                  f"订单 {oid} 状态为 paid，却**没有** purchase 流水"
                  f"（崩在「抢单」与「入账」之间）",
                  order_id=oid, user_id=uid)
        if status != ORDER_PAID and credited:
            issue("order_log_not_paid", "error",
                  f"订单 {oid} 有 purchase 流水，但状态仍是 {status!r}（状态没补上）",
                  order_id=oid, user_id=uid)
        if status == ORDER_PENDING:
            created = _parse_iso(order.get("created_at"))
            if created is not None:
                age_h = (now - created).total_seconds() / 3600
                if age_h >= pending_hours:
                    issue("order_pending_stale", "warn",
                          f"订单 {oid} 挂单 {age_h:.1f} 小时（≥ {pending_hours:g}h）"
                          f"仍未支付，人工核对",
                          order_id=oid, user_id=uid, age_hours=round(age_h, 1))

    # ── focus 过滤（只影响**呈现**，扫描始终全量）──────────
    if focus_ids is not None:
        user_rows = [r for r in user_rows if r["user_id"] in focus_ids]
        issues = [i for i in issues
                  if i.get("detail", {}).get("user_id") in focus_ids]

    errors = [i for i in issues if i["severity"] == "error"]
    warns = [i for i in issues if i["severity"] == "warn"]
    return {
        "backend": store.BACKEND,
        "generated_at": now.isoformat(timespec="seconds"),
        "counts": {"users": len(users), "logs": len(logs), "orders": len(orders)},
        "log_cap": log_cap,
        "pending_hours": pending_hours,
        "users": user_rows,
        "issues": issues,
        "summary": {"errors": len(errors), "warnings": len(warns)},
        "ok": not errors,
    }


# ─── 呈现 ───────────────────────────────────────────────────

_SEV_MARK = {"error": "✗", "warn": "!", "info": "·"}


def _print_human(report: dict, *, verbose: bool) -> None:
    c = report["counts"]
    print(f"账本对账 · 后端 = {report['backend']} · {report['generated_at']}")
    print(f"数据：用户 {c['users']} · 流水 {c['logs']} · 订单 {c['orders']}"
          f"（流水上限 {report['log_cap']}/用户，挂单阈值 {report['pending_hours']:g}h）")
    print()

    print("── 用户余额（✓ = 与流水自洽）──")
    rows = report["users"]
    shown = rows if verbose else [r for r in rows if not r["ok"]]
    if not shown:
        print(f"  ✓ 全部 {len(rows)} 个用户与流水自洽")
    for r in shown:
        mark = "✓" if r["ok"] else "✗"
        tail = r["sum_delta"] if r["sum_applicable"] else f"~{r['sum_delta']}(已裁剪)"
        name = f"{r['username']} ({r['user_id']})" if r["username"] else r["user_id"]
        print(f"  {mark} {name:<28} 余额={r['points']:<8} Σ={tail:<12} "
              f"最新balance_after={r['last_balance_after']}")
    print()

    errs = [i for i in report["issues"] if i["severity"] == "error"]
    warns = [i for i in report["issues"] if i["severity"] == "warn"]
    infos = [i for i in report["issues"] if i["severity"] == "info"]
    print(f"── 问题（{len(errs)} 个 error，{len(warns)} 个 warn）──")
    if not (errs or warns):
        print("  （无）")
    for i in errs + warns:
        print(f"  {_SEV_MARK[i['severity']]} [{i['type']}] {i['message']}")
    if infos:
        print(f"\n── 提示（{len(infos)} 个）──")
        for i in infos:
            print(f"  {_SEV_MARK['info']} [{i['type']}] {i['message']}")

    print()
    print("结论：✓ 账本自洽" if report["ok"]
          else f"结论：✗ 发现 {len(errs)} 个 error —— 见上（**不要自动改账**，人工核对后再处置）")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="账本对账：校验用户余额与积分流水是否自洽",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--user", default="", help="只看该用户（user_id / 用户名 / 邮箱）")
    parser.add_argument("--pending-hours", type=float, default=24.0,
                        help="挂单告警阈值（小时，默认 24）")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="打印每个用户的逐项明细（默认只打印有问题的）")
    args = parser.parse_args()

    focus_ids: set[str] | None = None
    if args.user.strip():
        hit = store.find_user(args.user.strip())
        if not hit:
            print(f"✗ 找不到用户：{args.user!r}", file=sys.stderr)
            return 2
        focus_ids = {hit["user_id"]}

    try:
        users = store.load_users()
        logs = store.load_point_logs()
        orders = store.load_orders()
    except Exception as e:   # 连不上库 / 文件损坏 → 明确报错，不做"空数据也能过"
        print(f"✗ 读取账本失败（后端 {store.BACKEND}）：{type(e).__name__}: {e}",
              file=sys.stderr)
        return 2

    report = build_report(users, logs, orders,
                          pending_hours=args.pending_hours, focus_ids=focus_ids)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report, verbose=args.verbose)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
