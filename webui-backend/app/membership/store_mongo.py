"""会员模块存储后端之二：**MongoDB**（生产后端）。

> 这是 `store.py` 门面背后的一个实现，不要直接 import 它 —— 业务层一律用
> `from . import store`。接口契约见 `store.py` 的 `_REQUIRED_API`。

集合与主键（`_id` 直接用**业务主键**：天然唯一、天然有主键索引，少建一个索引）
------------------------------------------------------------------------
| 集合 | `_id` | 说明 |
|---|---|---|
| `users` | `user_id` | 文档内另有 `user_id` 字段，读出来时只去掉 `_id` |
| `orders` | `order_id` | |
| `point_logs` | `id`（`pl_xxx`） | |
| `redeem_codes` | `code` | |
| `checkins` | `f"{user_id}:{date}"` | **幂等键就是主键** —— 重复签到撞主键 |

⚠️ **会话不在这里**：2026-10-12（Gate 3）已拆到 `session_store.py`（file / Redis 两选）。
   本后端因此**不再**涉及 `sessions` 集合；若历史库上残留该集合，可直接 drop。

索引（`tools/init_indexes.py` 创建，幂等可重复跑）
------------------------------------------------
```
users.username                      unique
users.email                         unique + partial（只索引非空字符串）
point_logs.(user_id, ref, kind)      unique + partial（kind ∈ refund/purchase）← 幂等第一防线
point_logs.(user_id, created_at)     普通
point_logs.created_at                普通（对账/列表倒序）
orders.out_trade_no                  普通（回调按商户订单号找单）
```

⚠️ **已知边界：单节点 Mongo 没有多文档事务**（Gate 0 实测 `setName=None`），所以
「改余额 + 记流水」「抢单 + 入账」在物理上是**两步**。崩溃可能留下：
  - 余额已变但流水缺失（`sum(point_logs.delta) != users.points`）
  - 订单 `paid` 但无 `purchase` 流水
这两类**刻意不静默修**（静默修账是更危险的事），由 `tools/reconcile.py` 对账报出来
人工处置。详见 STORAGE_MIGRATION_PLAN.md §3.2 与 Gate 4。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db import get_mongo_db
from .store_contract import (
    ORDER_EXPIRED,
    ORDER_PAID,
    ORDER_PENDING,
    AlreadyApplied,
    CodeUnavailable,
    DuplicateUser,
    InsufficientPoints,
    UserNotFound,
)


def _now() -> str:
    return datetime.now().isoformat()


def _clean(doc) -> Optional[dict]:
    """去掉 Mongo 的 `_id`，还原成与 file 后端一致的文档形态。

    因为 `_id` 用的就是业务主键，文档里本来就有同名字段（`user_id` /
    `order_id` / `code` / `id`），所以去掉 `_id` 不丢信息。
    """
    if not doc:
        return None
    d = dict(doc)
    d.pop("_id", None)
    return d


def _users():
    return get_mongo_db()["users"]


def _orders():
    return get_mongo_db()["orders"]


def _point_logs():
    return get_mongo_db()["point_logs"]


def _redeem_codes():
    return get_mongo_db()["redeem_codes"]


def _checkins():
    return get_mongo_db()["checkins"]


def _email_codes():
    return get_mongo_db()["email_codes"]


# ─── 用户：整份读写 ─────────────────────────────────────────

def load_users() -> dict:
    return {d["user_id"]: _clean(d) for d in _users().find({})}


def save_users(users: dict) -> None:
    """整份覆盖。⚠️ **资金/并发路径不要用**（并发会丢更新）。

    保留是因为它是冻结契约的一部分（测试与迁移脚本在用）。实现刻意避开
    `delete_many({}) + insert_many`：那样在并发下会短暂**清空整张表**，
    改用「逐文档 upsert + 删掉不在列表里的」。
    """
    coll = _users()
    ids = [u["user_id"] for u in users.values()]
    if ids:
        coll.delete_many({"_id": {"$nin": ids}})
    else:
        coll.delete_many({})
    for u in users.values():
        doc = dict(u)
        coll.replace_one({"_id": doc["user_id"]}, doc, upsert=True)


# ─── 用户：细粒度 ───────────────────────────────────────────

def _duplicate_field(err: DuplicateKeyError) -> str:
    """从 DuplicateKeyError 解析出冲突字段，好给出准确的错误（而不是笼统的"重复"）。"""
    details = getattr(err, "details", None) or {}
    key_pattern = details.get("keyPattern") or {}
    for name in ("username", "email"):
        if name in key_pattern:
            return name
    return "user_id"


def create_user(user: dict) -> None:
    """原子建号：靠 `users.username` / `users.email` 的**唯一索引**，
    并发同名/同邮箱只能成功一个（file 后端靠进程内锁，只覆盖单进程）。

    ⚠️ 必须显式把业务主键写进 `_id` —— 否则 Mongo 会自己生成一个 ObjectId，
    后面所有 `find_one({"_id": user_id})` 就全都找不到了。
    """
    doc = dict(user)
    doc["_id"] = doc["user_id"]
    try:
        _users().insert_one(doc)
    except DuplicateKeyError as e:
        field = _duplicate_field(e)
        raise DuplicateUser(field, str(user.get(field) or "")) from e


def get_user(user_id: str) -> Optional[dict]:
    return _clean(_users().find_one({"_id": user_id}))


def find_user(identifier: str) -> Optional[dict]:
    ident = (identifier or "").strip()
    if not ident:
        return None
    coll = _users()
    doc = coll.find_one({"_id": ident})          # 先按 user_id 精确命中
    if doc is None:
        doc = coll.find_one({"$or": [{"username": ident}, {"email": ident.lower()}]})
    return _clean(doc)


def update_user_fields(user_id: str, fields: dict) -> Optional[dict]:
    if not fields:
        return get_user(user_id)
    return _clean(_users().find_one_and_update(
        {"_id": user_id}, {"$set": dict(fields)}, return_document=ReturnDocument.AFTER))


# ─── 会话 ───────────────────────────────────────────────────
#
# 2026-10-12（Gate 3）：会话已整体移至 session_store（file / Redis 两选）。
# **本后端不再涉及会话** —— 会话不进 Mongo。


# ─── 积分流水 ───────────────────────────────────────────────

def load_point_logs() -> list:
    """按 `created_at` 倒序（与 file 的"新记录在前"一致）。"""
    return [_clean(d) for d in _point_logs().find({}).sort("created_at", -1)]


def append_point_log(entry: dict) -> None:
    doc = dict(entry)
    if doc.get("id"):
        doc["_id"] = doc["id"]
    _point_logs().insert_one(doc)


def trim_point_logs(user_id: str, keep: int) -> None:
    """把该用户第 keep+1 条起的流水删掉（按 `created_at` 倒序，丢最旧的）。"""
    if keep <= 0:
        return
    coll = _point_logs()
    stale = list(coll.find({"user_id": user_id}, {"_id": 1}).sort("created_at", -1).skip(keep))
    if stale:
        coll.delete_many({"_id": {"$in": [d["_id"] for d in stale]}})


def apply_point_delta(entry: dict, trim_keep: Optional[int] = None) -> int:
    """原子地改余额 + 记流水，返回新余额。**所有积分变动的唯一入口。**

    原子性来自两处（都不依赖多文档事务）：

    1. **条件 `$inc`**：`delta < 0` 时把 `points >= -delta` 写进**查询条件**，
       匹配不到就说明余额不足 —— 判断与扣减是同一次往返，没有 TOCTOU 窗口。
    2. **部分唯一索引**：`(user_id, ref, kind)` 且 `kind ∈ {refund, purchase}`。
       重复触发（回调重推、退款重试）时 `insert_one` 撞唯一索引，此时把刚才
       那次 `$inc` **精确回滚**（`$inc: -delta` 是相对操作，不受并发影响）。

    ⚠️ 残余窗口（已知、明确接受）：`$inc` 成功但 `insert_one` 之前进程被杀
       ⇒ 余额变了、流水没写。**不静默修** —— 由对账报出"流水合计 ≠ 余额"。
    """
    uid = entry["user_id"]
    delta = int(entry["delta"])
    kind = entry.get("kind") or ""
    ref = entry.get("ref") or ""
    users = _users()

    cond: dict = {"_id": uid}
    if delta < 0:
        cond["points"] = {"$gte": -delta}   # 余额不足 → 匹配不到 → 原子判定
    doc = users.find_one_and_update(
        cond,
        {"$inc": {"points": delta}, "$set": {"updated_at": _now()}},
        return_document=ReturnDocument.AFTER,
        projection={"points": 1},
    )
    if doc is None:
        cur = users.find_one({"_id": uid}, {"points": 1})
        if cur is None:
            raise UserNotFound(uid)
        raise InsufficientPoints(balance=int(cur.get("points", 0) or 0), need=-delta)

    new_balance = int(doc["points"])
    item = dict(entry)
    item["balance_after"] = new_balance
    if item.get("id"):
        item["_id"] = item["id"]
    try:
        _point_logs().insert_one(item)
    except DuplicateKeyError as e:
        users.update_one({"_id": uid}, {"$inc": {"points": -delta}})   # 精确回滚
        raise AlreadyApplied(ref, kind) from e

    if trim_keep:
        trim_point_logs(uid, trim_keep)
    return new_balance


# ─── 优惠码：整份读写 ───────────────────────────────────────

def load_redeem_codes() -> dict:
    return {d["code"]: _clean(d) for d in _redeem_codes().find({})}


def save_redeem_codes(codes: dict) -> None:
    coll = _redeem_codes()
    keys = list(codes)
    if keys:
        coll.delete_many({"_id": {"$nin": keys}})
    else:
        coll.delete_many({})
    for code, c in codes.items():
        doc = dict(c)
        doc["code"] = code
        coll.replace_one({"_id": code}, doc, upsert=True)


# ─── 优惠码：原子占坑 ───────────────────────────────────────

def claim_redeem_code(code: str, user_id: str, username: str) -> dict:
    """**一次原子更新**同时校验五项并占坑 —— 并发抢同一张码只可能有一个成功。

    校验塞进查询条件：`used_count < max_uses` 用 `$expr`（要比较**两个字段**，
    普通查询条件做不到）。失败时再查一次给出**具体原因**（失败是低频路径，
    多一次往返换准确的错误文案是划算的）。
    """
    now = time.time()
    coll = _redeem_codes()
    doc = coll.find_one_and_update(
        {
            "_id": code,
            "disabled": {"$ne": True},
            "used_by.user_id": {"$ne": user_id},              # 本人没兑过
            "$expr": {"$lt": ["$used_count", "$max_uses"]},   # 还有余量
            "$or": [{"expires_ts": None}, {"expires_ts": {"$gt": now}}],
        },
        {
            "$inc": {"used_count": 1},
            "$push": {"used_by": {"user_id": user_id, "username": username, "at": _now()}},
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc:
        return _clean(doc)

    cur = _clean(coll.find_one({"_id": code}))
    if not cur:
        raise CodeUnavailable(CodeUnavailable.NOT_FOUND, code)
    if cur.get("disabled"):
        raise CodeUnavailable(CodeUnavailable.DISABLED, code)
    if cur.get("expires_ts") and cur["expires_ts"] < now:
        raise CodeUnavailable(CodeUnavailable.EXPIRED, code)
    if int(cur.get("used_count", 0)) >= int(cur.get("max_uses", 1)):
        raise CodeUnavailable(CodeUnavailable.EXHAUSTED, code)
    raise CodeUnavailable(CodeUnavailable.ALREADY_USED, code)


def release_redeem_code(code: str, user_id: str) -> None:
    """回滚一次占坑（发积分失败时用）。`$pull` + 计数减一，幂等。"""
    _redeem_codes().update_one(
        {"_id": code, "used_by.user_id": user_id},
        {"$pull": {"used_by": {"user_id": user_id}}, "$inc": {"used_count": -1}},
    )


# ─── 签到 ───────────────────────────────────────────────────

def load_checkins() -> dict:
    """聚合成 file 后端的形态 `{user_id: [date, ...]}`（service 层按这个读）。"""
    out: dict = {}
    for d in _checkins().find({}, {"user_id": 1, "date": 1}):
        out.setdefault(d["user_id"], []).append(d["date"])
    for days in out.values():
        days.sort()
    return out


def save_checkins(data: dict) -> None:
    """整份覆盖（契约保留）。⚠️ 并发路径请用 `insert_checkin_unique`。"""
    coll = _checkins()
    want = [f"{uid}:{day}" for uid, days in data.items() for day in (days or [])]
    if want:
        coll.delete_many({"_id": {"$nin": want}})
    else:
        coll.delete_many({})
    for uid, days in data.items():
        for day in days or []:
            coll.replace_one({"_id": f"{uid}:{day}"},
                             {"user_id": uid, "date": day}, upsert=True)


def insert_checkin_unique(user_id: str, date: str) -> bool:
    """`_id = "{user_id}:{date}"` 就是幂等键 —— 重复签到撞主键，天然原子。

    这比"先查今天签没签、再写"硬：并发两个请求里只有一个能插进去。
    """
    try:
        _checkins().insert_one({"_id": f"{user_id}:{date}", "user_id": user_id,
                                "date": date, "created_at": _now()})
        return True
    except DuplicateKeyError:
        return False


# ─── 积分购买订单：整份读写 ─────────────────────────────────

def load_orders() -> dict:
    return {d["order_id"]: _clean(d) for d in _orders().find({})}


def save_orders(orders: dict) -> None:
    coll = _orders()
    ids = [o["order_id"] for o in orders.values()]
    if ids:
        coll.delete_many({"_id": {"$nin": ids}})
    else:
        coll.delete_many({})
    for o in orders.values():
        doc = dict(o)
        coll.replace_one({"_id": doc["order_id"]}, doc, upsert=True)


# ─── 积分购买订单：细粒度 / CAS ─────────────────────────────

def insert_order(order: dict) -> None:
    doc = dict(order)
    doc["_id"] = doc["order_id"]   # 同上：业务主键即 _id，否则按 _id 查不到
    _orders().insert_one(doc)


def get_order(order_id: str) -> Optional[dict]:
    return _clean(_orders().find_one({"_id": order_id}))


def find_order(out_trade_no: str) -> Optional[dict]:
    target = (out_trade_no or "").strip()
    if not target:
        return None
    coll = _orders()
    doc = coll.find_one({"_id": target})
    if doc is None:
        doc = coll.find_one({"out_trade_no": target})
    return _clean(doc)


def claim_order(order_id: str, provider: str, out_trade_no: str) -> Optional[dict]:
    """CAS 抢单：`status: pending|expired → paid`。**一次单文档原子更新**。

    这是"同一订单只入账一次"的唯一闸门：并发回调（平台重推、用户连点）里
    只有一个能把状态翻成 `paid`，其余拿到 None 直接返回 already_paid。

    ⚠️ 来源状态**必须**同时包含 `expired`：我们判的"过期"只是展示态，平台侧可能
    仍认这一单 —— 只认 pending 会让"踩着过期点付款"变成钱到了积分不给。
    """
    doc = _orders().find_one_and_update(
        {"_id": order_id, "status": {"$in": [ORDER_PENDING, ORDER_EXPIRED]}},
        {"$set": {
            "status": ORDER_PAID,
            "provider": provider,
            "out_trade_no": out_trade_no or order_id,
            "paid_at": _now(),
        }},
        return_document=ReturnDocument.AFTER,
    )
    return _clean(doc)


def expire_order(order_id: str) -> Optional[dict]:
    """CAS 置过期：`status: pending → expired`（已 paid 的绝不动）。

    没抢到返回 None —— 那是正常并发结果（回调刚抢成 paid），**不是错误**。
    """
    doc = _orders().find_one_and_update(
        {"_id": order_id, "status": ORDER_PENDING},
        {"$set": {"status": ORDER_EXPIRED, "expired_at": _now()}},
        return_document=ReturnDocument.AFTER,
    )
    return _clean(doc)


def mark_order_credited(order_id: str, log_id: str) -> None:
    _orders().update_one({"_id": order_id}, {"$set": {"credit_log_id": log_id}})


def update_order_fields(order_id: str, fields: dict) -> Optional[dict]:
    """只更新指定订单的指定字段（⚠️ **不要用它改 `status`**：状态流转走 `claim_order`
    的 CAS，否则「同一订单只入账一次」的闸门会被绕过）。"""
    doc = _orders().find_one_and_update(
        {"_id": order_id}, {"$set": fields}, return_document=ReturnDocument.AFTER,
    )
    return _clean(doc)


# ─── 邮箱验证码 ─────────────────────────────────────────────

def load_email_codes() -> dict:
    return {d["email"]: _clean(d) for d in _email_codes().find({})}


def save_email_codes(data: dict) -> None:
    coll = _email_codes()
    emails = list(data)
    if emails:
        coll.delete_many({"_id": {"$nin": emails}})
    else:
        coll.delete_many({})
    for email, v in data.items():
        doc = dict(v)
        doc["email"] = email
        coll.replace_one({"_id": email}, doc, upsert=True)
