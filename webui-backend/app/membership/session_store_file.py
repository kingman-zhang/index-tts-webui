"""会话存储后端之一：**JSON 文件**（默认后端，与历史完全一致）。

> 这是 `session_store.py` 门面背后的一个实现，不要直接 import 它 —— 业务层一律用
> `from . import session_store`。接口契约见 `session_store.py` 的 `_REQUIRED_API`。

存储形态：`DATA_DIR/members/sessions.json`
    {token: {"user_id": ..., "created_at": ..., "expires_ts": <float>}}

**与 Gate 3 之前逐字节相同**（同路径、同字段）—— 所以 `SESSION_STORE` 从 `redis`
改回 `file` 时不需要任何数据迁移，回滚即可用。

关于 `expires_ts`
-----------------
本后端自己维护、自己判过期，写到文件里。**调用方不要依赖这个字段**：Redis 后端
不写它（过期交给 TTL）。保留它的意义是**回滚兼容** —— 降级回旧代码时，旧代码正是
读它来判断会话是否过期的。

并发：模块级 threading.Lock 串行化读改写（与 store_file 同策略，**只覆盖单进程**）。
文件原语（读 / 原子写）来自 json_store.py。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from ..config import DATA_DIR
from .json_store import read_json as _read, write_json as _write

MEMBERS_DIR = DATA_DIR / "members"
MEMBERS_DIR.mkdir(parents=True, exist_ok=True)

SESSIONS_FILE = MEMBERS_DIR / "sessions.json"

_lock = threading.RLock()


def _expires_at(session: dict) -> float:
    try:
        return float(session.get("expires_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _purge_expired_locked(sessions: dict) -> int:
    """删掉已过期条目（调用方须持锁）。file 没有 TTL，只能靠惰性清理。"""
    now = time.time()
    expired = [t for t, s in sessions.items() if _expires_at(s) < now]
    for t in expired:
        del sessions[t]
    return len(expired)


# ─── 契约接口 ───────────────────────────────────────────────

def put_session(token: str, data: dict, ttl_seconds: int) -> None:
    with _lock:
        sessions = _read(SESSIONS_FILE, {})
        _purge_expired_locked(sessions)   # 顺手清，避免文件无限增长
        sessions[token] = {**data, "expires_ts": time.time() + max(0, int(ttl_seconds))}
        _write(SESSIONS_FILE, sessions)


def get_session(token: str) -> Optional[dict]:
    with _lock:
        sessions = _read(SESSIONS_FILE, {})
        s = sessions.get(token)
        if not s:
            return None
        if _expires_at(s) < time.time():
            # 过期即清（读到就顺手收拾，省得攒着）
            del sessions[token]
            _write(SESSIONS_FILE, sessions)
            return None
        return dict(s)


def delete_session(token: str) -> bool:
    """返回是否真的删掉了（幂等：不存在也返回 False，不报错）。"""
    with _lock:
        sessions = _read(SESSIONS_FILE, {})
        if token not in sessions:
            return False
        del sessions[token]
        _write(SESSIONS_FILE, sessions)
        return True


def delete_user_sessions(user_id: str) -> int:
    """删掉某用户的全部会话，返回删除条数（改密 / 禁用账号时踢下线用）。"""
    with _lock:
        sessions = _read(SESSIONS_FILE, {})
        targets = [t for t, s in sessions.items() if s.get("user_id") == user_id]
        if not targets:
            return 0
        for t in targets:
            del sessions[t]
        _write(SESSIONS_FILE, sessions)
        return len(targets)


def count_sessions() -> int:
    """当前**未过期**的会话数（自检 / 测试用）。"""
    with _lock:
        now = time.time()
        return sum(1 for s in _read(SESSIONS_FILE, {}).values() if _expires_at(s) >= now)
