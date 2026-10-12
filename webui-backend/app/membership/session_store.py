"""会话存储门面：按 `SESSION_STORE` 选择后端实现。

| `SESSION_STORE` | 实现 | 存储形态 |
|---|---|---|
| `file`（**默认**） | `session_store_file.py` | `data/members/sessions.json`，与历史**逐字节一致** |
| `redis` | `session_store_redis.py` | Redis，key 带 `REDIS_KEY_PREFIX`，TTL 自动过期 |

为什么与账本后端（`MEMBER_STORE`）**分开**
------------------------------------------
「会话进 Redis」和「账本进 Mongo」是两件独立的事，谁也依赖不到谁。分开配置才有
**单独切换、单独回滚**的余地 —— 一次只动一处，出问题才定位得清（Gate 3 的全部理由）。

为什么接口是**细粒度 KV**而不是「读整份 → 改 → 盖回去」
------------------------------------------------------
会话本就是天然的键值数据（token → 会话），且 Redis 后端只有 O(1) 单键操作才谈得上
收益：`load_sessions()` 在 Redis 上要么退化成 KEYS（阻塞实例）、要么退化成整库拉取，
把收益全吃掉。所以这里**只提供**细粒度接口，从接口上杜绝整表读写。

⚠️ 同样**不做**「redis 连不上就退回 file」的兜底：两个后端存储形态不同，静默切换会让
   全体用户读到另一套（空的）会话状态，表现为「集体掉线」却查不出原因。宁可明确报错。
"""

from __future__ import annotations

from ..config import SESSION_STORE

# 存储层异常：与账本共用 db.DBUnavailable（"基础设施不可用"是同一种语义）。
from ..db import DBUnavailable  # noqa: F401  （re-export，调用方只 import 本门面即可）

# ─── 后端选择（import 期一次决定）────────────────────────────

_SUPPORTED = ("file", "redis")

if SESSION_STORE == "redis":
    try:
        from . import session_store_redis as _impl
    except ImportError as _e:  # ModuleNotFoundError 是其子类
        raise RuntimeError(
            f"SESSION_STORE=redis，但 Redis 后端尚未就绪：{_e}\n"
            f"多半是没装 redis 包（pip install redis），或 SESSION_STORE 没写对。"
            f"改回 SESSION_STORE=file 即可用文件会话。"
        ) from _e
else:
    # 其余取值统一按 file 处理；非法值在下面拦下（不静默回退）。
    from . import session_store_file as _impl

if SESSION_STORE not in _SUPPORTED:
    raise RuntimeError(
        f"SESSION_STORE={SESSION_STORE!r} 不是受支持的后端；可选：{'、'.join(_SUPPORTED)}。"
        f"（配置见 .env / app/config.py）"
    )


# 接口契约：新增后端必须提供以下全部函数，否则 **import 期即失败**（fail fast，
# 而不是等某个请求调用到才发现）。
_REQUIRED_API = (
    "put_session", "get_session", "delete_session", "delete_user_sessions", "count_sessions",
)

_missing = [name for name in _REQUIRED_API if not hasattr(_impl, name)]
if _missing:
    raise RuntimeError(
        f"会话存储后端 {_impl.__name__!r} 未实现以下接口：{_missing}；"
        f"完整契约见 app/membership/session_store.py 的 _REQUIRED_API。"
    )


# ─── 契约接口 ───────────────────────────────────────────────

def put_session(token: str, data: dict, ttl_seconds: int) -> None:
    """写入 / 覆盖一条会话。`data` 至少含 `user_id`（反向索引要用）。

    ⚠️ 本函数**不**在 `data` 里补 `expires_ts` —— 那由 file 后端自己维护（回滚兼容用）。
       调用方（service）不得依赖这个字段，两个后端的返回因此可能差这一个键。
    """
    _impl.put_session(token, data, ttl_seconds)


def get_session(token: str) -> dict | None:
    """取一条会话；不存在**或已过期**返回 None。Redis 后端下是一次 GET（O(1)）。"""
    return _impl.get_session(token)


def delete_session(token: str) -> bool:
    """删除一条会话（登出）。幂等：不存在也返回 False，不报错。"""
    return _impl.delete_session(token)


def delete_user_sessions(user_id: str) -> int:
    """删除某用户的**全部**会话，返回删除条数（改密 / 禁用账号时踢下线用）。"""
    return _impl.delete_user_sessions(user_id)


def count_sessions() -> int:
    """当前有效会话数（自检 / 测试用）。"""
    return _impl.count_sessions()


# 供自检 / 日志用：当前生效的后端名（/api/version 之类可回报）
BACKEND = SESSION_STORE
