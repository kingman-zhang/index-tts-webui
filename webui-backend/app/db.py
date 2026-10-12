"""MongoDB / Redis 连接管理（懒连接、短超时、失败不阻塞启动）。

定位
----
本模块**只负责拿到可用的客户端**，不含任何业务逻辑；谁用谁 import。

三条设计约束（与 STORAGE_MIGRATION_PLAN.md §6 一致）
----------------------------------------------------
1. **懒连接**：import 本模块不产生任何网络请求；首次 `get_mongo_client()` /
   `get_redis()` 才建客户端，首次真正操作才建连接。
2. **失败不阻塞启动**：连不上时抛 `DBUnavailable`，由调用方决定怎么降级 ——
   backend 启动时的探活失败**不应**阻止进程起来（合成链路不依赖 DB）。
3. **超时短**：`DB_TIMEOUT_MS` 默认 3000 ms。Mongo 客户端默认 `serverSelectionTimeoutMS`
   是 30 s，配错地址时会把每个接口拖到 30 秒才失败。

⚠️ `pymongo` / `redis` 是**延迟 import**：`MEMBER_STORE=file`（默认）时这两个包
   完全不参与运行，因此本模块不会成为 file 模式的启动依赖。
"""

from __future__ import annotations

import threading

from .config import (
    DB_TIMEOUT_MS,
    MONGO_DB,
    MONGO_URL,
    REDIS_KEY_PREFIX,
    REDIS_URL,
)
from .config import logger


class DBUnavailable(RuntimeError):
    """数据库不可用（未配置 / 未安装客户端 / 连不上）。

    单独一个类型是为了让上层能**精确降级**（例如接口返回 503 而不是 500）。
    """


# ─── MongoDB ────────────────────────────────────────────────

_mongo_client = None
_mongo_lock = threading.Lock()


def get_mongo_client():
    """进程内共享的 `MongoClient`（懒创建，线程安全）。

    ⚠️ `MongoClient(...)` **本身不建连** —— 地址写错也要等第一次操作才暴露。
    要确认「到底连不连得上」，用 `mongo_ok()`（它主动 ping），
    而不是 try/except 本函数。
    """
    global _mongo_client
    if _mongo_client is not None:
        return _mongo_client
    with _mongo_lock:
        if _mongo_client is None:
            if not MONGO_URL:
                raise DBUnavailable(
                    "MONGO_URL 未配置。请在 .env 里补上连接串（凭据不要写进代码/文档）；"
                    "若不想用 Mongo，保持 MEMBER_STORE=file 即可。"
                )
            try:
                from pymongo import MongoClient
            except ImportError as e:  # 延迟 import：file 模式不需要这个包
                raise DBUnavailable(
                    "未安装 pymongo；使用 Mongo 后端需要它（pip install pymongo）。"
                ) from e
            _mongo_client = MongoClient(
                MONGO_URL,
                serverSelectionTimeoutMS=DB_TIMEOUT_MS,
                connectTimeoutMS=DB_TIMEOUT_MS,
                socketTimeoutMS=DB_TIMEOUT_MS,
                appname="podcast-webui",
            )
        return _mongo_client


def get_mongo_db():
    """账本所在的 database 句柄（库名 = `MONGO_DB`，默认 `podcast`）。"""
    return get_mongo_client()[MONGO_DB]


def mongo_ok() -> bool:
    """探活：能 ping 通返回 True；任何异常都吞掉返回 False（**不抛**）。"""
    try:
        get_mongo_client().admin.command("ping")
        return True
    except Exception:
        return False


# ─── Redis ──────────────────────────────────────────────────

_redis_client = None
_redis_lock = threading.Lock()


def get_redis():
    """进程内共享的 `redis.Redis`（懒创建，线程安全）。

    连接池由 redis-py 内部管理；`decode_responses=True` 是因为我们存的是
    JSON 文本，避免每处都手动 decode。
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is None:
            if not REDIS_URL:
                raise DBUnavailable(
                    "REDIS_URL 未配置。请在 .env 里补上"
                    "（形如 redis://:密码@主机:端口/库号）；不用 Redis 时无需配置。"
                )
            try:
                import redis
            except ImportError as e:  # 延迟 import：file 模式不需要这个包
                raise DBUnavailable(
                    "未安装 redis；会话入 Redis 需要它（pip install redis）。"
                ) from e
            _redis_client = redis.Redis.from_url(
                REDIS_URL,
                socket_timeout=DB_TIMEOUT_MS / 1000,
                socket_connect_timeout=DB_TIMEOUT_MS / 1000,
                decode_responses=True,
                health_check_interval=30,
            )
        return _redis_client


def redis_ok() -> bool:
    """探活：PING 通返回 True；任何异常都吞掉返回 False（**不抛**）。"""
    try:
        return bool(get_redis().ping())
    except Exception:
        return False


def redis_key(*parts: str) -> str:
    """拼 Redis key，**强制**带统一前缀 `REDIS_KEY_PREFIX`（默认 `podcast`）。

    这个实例上还有别的项目在用，`session:{token}` 这种通用名会互踩 ——
    所以这里**不提供**「不带前缀」的写法，从接口上杜绝。
    """
    return ":".join([REDIS_KEY_PREFIX, *(str(p) for p in parts)])


# ─── 自检 / 收尾 ────────────────────────────────────────────

def probe() -> dict:
    """一次性探活快照，供自检工具或 `/api/version` 回报（**不抛错**）。"""
    return {
        "mongo": {"configured": bool(MONGO_URL), "ok": mongo_ok() if MONGO_URL else False},
        "redis": {"configured": bool(REDIS_URL), "ok": redis_ok() if REDIS_URL else False},
    }


def close_all() -> None:
    """关闭已建立的客户端（测试隔离 / 优雅退出用）。"""
    global _mongo_client, _redis_client
    with _mongo_lock:
        if _mongo_client is not None:
            try:
                _mongo_client.close()
            except Exception as e:  # 关闭失败不该影响退出
                logger.warning("关闭 Mongo 连接失败（忽略）：%s", e)
            _mongo_client = None
    with _redis_lock:
        if _redis_client is not None:
            try:
                _redis_client.close()
            except Exception as e:
                logger.warning("关闭 Redis 连接失败（忽略）：%s", e)
            _redis_client = None
