"""会员模块业务层：注册/登录、资料、积分、优惠码、签到、扣费钩子。

配置（环境变量或 .env，均可缺省）：
  MEMBER_REG_BONUS              注册赠送积分，默认 100；0 表示关闭
  MEMBER_CHECKIN_BONUS          每日签到积分，默认 5；0 表示关闭
  MEMBER_POINTS_PER_1000_CHARS  合成扣费：每 1000 字符扣积分，默认 10；0 表示关闭按量扣费
  MEMBER_TOKEN_TTL_DAYS         会话有效期（天），默认 30
  MEMBER_ENFORCE                1 = 未登录/积分不足时拒绝提交合成任务；默认 0（仅登录用户记账，不强制）
  MEMBER_ADMIN_TOKEN            管理接口令牌；未设置则管理接口整体禁用
"""

from __future__ import annotations

import math
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from . import store
from .security import hash_password, new_token, verify_password
from . import mailer

# ─── 配置 ───────────────────────────────────────────────────

REG_BONUS = int(os.environ.get("MEMBER_REG_BONUS", "100"))
CHECKIN_BONUS = int(os.environ.get("MEMBER_CHECKIN_BONUS", "5"))
POINTS_PER_1000_CHARS = int(os.environ.get("MEMBER_POINTS_PER_1000_CHARS", "10"))
TOKEN_TTL_DAYS = int(os.environ.get("MEMBER_TOKEN_TTL_DAYS", "30"))
ENFORCE = os.environ.get("MEMBER_ENFORCE", "0") == "1"
ADMIN_TOKEN = os.environ.get("MEMBER_ADMIN_TOKEN", "") or None

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fa5]{2,24}$")
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
MAX_POINT_LOGS_PER_USER = 500  # 每用户流水上限（防无限增长），超限丢最旧的

# 邮箱验证码策略
EMAIL_CODE_TTL = 10 * 60          # 有效期 10 分钟
EMAIL_CODE_RESEND_INTERVAL = 60   # 同邮箱重发间隔
EMAIL_CODE_DAILY_LIMIT = 10       # 同邮箱每日上限
EMAIL_CODE_MAX_ATTEMPTS = 5       # 验证错误次数上限，超过作废


class MemberError(Exception):
    """业务错误：message 面向用户，code 为 HTTP 状态码。"""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


# ─── 用户序列化 ─────────────────────────────────────────────

def public_user(user: dict) -> dict:
    """对外暴露的用户字段（绝不包含口令相关内容）。"""
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "nickname": user.get("nickname") or user["username"],
        "bio": user.get("bio") or "",
        "email": user.get("email") or "",
        "points": user.get("points", 0),
        "disabled": user.get("disabled", False),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
    }


# ─── 会话 ───────────────────────────────────────────────────

def _purge_expired_sessions(sessions: dict) -> None:
    now = time.time()
    expired = [t for t, s in sessions.items() if s.get("expires_ts", 0) < now]
    for t in expired:
        del sessions[t]


def resolve_token(token: Optional[str]) -> Optional[dict]:
    """token → 用户；无效/过期返回 None（不抛错，供可选鉴权使用）。"""
    if not token:
        return None
    sessions = store.load_sessions()
    session = sessions.get(token)
    if not session or session.get("expires_ts", 0) < time.time():
        return None
    user = store.load_users().get(session["user_id"])
    if not user or user.get("disabled"):
        return None
    return user


def create_session(user_id: str) -> str:
    sessions = store.load_sessions()
    _purge_expired_sessions(sessions)
    token = new_token()
    sessions[token] = {
        "user_id": user_id,
        "created_at": datetime.now().isoformat(),
        "expires_ts": time.time() + TOKEN_TTL_DAYS * 86400,
    }
    store.save_sessions(sessions)
    return token


def revoke_session(token: str) -> None:
    sessions = store.load_sessions()
    if token in sessions:
        del sessions[token]
        store.save_sessions(sessions)


# ─── 注册 / 登录 ────────────────────────────────────────────

def register(username: str, password: str, nickname: str = "") -> dict:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise MemberError("用户名需 2-24 位，仅限中英文、数字、下划线、连字符")
    if len(password or "") < 6:
        raise MemberError("密码至少 6 位")
    users = store.load_users()
    if any(u["username"] == username for u in users.values()):
        raise MemberError("用户名已被注册")
    user_id = f"u_{uuid.uuid4().hex[:12]}"
    salt, pwd_hash = hash_password(password)
    user = {
        "user_id": user_id,
        "username": username,
        "nickname": (nickname or "").strip() or username,
        "bio": "",
        "salt": salt,
        "password_hash": pwd_hash,
        "points": 0,
        "disabled": False,
        "created_at": datetime.now().isoformat(),
        "last_login_at": None,
    }
    users[user_id] = user
    store.save_users(users)
    if REG_BONUS > 0:
        _apply_delta(user, REG_BONUS, "earn", "注册赠送")
        user = store.load_users()[user_id]  # 取加赠后的最新数据返回给前端
    token = create_session(user_id)
    return {"token": token, "user": public_user(user)}


def login(identifier: str, password: str) -> dict:
    """登录。identifier 可以是用户名或邮箱（邮箱注册的用户用邮箱登录）。"""
    ident = (identifier or "").strip()
    users = store.load_users()
    user = next(
        (u for u in users.values() if u["username"] == ident or (u.get("email") or "") == ident.lower()),
        None,
    )
    if not user or not verify_password(password or "", user["salt"], user["password_hash"]):
        raise MemberError("用户名/邮箱或密码错误", 401)
    if user.get("disabled"):
        raise MemberError("账号已被禁用，请联系管理员", 403)
    user["last_login_at"] = datetime.now().isoformat()
    store.save_users(users)
    token = create_session(user["user_id"])
    return {"token": token, "user": public_user(user)}


def logout(token: str) -> None:
    revoke_session(token)


# ─── 邮箱验证码 / 邮箱注册 ─────────────────────────────────

def request_email_code(email: str) -> dict:
    """发送注册验证码。限频：同邮箱 60s 一条、每日 10 条。"""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise MemberError("邮箱格式不正确")
    if not mailer.configured():
        raise MemberError("邮箱服务未配置（服务端需设置 SMTP_* 环境变量）", 503)
    users = store.load_users()
    if any((u.get("email") or "") == email for u in users.values()):
        raise MemberError("该邮箱已注册，可直接登录", 409)
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    codes = store.load_email_codes()
    entry = codes.get(email)
    if entry:
        if now - entry.get("sent_ts", 0) < EMAIL_CODE_RESEND_INTERVAL:
            wait = int(EMAIL_CODE_RESEND_INTERVAL - (now - entry.get("sent_ts", 0)))
            raise MemberError(f"发送太频繁，请 {wait} 秒后再试", 429)
        if entry.get("day") == today and entry.get("day_count", 0) >= EMAIL_CODE_DAILY_LIMIT:
            raise MemberError("该邮箱今日验证码发送次数已达上限", 429)
    code = f"{secrets.randbelow(1000000):06d}"
    # 顺带清理过期条目，防文件膨胀
    codes = {k: v for k, v in codes.items() if v.get("expires_ts", 0) > now - 86400}
    codes[email] = {
        "code": code,
        "expires_ts": now + EMAIL_CODE_TTL,
        "sent_ts": now,
        "attempts": 0,
        "day": today,
        "day_count": (entry.get("day_count", 0) + 1) if entry and entry.get("day") == today else 1,
    }
    store.save_email_codes(codes)
    try:
        mailer.send_verification_code(email, code, EMAIL_CODE_TTL // 60)
    except Exception as e:  # 发送失败即作废本次验证码，避免"收不到码还报成功"
        codes = store.load_email_codes()
        codes.pop(email, None)
        store.save_email_codes(codes)
        raise MemberError(f"验证码发送失败，请稍后重试（{type(e).__name__}）", 502)
    return {"ok": True, "ttl_minutes": EMAIL_CODE_TTL // 60}


def register_email(email: str, code: str, password: str, nickname: str = "") -> dict:
    """邮箱注册：验证码通过后建号。用户名从邮箱前缀自动派生并保证唯一。"""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not EMAIL_RE.match(email):
        raise MemberError("邮箱格式不正确")
    if len(password or "") < 6:
        raise MemberError("密码至少 6 位")
    codes = store.load_email_codes()
    entry = codes.get(email)
    if not entry:
        raise MemberError("请先获取验证码")
    if entry.get("expires_ts", 0) < time.time():
        raise MemberError("验证码已过期，请重新获取")
    if entry.get("attempts", 0) >= EMAIL_CODE_MAX_ATTEMPTS:
        raise MemberError("验证码错误次数过多，请重新获取")
    if code != entry["code"]:
        entry["attempts"] = entry.get("attempts", 0) + 1
        store.save_email_codes(codes)
        remain = EMAIL_CODE_MAX_ATTEMPTS - entry["attempts"]
        raise MemberError(f"验证码错误（剩余 {max(0, remain)} 次机会）")
    users = store.load_users()
    if any((u.get("email") or "") == email for u in users.values()):
        raise MemberError("该邮箱已注册，可直接登录", 409)
    # 用户名派生：邮箱前缀过滤成合法字符，冲突则加随机后缀
    base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fa5]", "", email.split("@")[0])[:20] or "user"
    username = base
    while any(u["username"] == username for u in users.values()):
        username = f"{base}_{uuid.uuid4().hex[:4]}"
    # 验证通过：作废验证码（一次性）
    codes.pop(email, None)
    store.save_email_codes(codes)

    user_id = f"u_{uuid.uuid4().hex[:12]}"
    salt, pwd_hash = hash_password(password)
    user = {
        "user_id": user_id,
        "username": username,
        "nickname": (nickname or "").strip() or username,
        "bio": "",
        "email": email,
        "salt": salt,
        "password_hash": pwd_hash,
        "points": 0,
        "disabled": False,
        "created_at": datetime.now().isoformat(),
        "last_login_at": None,
    }
    users[user_id] = user
    store.save_users(users)
    if REG_BONUS > 0:
        _apply_delta(user, REG_BONUS, "earn", "注册赠送")
        user = store.load_users()[user_id]
    token = create_session(user_id)
    return {"token": token, "user": public_user(user)}


# ─── 资料编辑 ───────────────────────────────────────────────

def update_profile(user: dict, nickname: Optional[str] = None, bio: Optional[str] = None) -> dict:
    users = store.load_users()
    target = users.get(user["user_id"])
    if not target:
        raise MemberError("用户不存在", 404)
    if nickname is not None:
        nickname = nickname.strip()
        if not (1 <= len(nickname) <= 24):
            raise MemberError("昵称需 1-24 个字符")
        target["nickname"] = nickname
    if bio is not None:
        if len(bio) > 200:
            raise MemberError("简介最多 200 字")
        target["bio"] = bio
    target["updated_at"] = datetime.now().isoformat()
    store.save_users(users)
    return public_user(target)


def change_password(user: dict, old_password: str, new_password: str) -> None:
    users = store.load_users()
    target = users.get(user["user_id"])
    if not target:
        raise MemberError("用户不存在", 404)
    if not verify_password(old_password or "", target["salt"], target["password_hash"]):
        raise MemberError("当前密码错误")
    if len(new_password or "") < 6:
        raise MemberError("新密码至少 6 位")
    target["salt"], target["password_hash"] = hash_password(new_password)
    target["updated_at"] = datetime.now().isoformat()
    store.save_users(users)
    # 改密后吊销该用户全部会话（除当前）——此处简单起见全部吊销，前端会跳登录页
    sessions = store.load_sessions()
    changed = False
    for t in [t for t, s in sessions.items() if s.get("user_id") == user["user_id"]]:
        del sessions[t]
        changed = True
    if changed:
        store.save_sessions(sessions)


# ─── 积分核心 ───────────────────────────────────────────────

def _apply_delta(user: dict, delta: int, kind: str, reason: str, ref: str = "",
                 log_id: Optional[str] = None) -> int:
    """写入积分变动 + 流水。delta 可正可负；余额不足时抛错（kind=spend）。"""
    users = store.load_users()
    target = users.get(user["user_id"])
    if not target:
        raise MemberError("用户不存在", 404)
    new_balance = target.get("points", 0) + delta
    if new_balance < 0:
        raise MemberError(f"积分不足（当前 {target.get('points', 0)}，需要 {-delta}）", 402)
    target["points"] = new_balance
    target["updated_at"] = datetime.now().isoformat()
    store.save_users(users)
    log_entry = {
        "id": log_id or f"pl_{uuid.uuid4().hex[:12]}",
        "user_id": target["user_id"],
        "delta": delta,
        "balance_after": new_balance,
        "kind": kind,  # earn/spend/redeem/checkin/grant/refund
        "reason": reason,
        "ref": ref,
        "created_at": datetime.now().isoformat(),
    }
    store.append_point_log(log_entry)
    _trim_logs(target["user_id"])
    return new_balance


def _trim_logs(user_id: str) -> None:
    """每用户流水超出上限时，从尾部（最旧）截断。"""
    with store._lock:
        logs = store._read(store.POINT_LOGS_FILE, [])
        mine = [i for i, e in enumerate(logs) if e.get("user_id") == user_id]
        if len(mine) > MAX_POINT_LOGS_PER_USER:
            drop_indexes = set(mine[MAX_POINT_LOGS_PER_USER:])
            logs = [e for i, e in enumerate(logs) if i not in drop_indexes]
            store._write(store.POINT_LOGS_FILE, logs)


def list_point_logs(user_id: str, offset: int = 0, limit: int = 20) -> dict:
    logs = [e for e in store.load_point_logs() if e.get("user_id") == user_id]
    total = len(logs)
    page = logs[offset: offset + max(1, min(limit, 100))]
    return {"total": total, "offset": offset, "logs": page}


# ─── 优惠码 ─────────────────────────────────────────────────

def create_redeem_codes(count: int, points: int, max_uses: int = 1,
                        expires_days: int = 0, note: str = "") -> list[str]:
    """批量生成优惠码（管理端）。expires_days=0 表示永久有效。"""
    if points <= 0:
        raise MemberError("面值必须大于 0")
    if not (1 <= count <= 100):
        raise MemberError("单次生成 1-100 个")
    max_uses = max(1, max_uses)
    codes = store.load_redeem_codes()
    created = []
    for _ in range(count):
        code = uuid.uuid4().hex[:10].upper()
        codes[code] = {
            "code": code,
            "points": points,
            "max_uses": max_uses,
            "used_count": 0,
            "used_by": [],  # [{user_id, username, at}]
            "expires_ts": (time.time() + expires_days * 86400) if expires_days > 0 else None,
            "note": note,
            "created_at": datetime.now().isoformat(),
            "disabled": False,
        }
        created.append(code)
    store.save_redeem_codes(codes)
    return created


def redeem(user: dict, code: str) -> dict:
    code = (code or "").strip().upper()
    codes = store.load_redeem_codes()
    entry = codes.get(code)
    if not entry:
        raise MemberError("优惠码不存在")
    if entry.get("disabled"):
        raise MemberError("优惠码已停用")
    if entry.get("expires_ts") and entry["expires_ts"] < time.time():
        raise MemberError("优惠码已过期")
    if entry.get("used_count", 0) >= entry.get("max_uses", 1):
        raise MemberError("优惠码已被使用")
    if any(u["user_id"] == user["user_id"] for u in entry.get("used_by", [])):
        raise MemberError("您已使用过该优惠码")
    # 先占坑再发积分，防止并发重复兑换
    entry["used_count"] = entry.get("used_count", 0) + 1
    entry["used_by"].append({
        "user_id": user["user_id"],
        "username": user["username"],
        "at": datetime.now().isoformat(),
    })
    store.save_redeem_codes(codes)
    try:
        balance = _apply_delta(user, entry["points"], "redeem", f"优惠码兑换 {code}", ref=code)
    except MemberError:
        # 理论上不会发生（兑换只加不减），占坑回滚保平安
        entry["used_count"] -= 1
        entry["used_by"] = [u for u in entry["used_by"] if u["user_id"] != user["user_id"]]
        store.save_redeem_codes(codes)
        raise
    return {"added": entry["points"], "balance": balance, "code": code}


# ─── 每日签到 ───────────────────────────────────────────────

def checkin(user: dict) -> dict:
    if CHECKIN_BONUS <= 0:
        raise MemberError("签到功能未开启")
    today = datetime.now().strftime("%Y-%m-%d")
    data = store.load_checkins()
    days = data.setdefault(user["user_id"], set())
    days = set(days)
    if today in days:
        raise MemberError("今天已经签到过啦", 409)
    days.add(today)
    data[user["user_id"]] = list(days)
    store.save_checkins(data)
    balance = _apply_delta(user, CHECKIN_BONUS, "checkin", f"每日签到 {today}")
    return {"added": CHECKIN_BONUS, "balance": balance, "date": today}


def checkin_status(user: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    days = store.load_checkins().get(user["user_id"], [])
    return {"checked_in_today": today in days, "total_days": len(days), "bonus": CHECKIN_BONUS}


# ─── 合成任务扣费钩子（MEMBER_ENFORCE / 记账模式） ──────────

def estimate_task_cost(lines: list) -> int:
    """按任务总字数估算积分成本。POINTS_PER_1000_CHARS=0 表示不计费。"""
    if POINTS_PER_1000_CHARS <= 0:
        return 0
    chars = 0
    for line in lines or []:
        if isinstance(line, dict):
            chars += len((line.get("text") or "").strip())
        elif isinstance(line, str):
            chars += len(line.strip())
    return math.ceil(chars / 1000) * POINTS_PER_1000_CHARS


def charge_for_task(user: dict, lines: list, task_id: str) -> dict:
    """提交任务时预扣积分。成本为 0 时返回 {"balance": 当前, "log_id": ""}。

    返回 log_id 供退款幂等：每个扣费记录只允许对应一笔退款。
    """
    cost = estimate_task_cost(lines)
    if cost <= 0:
        return {"balance": user.get("points", 0), "log_id": ""}
    users = store.load_users()
    target = users.get(user["user_id"])
    if not target:
        raise MemberError("用户不存在", 404)
    balance = target.get("points", 0)
    if balance < cost:
        raise MemberError(f"积分不足（当前 {balance}，本次任务需 {cost}）", 402)
    log_id = f"pl_{uuid.uuid4().hex[:12]}"
    new_balance = _apply_delta(target, -cost, "spend", f"合成扣费 {task_id}", ref=task_id, log_id=log_id)
    return {"balance": new_balance, "log_id": log_id}


def refund_task_charge(user_id: str, task_id: str, amount: int, charge_log_id: str = "") -> None:
    """任务失败/取消/中断时退还预扣积分。

    幂等：以 f"{task_id}:{charge_log_id}" 作为退款 ref，同一笔扣费只会退一次。
    """
    if amount <= 0:
        return
    users = store.load_users()
    user = users.get(user_id)
    if not user:
        return
    refund_ref = f"{task_id}:{charge_log_id or 'legacy'}"
    if _has_refund(user_id, refund_ref):
        return
    _apply_delta(user, amount, "refund", f"任务退款 {task_id}", ref=refund_ref)


def _has_refund(user_id: str, ref: str) -> bool:
    return any(
        e.get("user_id") == user_id and e.get("ref") == ref and e.get("kind") == "refund"
        for e in store.load_point_logs()
    )


def rebalance_task_charge(user: dict, lines: list, task_id: str,
                          prev_charged: int, prev_log_id: str) -> dict:
    """编辑已扣费任务后按新字数多退少补。返回新扣费额与 log_id。"""
    new_cost = estimate_task_cost(lines)
    diff = new_cost - prev_charged
    log_id = prev_log_id
    if diff > 0:
        users = store.load_users()
        target = users.get(user["user_id"])
        if not target:
            raise MemberError("用户不存在", 404)
        if target.get("points", 0) < diff:
            raise MemberError(f"积分不足（当前 {target.get('points', 0)}，还需 {diff}）", 402)
        _apply_delta(target, -diff, "spend", f"任务改稿补扣 {task_id}", ref=task_id)
    elif diff < 0:
        users = store.load_users()
        target = users.get(user["user_id"])
        if target:
            _apply_delta(target, -diff, "refund", f"任务改稿退差 {task_id}", ref=task_id)
    return {"charged": new_cost, "log_id": log_id}


# ─── 管理端 ─────────────────────────────────────────────────

def admin_grant_points(username_or_id: str, delta: int, reason: str) -> dict:
    users = store.load_users()
    user = next(
        (u for u in users.values() if u["username"] == username_or_id or u["user_id"] == username_or_id),
        None,
    )
    if not user:
        raise MemberError("用户不存在", 404)
    if delta == 0:
        raise MemberError("变动值不能为 0")
    balance = _apply_delta(user, delta, "grant", reason or "管理员调整")
    return {"user": public_user(store.load_users()[user["user_id"]]), "balance": balance}


def admin_list_users(query: str = "") -> list[dict]:
    users = store.load_users()
    q = (query or "").strip().lower()
    result = []
    for u in users.values():
        if q and q not in u["username"].lower() and q not in (u.get("nickname") or "").lower():
            continue
        result.append(public_user(u))
    result.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return result


def admin_set_disabled(username_or_id: str, disabled: bool) -> dict:
    users = store.load_users()
    user = next(
        (u for u in users.values() if u["username"] == username_or_id or u["user_id"] == username_or_id),
        None,
    )
    if not user:
        raise MemberError("用户不存在", 404)
    user["disabled"] = disabled
    store.save_users(users)
    if disabled:
        # 禁用即踢下线
        sessions = store.load_sessions()
        changed = False
        for t in [t for t, s in sessions.items() if s.get("user_id") == user["user_id"]]:
            del sessions[t]
            changed = True
        if changed:
            store.save_sessions(sessions)
    return public_user(user)


def admin_list_codes() -> list[dict]:
    codes = sorted(store.load_redeem_codes().values(), key=lambda c: c.get("created_at") or "", reverse=True)
    now = time.time()
    for c in codes:
        c["expired"] = bool(c.get("expires_ts") and c["expires_ts"] < now)
    return codes
