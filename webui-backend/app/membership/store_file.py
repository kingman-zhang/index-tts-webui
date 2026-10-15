"""会员模块存储后端之一：**JSON 文件**（默认后端，与项目 stores.py 风格一致）。

> 这是 `store.py` 门面背后的一个实现，不要直接 import 它 —— 业务层一律用
> `from . import store`。接口契约见 `store.py` 的 `_REQUIRED_API`。

文件布局（DATA_DIR/members/）：
  users.json         用户表 {user_id: {...}}
  point_logs.json    积分流水（追加列表，新记录在前）
  redeem_codes.json  优惠码 {code: {...}}
  checkins.json      签到记录 {user_id: {"2026-09-19", ...}}
  orders.json        积分购买订单 {order_id: {...}}（充值链路，2026-10-05 新增）

⚠️ **会话（sessions.json）不在这里** —— 2026-10-12（Gate 3）已拆到 `session_store_file.py`：
   会话是天然的键值数据、与账本没有事务关系，且需要能单独切 Redis。文件路径与格式
   都没变，只是换了个模块管（见 session_store.py）。

并发策略：模块级 threading.Lock 串行化读改写；写入采用 tmp+replace 原子替换
（原语在 `json_store.py`，与会话后端共用）。
⚠️ 该锁**只覆盖单进程**（`main.run()` 未传 workers）；多实例部署需换后端，
   见 STORAGE_MIGRATION_PLAN.md。

规模预期（几百~几千用户、几万条流水）下 JSON 文件足够。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import DATA_DIR
from ..json_store import read_json as _read, write_json as _write
from .store_contract import (
    IDEMPOTENT_KINDS,
    ORDER_EXPIRED,
    ORDER_PAID,
    ORDER_PENDING,
    AlreadyApplied,
    CodeUnavailable,
    DuplicateUser,
    InsufficientPoints,
    UserNotFound,
)

MEMBERS_DIR = DATA_DIR / "members"
MEMBERS_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = MEMBERS_DIR / "users.json"
POINT_LOGS_FILE = MEMBERS_DIR / "point_logs.json"
REDEEM_CODES_FILE = MEMBERS_DIR / "redeem_codes.json"
CHECKINS_FILE = MEMBERS_DIR / "checkins.json"

_lock = threading.RLock()


# `_read` / `_write` 两个原语已抽到 json_store.py（会话后端共用同一份实现）。
# 这里保留同名别名，调用点不必改。


# ─── 用户 ───────────────────────────────────────────────────

def load_users() -> dict:
    with _lock:
        return _read(USERS_FILE, {})


def save_users(users: dict) -> None:
    with _lock:
        _write(USERS_FILE, users)


# ─── 用户：细粒度（资金/并发路径用；避免"读整份→改→盖回去"的丢更新）──

def create_user(user: dict) -> None:
    """原子建号。唯一性由模块锁保证 —— ⚠️ 与 mongo 不同，这里**只覆盖单进程**。"""
    with _lock:
        users = _read(USERS_FILE, {})
        username = user.get("username")
        email = (user.get("email") or "").strip().lower()
        if any(u.get("username") == username for u in users.values()):
            raise DuplicateUser("username", username or "")
        if email and any((u.get("email") or "").strip().lower() == email for u in users.values()):
            raise DuplicateUser("email", email)
        users[user["user_id"]] = dict(user)
        _write(USERS_FILE, users)


def get_user(user_id: str) -> Optional[dict]:
    with _lock:
        u = _read(USERS_FILE, {}).get(user_id)
        return dict(u) if u else None


def find_user(identifier: str) -> Optional[dict]:
    ident = (identifier or "").strip()
    if not ident:
        return None
    low = ident.lower()
    with _lock:
        users = _read(USERS_FILE, {})
        hit = users.get(ident)          # 先按 user_id 精确命中
        if hit:
            return dict(hit)
        for u in users.values():        # 再按 username / email
            if u.get("username") == ident or (u.get("email") or "").strip().lower() == low:
                return dict(u)
        return None


def update_user_fields(user_id: str, fields: dict) -> Optional[dict]:
    if not fields:
        return get_user(user_id)
    with _lock:
        users = _read(USERS_FILE, {})
        target = users.get(user_id)
        if not target:
            return None
        target.update(fields)
        users[user_id] = target
        _write(USERS_FILE, users)
        return dict(target)


# ─── 会话 ───────────────────────────────────────────────────
#
# 2026-10-12（Gate 3）：会话已拆到 session_store_file.py / session_store_redis.py，
# 由 session_store.py 门面按 SESSION_STORE 分派。**本模块不再涉及会话。**


# ─── 积分流水 ───────────────────────────────────────────────

def load_point_logs() -> list:
    with _lock:
        return _read(POINT_LOGS_FILE, [])


def append_point_log(entry: dict) -> None:
    """追加一条流水（新记录插到最前，读取时天然按时间倒序）。"""
    with _lock:
        logs = _read(POINT_LOGS_FILE, [])
        logs.insert(0, entry)
        _write(POINT_LOGS_FILE, logs)


def _trim_locked(user_id: str, keep: int) -> None:
    """裁到最多 keep 条（从尾部、即最旧的开始丢）。**调用方须已持有 _lock**。"""
    logs = _read(POINT_LOGS_FILE, [])
    mine = [i for i, e in enumerate(logs) if e.get("user_id") == user_id]
    if len(mine) > keep:
        drop_indexes = set(mine[keep:])
        logs = [e for i, e in enumerate(logs) if i not in drop_indexes]
        _write(POINT_LOGS_FILE, logs)


def trim_point_logs(user_id: str, keep: int) -> None:
    """把某用户的流水裁到最多 keep 条（从尾部、即最旧的开始丢）。

    原先是 service 层 `_trim_logs()` 直接操作文件私有 API（`_lock`/`_read`/`_write`）
    —— 抽象层已经泄漏，2026-10-12 抽接口时下沉到这里，语义不变。
    上限（`MAX_POINT_LOGS_PER_USER`）是**业务策略**，仍由调用方传入。
    """
    with _lock:
        _trim_locked(user_id, keep)


def apply_point_delta(entry: dict, trim_keep: Optional[int] = None) -> int:
    """原子地改余额 + 记流水，返回新余额（file 版：整段在模块锁内完成）。

    ⚠️ 与 mongo 版的差别：这里靠**进程内锁**保证原子 —— 只覆盖单进程。
       mongo 版靠条件 `$inc` + 唯一索引，跨实例也成立。这是 file 后端的
       已知边界（见 STORAGE_MIGRATION_PLAN.md §2 P0-2）。
    """
    uid = entry["user_id"]
    delta = int(entry["delta"])
    kind = entry.get("kind") or ""
    ref = entry.get("ref") or ""
    with _lock:
        logs = _read(POINT_LOGS_FILE, [])
        # 幂等防线：refund / purchase 的 (user_id, ref, kind) 只允许一条流水。
        # mongo 侧由**部分唯一索引**兜底，这里用同一套规则在锁内判断，语义对齐。
        if ref and kind in IDEMPOTENT_KINDS and any(
            e.get("user_id") == uid and e.get("ref") == ref and e.get("kind") == kind
            for e in logs
        ):
            raise AlreadyApplied(ref, kind)

        users = _read(USERS_FILE, {})
        target = users.get(uid)
        if not target:
            raise UserNotFound(uid)
        now_balance = int(target.get("points", 0))
        new_balance = now_balance + delta
        if new_balance < 0:
            raise InsufficientPoints(balance=now_balance, need=-delta)
        target["points"] = new_balance
        target["updated_at"] = datetime.now().isoformat()
        users[uid] = target
        _write(USERS_FILE, users)

        item = dict(entry)
        item["balance_after"] = new_balance
        logs.insert(0, item)
        _write(POINT_LOGS_FILE, logs)
        if trim_keep:
            _trim_locked(uid, trim_keep)
        return new_balance


# ─── 优惠码 ─────────────────────────────────────────────────

def load_redeem_codes() -> dict:
    with _lock:
        return _read(REDEEM_CODES_FILE, {})


def save_redeem_codes(codes: dict) -> None:
    with _lock:
        _write(REDEEM_CODES_FILE, codes)


def claim_redeem_code(code: str, user_id: str, username: str) -> dict:
    """原子占坑一张优惠码。校验顺序与历史行为保持一致（保证同样的错误文案）。"""
    now = time.time()
    with _lock:
        codes = _read(REDEEM_CODES_FILE, {})
        entry = codes.get(code)
        if not entry:
            raise CodeUnavailable(CodeUnavailable.NOT_FOUND, code)
        if entry.get("disabled"):
            raise CodeUnavailable(CodeUnavailable.DISABLED, code)
        if entry.get("expires_ts") and entry["expires_ts"] < now:
            raise CodeUnavailable(CodeUnavailable.EXPIRED, code)
        if int(entry.get("used_count", 0)) >= int(entry.get("max_uses", 1)):
            raise CodeUnavailable(CodeUnavailable.EXHAUSTED, code)
        used_by = entry.get("used_by") or []
        if any(u.get("user_id") == user_id for u in used_by):
            raise CodeUnavailable(CodeUnavailable.ALREADY_USED, code)
        # 先占坑再发积分：并发下同一张码不会被两个人同时抢走
        entry["used_count"] = int(entry.get("used_count", 0)) + 1
        entry["used_by"] = used_by + [{
            "user_id": user_id,
            "username": username,
            "at": datetime.now().isoformat(),
        }]
        codes[code] = entry
        _write(REDEEM_CODES_FILE, codes)
        return dict(entry)


def release_redeem_code(code: str, user_id: str) -> None:
    """回滚一次占坑（发积分失败时用）。幂等：没占过坑也不报错。"""
    with _lock:
        codes = _read(REDEEM_CODES_FILE, {})
        entry = codes.get(code)
        if not entry:
            return
        used_by = entry.get("used_by") or []
        if not any(u.get("user_id") == user_id for u in used_by):
            return
        entry["used_by"] = [u for u in used_by if u.get("user_id") != user_id]
        entry["used_count"] = max(0, int(entry.get("used_count", 0)) - 1)
        codes[code] = entry
        _write(REDEEM_CODES_FILE, codes)


# ─── 签到 ───────────────────────────────────────────────────

def load_checkins() -> dict:
    with _lock:
        return _read(CHECKINS_FILE, {})


def save_checkins(data: dict) -> None:
    with _lock:
        _write(CHECKINS_FILE, data)


def insert_checkin_unique(user_id: str, date: str) -> bool:
    """原子签到：`(user_id, date)` 首次插入返回 True，已签过返回 False。"""
    with _lock:
        data = _read(CHECKINS_FILE, {})
        days = set(data.get(user_id) or [])
        if date in days:
            return False
        days.add(date)
        # 排序后落盘（原来是 set→list 顺序不定）—— 读出来稳定，且不影响任何判断
        data[user_id] = sorted(days)
        _write(CHECKINS_FILE, data)
        return True


# ─── 积分购买订单 ───────────────────────────────────────────

ORDERS_FILE = MEMBERS_DIR / "orders.json"


def load_orders() -> dict:
    with _lock:
        return _read(ORDERS_FILE, {})


def save_orders(orders: dict) -> None:
    with _lock:
        _write(ORDERS_FILE, orders)


def insert_order(order: dict) -> None:
    with _lock:
        orders = _read(ORDERS_FILE, {})
        orders[order["order_id"]] = dict(order)
        _write(ORDERS_FILE, orders)


def get_order(order_id: str) -> Optional[dict]:
    with _lock:
        o = _read(ORDERS_FILE, {}).get(order_id)
        return dict(o) if o else None


def find_order(out_trade_no: str) -> Optional[dict]:
    """按商户订单号或内部 order_id 找单（真实回调只回传 out_trade_no）。"""
    target = (out_trade_no or "").strip()
    if not target:
        return None
    with _lock:
        orders = _read(ORDERS_FILE, {})
        hit = orders.get(target)
        if hit:
            return dict(hit)
        for o in orders.values():
            if o.get("out_trade_no") == target or o.get("order_id") == target:
                return dict(o)
        return None


def claim_order(order_id: str, provider: str, out_trade_no: str) -> Optional[dict]:
    """CAS 抢单：`pending|expired → paid`。抢到返回订单，状态已不是可支付态则返回 None。

    这是"同一订单只入账一次"的**唯一闸门**：并发回调里只有一个能把它翻成 paid
    （file 版靠模块锁，mongo 版靠单文档原子更新）。

    ⚠️ 来源状态**必须**同时包含 `expired`：我们判的"过期"只是展示态，平台侧可能
    仍认这一单 —— 只认 pending 会让"踩着过期点付款"变成钱到了积分不给。
    """
    with _lock:
        orders = _read(ORDERS_FILE, {})
        order = orders.get(order_id)
        if not order or order.get("status") not in (ORDER_PENDING, ORDER_EXPIRED):
            return None
        order["status"] = ORDER_PAID
        order["provider"] = provider
        order["out_trade_no"] = out_trade_no or order.get("out_trade_no") or order_id
        order["paid_at"] = datetime.now().isoformat()
        orders[order_id] = order
        _write(ORDERS_FILE, orders)
        return dict(order)


def expire_order(order_id: str) -> Optional[dict]:
    """CAS 置过期：`pending → expired`（已 paid 的绝不动）。没抢到返回 None。

    返回 None 是正常并发结果（回调刚抢成 paid），**不是错误**。
    """
    with _lock:
        orders = _read(ORDERS_FILE, {})
        order = orders.get(order_id)
        if not order or order.get("status") != ORDER_PENDING:
            return None
        order["status"] = ORDER_EXPIRED
        order["expired_at"] = datetime.now().isoformat()
        orders[order_id] = order
        _write(ORDERS_FILE, orders)
        return dict(order)


def mark_order_credited(order_id: str, log_id: str) -> None:
    with _lock:
        orders = _read(ORDERS_FILE, {})
        order = orders.get(order_id)
        if not order:
            return
        order["credit_log_id"] = log_id
        orders[order_id] = order
        _write(ORDERS_FILE, orders)


def update_order_fields(order_id: str, fields: dict) -> Optional[dict]:
    """只更新指定订单的指定字段，返回更新后的订单；订单不存在返回 None。

    ⚠️ 只做**字段**更新，**不要用它改 `status`** —— 状态流转一律走 `claim_order`
    的 CAS，否则「同一订单只入账一次」的闸门会被绕过。
    """
    with _lock:
        orders = _read(ORDERS_FILE, {})
        order = orders.get(order_id)
        if not order:
            return None
        order.update(fields)
        orders[order_id] = order
        _write(ORDERS_FILE, orders)
        return dict(order)


# ─── 邮箱验证码 ─────────────────────────────────────────────

EMAIL_CODES_FILE = MEMBERS_DIR / "email_codes.json"


def load_email_codes() -> dict:
    with _lock:
        return _read(EMAIL_CODES_FILE, {})


def save_email_codes(data: dict) -> None:
    with _lock:
        _write(EMAIL_CODES_FILE, data)
