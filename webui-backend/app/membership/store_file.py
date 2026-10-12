"""会员模块存储后端之一：**JSON 文件**（默认后端，与项目 stores.py 风格一致）。

> 这是 `store.py` 门面背后的一个实现，不要直接 import 它 —— 业务层一律用
> `from . import store`。接口契约见 `store.py` 的 `_REQUIRED_API`。

文件布局（DATA_DIR/members/）：
  users.json         用户表 {user_id: {...}}
  sessions.json      会话表 {token: {...}}
  point_logs.json    积分流水（追加列表，新记录在前）
  redeem_codes.json  优惠码 {code: {...}}
  checkins.json      签到记录 {user_id: {"2026-09-19", ...}}
  orders.json        积分购买订单 {order_id: {...}}（充值链路，2026-10-05 新增）

并发策略：模块级 threading.Lock 串行化读改写；写入采用 tmp+replace 原子替换。
⚠️ 该锁**只覆盖单进程**（`main.run()` 未传 workers）；多实例部署需换后端，
   见 STORAGE_MIGRATION_PLAN.md。

规模预期（几百~几千用户、几万条流水）下 JSON 文件足够。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from ..config import DATA_DIR

MEMBERS_DIR = DATA_DIR / "members"
MEMBERS_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = MEMBERS_DIR / "users.json"
SESSIONS_FILE = MEMBERS_DIR / "sessions.json"
POINT_LOGS_FILE = MEMBERS_DIR / "point_logs.json"
REDEEM_CODES_FILE = MEMBERS_DIR / "redeem_codes.json"
CHECKINS_FILE = MEMBERS_DIR / "checkins.json"

_lock = threading.RLock()


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 损坏时备份原文件并返回默认值，避免整站不可用
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        return default


def _write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ─── 用户 ───────────────────────────────────────────────────

def load_users() -> dict:
    with _lock:
        return _read(USERS_FILE, {})


def save_users(users: dict) -> None:
    with _lock:
        _write(USERS_FILE, users)


# ─── 会话 ───────────────────────────────────────────────────

def load_sessions() -> dict:
    with _lock:
        return _read(SESSIONS_FILE, {})


def save_sessions(sessions: dict) -> None:
    with _lock:
        _write(SESSIONS_FILE, sessions)


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


def trim_point_logs(user_id: str, keep: int) -> None:
    """把某用户的流水裁到最多 keep 条（从尾部、即最旧的开始丢）。

    原先是 service 层 `_trim_logs()` 直接操作文件私有 API（`_lock`/`_read`/`_write`）
    —— 抽象层已经泄漏，2026-10-12 抽接口时下沉到这里，语义不变。
    上限（`MAX_POINT_LOGS_PER_USER`）是**业务策略**，仍由调用方传入。
    """
    with _lock:
        logs = _read(POINT_LOGS_FILE, [])
        mine = [i for i, e in enumerate(logs) if e.get("user_id") == user_id]
        if len(mine) > keep:
            drop_indexes = set(mine[keep:])
            logs = [e for i, e in enumerate(logs) if i not in drop_indexes]
            _write(POINT_LOGS_FILE, logs)


# ─── 优惠码 ─────────────────────────────────────────────────

def load_redeem_codes() -> dict:
    with _lock:
        return _read(REDEEM_CODES_FILE, {})


def save_redeem_codes(codes: dict) -> None:
    with _lock:
        _write(REDEEM_CODES_FILE, codes)


# ─── 签到 ───────────────────────────────────────────────────

def load_checkins() -> dict:
    with _lock:
        return _read(CHECKINS_FILE, {})


def save_checkins(data: dict) -> None:
    with _lock:
        _write(CHECKINS_FILE, data)


# ─── 积分购买订单 ───────────────────────────────────────────

ORDERS_FILE = MEMBERS_DIR / "orders.json"


def load_orders() -> dict:
    with _lock:
        return _read(ORDERS_FILE, {})


def save_orders(orders: dict) -> None:
    with _lock:
        _write(ORDERS_FILE, orders)


# ─── 邮箱验证码 ─────────────────────────────────────────────

EMAIL_CODES_FILE = MEMBERS_DIR / "email_codes.json"


def load_email_codes() -> dict:
    with _lock:
        return _read(EMAIL_CODES_FILE, {})


def save_email_codes(data: dict) -> None:
    with _lock:
        _write(EMAIL_CODES_FILE, data)
