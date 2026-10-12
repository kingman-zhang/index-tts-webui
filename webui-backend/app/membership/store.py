"""会员模块存储门面：按 `MEMBER_STORE` 选择后端实现。

| `MEMBER_STORE` | 实现 | 状态 |
|---|---|---|
| `file`（**默认**） | `store_file.py`（JSON 文件） | 与历史行为**完全一致** |
| `mongo` | `store_mongo.py`（MongoDB 账本） | 见 STORAGE_MIGRATION_PLAN.md 的 Gate 2 |

**接口分两类**（见 `_REQUIRED_API`）：

1. **整份读写**（`load_users` / `save_users` / …）—— file 时代的形态，
   语义是「读整份 → 改 → 盖回去」。**资金路径一律不要用**（并发会丢更新）；
   保留给低频管理页（`admin_list_users`）、列表分页与测试。
2. **细粒度 / 原子操作**（`get_user` / `apply_point_delta` / `claim_order` / …）——
   资金与并发路径**必须**走这些。后端差异（`$inc` 条件更新、唯一索引、CAS）
   全部藏在这一层，service 层因此不再需要任何锁。

⚠️ **本模块只做转发，不含业务逻辑**；后端差异全部由被选中的实现承担。
⚠️ 刻意**不做**「mongo 连不上就退回 file」这种兜底：两个后端的存储形态不同，
   静默切换会让系统读到另一本（空的）账 —— 宁可启动即失败。
"""

from __future__ import annotations

from ..config import MEMBER_STORE

# 错误契约与共享常量：单独放 store_contract，避免"门面 ↔ 后端"循环 import。
# 这里 re-export 一份，让调用方只 import 门面就够了。
from .store_contract import (  # noqa: F401
    IDEMPOTENT_KINDS,
    POINT_LOG_KINDS,
    AlreadyApplied,
    CodeUnavailable,
    DuplicateUser,
    InsufficientPoints,
    StoreError,
    UserNotFound,
)

# ─── 后端选择（import 期一次决定）────────────────────────────

_SUPPORTED = ("file", "mongo")

if MEMBER_STORE == "mongo":
    try:
        from . import store_mongo as _impl
    except ImportError as _e:  # ModuleNotFoundError 是其子类
        raise RuntimeError(
            f"MEMBER_STORE=mongo，但 Mongo 后端尚未就绪：{_e}\n"
            f"当前可用后端：file。请先完成 STORAGE_MIGRATION_PLAN.md 的 Gate 2，"
            f"或把 .env 里的 MEMBER_STORE 改回 file。"
        ) from _e
else:
    # 其余取值统一按 file 处理；非法值在下面拦下（不静默回退）。
    from . import store_file as _impl

if MEMBER_STORE not in _SUPPORTED:
    raise RuntimeError(
        f"MEMBER_STORE={MEMBER_STORE!r} 不是受支持的后端；可选：{'、'.join(_SUPPORTED)}。"
        f"（配置见 .env / app/config.py）"
    )


# 接口契约：新增后端必须提供以下全部函数，否则 **import 期即失败**（fail fast，
# 而不是等某个请求调用到才发现）。顺序与 service 层依赖一致。
_REQUIRED_API = (
    # ── 用户 ──
    "load_users", "save_users",
    "create_user", "get_user", "find_user", "update_user_fields",
    # ── 会话 ──
    "load_sessions", "save_sessions",
    # ── 积分流水 ──
    "load_point_logs", "append_point_log", "trim_point_logs", "apply_point_delta",
    # ── 优惠码 ──
    "load_redeem_codes", "save_redeem_codes",
    "claim_redeem_code", "release_redeem_code",
    # ── 签到 ──
    "load_checkins", "save_checkins", "insert_checkin_unique",
    # ── 积分购买订单 ──
    "load_orders", "save_orders",
    "insert_order", "get_order", "find_order", "claim_order", "mark_order_credited",
    # ── 邮箱验证码 ──
    "load_email_codes", "save_email_codes",
)

_missing = [name for name in _REQUIRED_API if not hasattr(_impl, name)]
if _missing:
    raise RuntimeError(
        f"存储后端 {_impl.__name__!r} 未实现以下接口：{_missing}；"
        f"完整契约见 app/membership/store.py 的 _REQUIRED_API。"
    )


# ─── 用户：整份读写 ─────────────────────────────────────────

def load_users() -> dict:
    return _impl.load_users()


def save_users(users: dict) -> None:
    _impl.save_users(users)


# ─── 用户：细粒度 ───────────────────────────────────────────

def create_user(user: dict) -> None:
    """原子建号。用户名/邮箱已被占用时抛 `DuplicateUser`。"""
    _impl.create_user(user)


def get_user(user_id: str) -> dict | None:
    """按 user_id 取单个用户（不必 load 整张表），不存在返回 None。"""
    return _impl.get_user(user_id)


def find_user(identifier: str) -> dict | None:
    """按 user_id / username / email **任一**匹配取一个用户；不存在返回 None。

    存在的意义：让 `login`（用户名或邮箱）、`register_email`（查邮箱占用）、
    `admin_*`（按用户名或 id）都不必 load 整张用户表。
    """
    return _impl.find_user(identifier)


def update_user_fields(user_id: str, fields: dict) -> dict | None:
    """只更新指定用户的指定字段，返回更新后的用户；用户不存在返回 None。

    `updated_at` 之类的业务字段由调用方放进 `fields`（存储层不掺业务语义）。
    """
    return _impl.update_user_fields(user_id, fields)


# ─── 会话 ───────────────────────────────────────────────────

def load_sessions() -> dict:
    return _impl.load_sessions()


def save_sessions(sessions: dict) -> None:
    _impl.save_sessions(sessions)


# ─── 积分流水 ───────────────────────────────────────────────

def load_point_logs() -> list:
    return _impl.load_point_logs()


def append_point_log(entry: dict) -> None:
    _impl.append_point_log(entry)


def trim_point_logs(user_id: str, keep: int) -> None:
    _impl.trim_point_logs(user_id, keep)


def apply_point_delta(entry: dict, trim_keep: int | None = None) -> int:
    """**原子地**改余额 + 记流水，返回新余额。这是所有积分变动的唯一入口。

    `entry` 需含：`id` / `user_id` / `delta` / `kind` / `reason` / `ref` / `created_at`；
    实现方会补上 `balance_after` 后落库。

    失败语义（两个后端一致）：
      - `UserNotFound`       —— 用户不存在
      - `InsufficientPoints` —— delta < 0 且余额不足（**原子判定**，非"先查后写"）
      - `AlreadyApplied`     —— `kind ∈ IDEMPOTENT_KINDS` 且 (user_id, ref, kind) 已有流水
    """
    return _impl.apply_point_delta(entry, trim_keep)


# ─── 优惠码：整份读写 ───────────────────────────────────────

def load_redeem_codes() -> dict:
    return _impl.load_redeem_codes()


def save_redeem_codes(codes: dict) -> None:
    _impl.save_redeem_codes(codes)


# ─── 优惠码：原子占坑 ───────────────────────────────────────

def claim_redeem_code(code: str, user_id: str, username: str) -> dict:
    """**原子**占坑一张优惠码，返回占坑后的码文档（含 `points`）。

    可用性校验（存在/停用/过期/用完/本人已兑）在原子操作内部完成，
    不可用时抛 `CodeUnavailable(reason)`。
    """
    return _impl.claim_redeem_code(code, user_id, username)


def release_redeem_code(code: str, user_id: str) -> None:
    """回滚一次占坑（发积分失败时用）。幂等：没占过坑也不会报错。"""
    _impl.release_redeem_code(code, user_id)


# ─── 签到 ───────────────────────────────────────────────────

def load_checkins() -> dict:
    return _impl.load_checkins()


def save_checkins(data: dict) -> None:
    _impl.save_checkins(data)


def insert_checkin_unique(user_id: str, date: str) -> bool:
    """**原子**签到：`(user_id, date)` 首次插入返回 True，已签过返回 False。"""
    return _impl.insert_checkin_unique(user_id, date)


# ─── 积分购买订单：整份读写 ─────────────────────────────────

def load_orders() -> dict:
    return _impl.load_orders()


def save_orders(orders: dict) -> None:
    _impl.save_orders(orders)


# ─── 积分购买订单：细粒度 / CAS ─────────────────────────────

def insert_order(order: dict) -> None:
    _impl.insert_order(order)


def get_order(order_id: str) -> dict | None:
    return _impl.get_order(order_id)


def find_order(out_trade_no: str) -> dict | None:
    """按商户订单号（`out_trade_no`）或内部 `order_id` 找单（真实回调入口用）。"""
    return _impl.find_order(out_trade_no)


def claim_order(order_id: str, provider: str, out_trade_no: str) -> dict | None:
    """**CAS 抢单**：`status: pending → paid`。返回抢占后的订单；没抢到返回 None。

    ⚠️ 抢到 ≠ 已入账 —— 调用方拿到返回后**必须**紧接着做积分入账，
    否则会留下「paid 但无流水」的订单（由 Gate 4 的对账工具发现）。
    """
    return _impl.claim_order(order_id, provider, out_trade_no)


def mark_order_credited(order_id: str, log_id: str) -> None:
    """回填入账流水 id（`credit_log_id`）。"""
    _impl.mark_order_credited(order_id, log_id)


# ─── 邮箱验证码 ─────────────────────────────────────────────

def load_email_codes() -> dict:
    return _impl.load_email_codes()


def save_email_codes(data: dict) -> None:
    _impl.save_email_codes(data)


# 供自检 / 日志用：当前生效的后端名（`/api/version` 之类可回报）
BACKEND = MEMBER_STORE
