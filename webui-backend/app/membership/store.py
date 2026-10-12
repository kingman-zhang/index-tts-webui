"""会员模块存储门面：按 `MEMBER_STORE` 选择后端实现。

| `MEMBER_STORE` | 实现 | 状态 |
|---|---|---|
| `file`（**默认**） | `store_file.py`（JSON 文件） | 与历史行为**完全一致** |
| `mongo` | `store_mongo.py`（MongoDB 账本） | 见 STORAGE_MIGRATION_PLAN.md 的 Gate 2 |

**接口契约**（两个后端都必须满足，见 `_REQUIRED_API`）：

- 读函数返回**整份**数据（`dict` / `list`）；
- 写函数接收**整份**数据并覆盖写；
- `trim_point_logs(user_id, keep)` 负责「每用户流水上限」的裁剪
  （上限值本身是业务策略，由 service 层传入）；
- 函数签名与语义一经冻结，service 层不再感知后端差异。

⚠️ **本模块只做转发，不含业务逻辑**；后端差异全部由被选中的实现承担。
⚠️ 刻意**不做**「mongo 连不上就退回 file」这种兜底：两个后端的存储形态不同，
   静默切换会让系统读到另一本（空的）账 —— 宁可启动即失败。
"""

from __future__ import annotations

from ..config import MEMBER_STORE

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
    "load_users", "save_users",
    "load_sessions", "save_sessions",
    "load_point_logs", "append_point_log", "trim_point_logs",
    "load_redeem_codes", "save_redeem_codes",
    "load_checkins", "save_checkins",
    "load_orders", "save_orders",
    "load_email_codes", "save_email_codes",
)

_missing = [name for name in _REQUIRED_API if not hasattr(_impl, name)]
if _missing:
    raise RuntimeError(
        f"存储后端 {_impl.__name__!r} 未实现以下接口：{_missing}；"
        f"完整契约见 app/membership/store.py 的 _REQUIRED_API。"
    )


# ─── 接口实现（显式转发，签名即契约）──────────────────────────
# 刻意写成显式函数而非 `globals().update(...)` 批量绑定：这样 `store.load_users`
# 的签名在源码里可见，IDE / 类型检查能识别，新增后端时也一眼看出要补齐哪些。

# 用户

def load_users() -> dict:
    return _impl.load_users()


def save_users(users: dict) -> None:
    _impl.save_users(users)


# 会话

def load_sessions() -> dict:
    return _impl.load_sessions()


def save_sessions(sessions: dict) -> None:
    _impl.save_sessions(sessions)


# 积分流水

def load_point_logs() -> list:
    return _impl.load_point_logs()


def append_point_log(entry: dict) -> None:
    _impl.append_point_log(entry)


def trim_point_logs(user_id: str, keep: int) -> None:
    _impl.trim_point_logs(user_id, keep)


# 优惠码

def load_redeem_codes() -> dict:
    return _impl.load_redeem_codes()


def save_redeem_codes(codes: dict) -> None:
    _impl.save_redeem_codes(codes)


# 签到

def load_checkins() -> dict:
    return _impl.load_checkins()


def save_checkins(data: dict) -> None:
    _impl.save_checkins(data)


# 积分购买订单

def load_orders() -> dict:
    return _impl.load_orders()


def save_orders(orders: dict) -> None:
    _impl.save_orders(orders)


# 邮箱验证码

def load_email_codes() -> dict:
    return _impl.load_email_codes()


def save_email_codes(data: dict) -> None:
    _impl.save_email_codes(data)


# 供自检 / 日志用：当前生效的后端名（`/api/version` 之类可回报）
BACKEND = MEMBER_STORE
