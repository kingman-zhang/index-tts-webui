"""会员模块业务层：注册/登录、资料、积分、优惠码、签到、购买订单、扣费钩子。

配置（环境变量或 .env，均可缺省）：
  MEMBER_REG_BONUS              注册赠送积分，默认 500；0 表示关闭
  MEMBER_CHECKIN_BONUS          每日签到积分，默认 20；0 表示关闭
  MEMBER_POINTS_PER_1000_CHARS  合成扣费单价：每 1000 字符扣积分，默认 10；0 = 关闭按量扣费
  MEMBER_MIN_CHARGE             单次合成最低收费（积分），默认 5；0 = 不设地板
  MEMBER_TOKEN_TTL_DAYS         会话有效期（天），默认 30
  MEMBER_ENFORCE                1 = 未登录/积分不足时拒绝提交合成任务；默认 0（仅登录用户记账，不强制）
  MEMBER_REQUIRE_LOGIN          1 = 合成提交必须登录（不扣费）；与 MEMBER_ENFORCE 相互独立
  MEMBER_ADMIN_TOKEN            管理接口令牌；未设置则管理接口整体禁用
  MEMBER_MOCK_PAY               1 = 开放「模拟支付成功」端点（积分商城演示用）；
                                **默认 0**（2026-10-14 由 1 改为 0）——
                                开着等于任何登录用户都能给自己的订单免费入账，
                                只在纯演示环境才置 1。接入真实支付后必须是 0。
  PAY_NOTIFY_SECRET             「通用通道」的回调验签密钥（HMAC-SHA256）；
                                未配置则通用通道的回调一律 503。
  PAY_PROVIDER                  当前生效的**支付通道 id**（注册表见 app/membership/pay.py）；
                                留空 = 未接真实支付（商城只能模拟支付）。当前可选：mpay
  PAY_PUBLIC_BASE_URL           我们的**公网根地址**；回调地址 = {它}/api/pay/notify/{通道id}
                                ⚠️ 支付平台必须能访问到它，否则回调收不到
  PAY_RETURN_URL                付款完成后跳回前端的地址（下发给通道的 return_url）
  PAY_AMOUNT_CHECK              回调金额校验策略的**全局覆盖**：strict/ge/off；
                                留空 = 按通道默认（mpay 为 ge，因为金额会被微调）
  MPAY_*                        mpay 通道配置（PAY_PROVIDER=mpay 时必填）；
                                通道差异只允许出现在 app/membership/pay_mpay.py
"""

from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from datetime import datetime
from typing import Optional

from ..config import logger
from ..db import DBUnavailable
from . import store
from . import session_store
from .store_contract import ORDER_EXPIRED, ORDER_PAID, ORDER_PENDING
from .security import hash_password, new_token, verify_password
from . import mailer
from . import packs as _packs
from . import pay as _pay

# 会员模块的结构版本（供 /api/version 取值域自检）：
#   1 = 只有注册/登录/签到/优惠码/扣费，**没有**购买流程
#   2 = 增加积分套餐 + 购买订单 + 支付回调（2026-10-05）
MEMBERSHIP_SCHEMA_VERSION = 2

# ─── 配置 ───────────────────────────────────────────────────

REG_BONUS = int(os.environ.get("MEMBER_REG_BONUS", "500"))
CHECKIN_BONUS = int(os.environ.get("MEMBER_CHECKIN_BONUS", "20"))
POINTS_PER_1000_CHARS = int(os.environ.get("MEMBER_POINTS_PER_1000_CHARS", "10"))
MIN_CHARGE = int(os.environ.get("MEMBER_MIN_CHARGE", "5"))
TOKEN_TTL_DAYS = int(os.environ.get("MEMBER_TOKEN_TTL_DAYS", "30"))
ENFORCE = os.environ.get("MEMBER_ENFORCE", "0") == "1"
REQUIRE_LOGIN = os.environ.get("MEMBER_REQUIRE_LOGIN", "0") == "1"
ADMIN_TOKEN = os.environ.get("MEMBER_ADMIN_TOKEN", "") or None
# 模拟支付：**默认关**（2026-10-14 由 "1" 改为 "0"）。
# 默认开是个坑：接了真实支付却忘写这一项 ⇒ 任何登录用户都能调 mock-pay 端点
# 给自己的订单免费入账（实测可复现）。默认值应当**安全的那一侧**。
# 纯演示环境（没有真实通道、要给人看购买流程）才显式写 1。
MOCK_PAY = os.environ.get("MEMBER_MOCK_PAY", "0") == "1"
# 真实支付回调验签密钥；空 = 回调端点整体拒收（见 pay.py 的安全边界说明）。
# ⚠️ 这是「通用通道」（pay_generic）的密钥；具体业务通道各有自己的密钥（如 MPAY_KEY）。
PAY_NOTIFY_SECRET = os.environ.get("PAY_NOTIFY_SECRET", "") or None

# ── 真实支付通道（2026-10-14；方案见 PAY_MPAY_PLAN.md，实现见 app/membership/pay*.py）──
#
# **当前生效的支付通道 id** —— 通道注册表在 pay.py。留空 = 未接真实支付：
# 回调端点一律 503、商城只能走模拟支付。加/换通道 = 加一个通道模块 + 改这一行，
# service / routes 不用动（这正是「mpay 不与标准支付耦合」的落点）。
PAY_PROVIDER = (os.environ.get("PAY_PROVIDER", "") or "").strip()
# 我们的**公网根地址**：回调地址 = f"{它}/api/pay/notify/{通道id}"。
# ⚠️ 支付平台必须能访问到它（要填进通道后台），否则回调收不到、只能靠主动查单兜底。
PAY_PUBLIC_BASE_URL = (os.environ.get("PAY_PUBLIC_BASE_URL", "") or "").rstrip("/")
# 付款完成后跳回前端的地址（下发给通道的 return_url）。
PAY_RETURN_URL = os.environ.get("PAY_RETURN_URL", "") or ""
# 回调金额校验策略的**全局覆盖**（留空 = 按通道默认）。
# 可选 strict/ge/off —— 见 pay_contract.AMOUNT_POLICIES 与 pay.resolve_amount_policy。
PAY_AMOUNT_CHECK = (os.environ.get("PAY_AMOUNT_CHECK", "") or "").strip()
# 支付码**有效期**（秒）。
# ⚠️ 必须与支付平台侧那条通道的「识别有效期」设成**同一个值**
#    （mpay 后台：`receipt_valid_seconds`，默认 300、上限 1800）。
#    为什么这件事必须由我们记住：钱到账后**能不能认单，全看平台那个有效期**——
#    平台只在有效期内为订单保留匹配资格；而我们的复用窗口若比它长，
#    用户就会在「平台已经不再认这张码」的时间段里被复用旧码
#    ⇒ 付了钱、平台匹配不到订单 = **必然漏单**（2026-10-15 实测踩到）。
#    取值口径：与 mpay 后台逐字对齐；改一边必须同时改另一边（清单里已列成一步）。
# 2026-10-15 由 900 改为 300（用户拍板统一 5 分钟）：这个值同时决定
#   ① 前端「请在 N 分钟内完成支付」② 复用窗口 ③ 订单多久后判为 expired。
#   ⇒ 三方同源，任一处改了另外两处自动跟随，不会各说各话。
PAY_CHARGE_VALID_SECONDS = int(os.environ.get("PAY_CHARGE_VALID_SECONDS", "300"))
# 出码信息复用窗口（秒）：同一订单在此窗口内重复请求出码，直接复用已有 pay_info，
# 不重复向下游下单 —— 各通道对「同一商户订单号重复下单」的行为不一致，复用最安全。
# ⚠️ 由 `PAY_CHARGE_VALID_SECONDS` **派生**（留 60 秒余量），不是独立开关：
#    「复用窗口 ≤ 平台有效期」是硬约束，写成同一个表达式才不会被人单独调漂。
#    用户在前端看到的「请在 N 分钟内完成支付」也取自有效期那一项，三者同源。
PAY_CHARGE_REUSE_MARGIN_SECONDS = 60
PAY_CHARGE_REUSE_SECONDS = max(0, PAY_CHARGE_VALID_SECONDS - PAY_CHARGE_REUSE_MARGIN_SECONDS)

# ── 通道：mpay_v2（码支付，ePay V1 / MD5）—— 仅 PAY_PROVIDER=mpay 时生效 ──
# 通道专属配置刻意留在 service（与其它 MEMBER_* 同一处），通道模块 delayed-import
# 读它 —— 这样"配置改了不生效"只需查一个地方，测试也能直接改常量即时生效。
MPAY_BASE_URL = (os.environ.get("MPAY_BASE_URL", "") or "").rstrip("/")
MPAY_PID = (os.environ.get("MPAY_PID", "") or "").strip()
MPAY_KEY = os.environ.get("MPAY_KEY", "") or ""
MPAY_TYPE = (os.environ.get("MPAY_TYPE", "wxpay") or "wxpay").strip()

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fa5]{2,24}$")
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
# 行内停顿标记（[pause:0.5] / [pause: 1]），只控制插静音、不计入计费字数。
# 与前端 types/index.ts 的 stripPauseTokens 同一形态。
PAUSE_TOKEN_RE = re.compile(r"\[pause:\s*[\d.]+\s*\]")
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


class SessionStoreUnavailable(Exception):
    """**会话存储不可用** —— 与「token 无效」是两回事，必须能被调用方区分开。

    为什么单列一个异常（2026-10-15 本地"操作几下就被登出"事故）：
      `resolve_token` 原先把存储故障**压成 None**（= 按未登录处理）。要求登录的
      端点因此回 **401**，而前端 `authFetch` 见 401 就 `localStorage.removeItem`
      —— 一次 Redis/TLS 抖动就直接抹掉浏览器里的 token（服务端会话其实还活着，
      TTL 30 天）。日志实证：本地 4 次 `Timeout reading from socket`，每次之后
      用户都得重新登录；生产 Redis 在 compose 内网不抖，所以没暴露。
    ⇒ 现在：存储故障抛本异常，由 `routes.get_optional_user` 决定怎么呈现 ——
      「请求带了 token」⇒ 503（前端不清 token）；「压根没带 token」⇒ 仍返回 None
      （保住"公开链路不因存储故障被打挂"这条原有性质）。
    """

    def __init__(self, detail: str = ""):
        super().__init__(detail or "会话存储不可用")
        self.detail = detail


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
#
# 2026-10-12（Gate 3）：会话从账本存储里拆出来，单独走 session_store 门面
# （`SESSION_STORE=file|redis`）。收益：
#   * `resolve_token` 从「读整份会话表 + 反序列化」降为**一次单键读**（Redis 下 O(1)）；
#   * 过期不再需要自己扫表清理（Redis 靠 TTL，file 后端自己惰性清）；
#   * 「踢某用户全部下线」从「读全表 → 过滤 → 盖回去」变成一个明确的语义操作。
#
# 故障语义（2026-10-15 修订，理由见 SessionStoreUnavailable 的 docstring）：
#   * 读路径（resolve_token）：存储故障 ⇒ **抛 SessionStoreUnavailable**（不再静默
#     返回 None）。调用方 `routes.get_optional_user` 按「这次请求带没带 token」分流：
#     带了 ⇒ 503（前端因此**不会**清本地 token）；没带 ⇒ None（公开链路照旧）。
#   * 建会话（create_session）：存储故障 ⇒ **MemberError(503)**。用户点了登录，必须
#     拿到明确反馈，不能是 500，也不能含混成「密码错误」。
#   * 删会话（登出 / 踢下线）：存储故障 ⇒ **记 warning 后忽略**。故障期间那个 token
#     本来就通不过鉴权，报错只会让前端卡在「登出中」；会话最终由 TTL 自然过期。

def resolve_token(token: Optional[str]) -> Optional[dict]:
    """token → 用户；无效/过期返回 None。

    ⚠️ **存储故障不返回 None，而是抛 `SessionStoreUnavailable`**（见上）。
    """
    if not token:
        return None
    try:
        session = session_store.get_session(token)
    except DBUnavailable as e:
        logger.warning("会话存储不可用（不再按「未登录」处理，由调用方区分 401/503）：%s", e)
        raise SessionStoreUnavailable(str(e)) from e
    if not session:
        return None
    # 只取这一个用户 —— 不再为了鉴权把整张用户表反序列化一遍（Gate 2）
    user = store.get_user(session.get("user_id") or "")
    if not user or user.get("disabled"):
        return None
    return user


def create_session(user_id: str) -> str:
    """建会话，返回 token。有效期 = `MEMBER_TOKEN_TTL_DAYS`（默认 30 天）。"""
    token = new_token()
    try:
        session_store.put_session(
            token,
            {"user_id": user_id, "created_at": datetime.now().isoformat()},
            TOKEN_TTL_DAYS * 86400,
        )
    except DBUnavailable as e:
        raise MemberError("会话服务暂时不可用，请稍后重试", 503) from e
    return token


def revoke_session(token: str) -> None:
    """登出。存储故障不报错（见上方故障语义）。"""
    try:
        session_store.delete_session(token)
    except DBUnavailable as e:
        logger.warning("登出时会话存储不可用（忽略，会话将由 TTL 过期）：%s", e)


def revoke_user_sessions(user_id: str) -> int:
    """吊销某用户的**全部**会话（改密 / 禁用账号踢下线用），返回条数。

    存储故障不报错（见上方故障语义）—— 故障期间该用户本来也鉴权不过。
    """
    try:
        return session_store.delete_user_sessions(user_id)
    except DBUnavailable as e:
        logger.warning("吊销用户会话失败（忽略）：user_id=%s %s", user_id, e)
        return 0


# ─── 注册 / 登录 ────────────────────────────────────────────

def register(username: str, password: str, nickname: str = "") -> dict:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise MemberError("用户名需 2-24 位，仅限中英文、数字、下划线、连字符")
    if len(password or "") < 6:
        raise MemberError("密码至少 6 位")
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
    try:
        # 原子建号：mongo 侧由 username/email 唯一索引保证并发下只成功一个
        store.create_user(user)
    except store.DuplicateUser as e:
        raise MemberError("用户名已被注册" if e.field == "username" else "该邮箱已注册，可直接登录",
                          400 if e.field == "username" else 409) from e
    if REG_BONUS > 0:
        _apply_delta(user, REG_BONUS, "earn", "注册赠送")
        user = store.get_user(user_id) or user  # 取加赠后的最新数据返回给前端
    token = create_session(user_id)
    return {"token": token, "user": public_user(user)}


def login(identifier: str, password: str) -> dict:
    """登录。identifier 可以是用户名或邮箱（邮箱注册的用户用邮箱登录）。"""
    ident = (identifier or "").strip()
    user = store.find_user(ident)
    if not user or not verify_password(password or "", user["salt"], user["password_hash"]):
        raise MemberError("用户名/邮箱或密码错误", 401)
    if user.get("disabled"):
        raise MemberError("账号已被禁用，请联系管理员", 403)
    user["last_login_at"] = datetime.now().isoformat()
    # 只改这一个用户的这一个字段（原来是"读整份 → 改 → 盖回去"，并发会丢别人的更新）
    store.update_user_fields(user["user_id"], {"last_login_at": user["last_login_at"]})
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
    if store.find_user(email):
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
    if store.find_user(email):
        raise MemberError("该邮箱已注册，可直接登录", 409)
    # 用户名派生：邮箱前缀过滤成合法字符，冲突则加随机后缀
    base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fa5]", "", email.split("@")[0])[:20] or "user"
    username = base
    while store.find_user(username):
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
    try:
        store.create_user(user)   # 原子建号（并发下用户名/邮箱唯一）
    except store.DuplicateUser as e:
        if e.field == "email":
            raise MemberError("该邮箱已注册，可直接登录", 409) from e
        raise MemberError("用户名已被注册") from e
    if REG_BONUS > 0:
        _apply_delta(user, REG_BONUS, "earn", "注册赠送")
        user = store.get_user(user_id) or user
    token = create_session(user_id)
    return {"token": token, "user": public_user(user)}


# ─── 资料编辑 ───────────────────────────────────────────────

def update_profile(user: dict, nickname: Optional[str] = None, bio: Optional[str] = None) -> dict:
    fields: dict = {}
    if nickname is not None:
        nickname = nickname.strip()
        if not (1 <= len(nickname) <= 24):
            raise MemberError("昵称需 1-24 个字符")
        fields["nickname"] = nickname
    if bio is not None:
        if len(bio) > 200:
            raise MemberError("简介最多 200 字")
        fields["bio"] = bio
    fields["updated_at"] = datetime.now().isoformat()
    # 只改自己的文档（原来"读整份 → 改 → 盖回去"会覆盖别人的并发更新）
    target = store.update_user_fields(user["user_id"], fields)
    if not target:
        raise MemberError("用户不存在", 404)
    return public_user(target)


def change_password(user: dict, old_password: str, new_password: str) -> None:
    target = store.get_user(user["user_id"])
    if not target:
        raise MemberError("用户不存在", 404)
    if not verify_password(old_password or "", target["salt"], target["password_hash"]):
        raise MemberError("当前密码错误")
    if len(new_password or "") < 6:
        raise MemberError("新密码至少 6 位")
    salt, pwd_hash = hash_password(new_password)
    store.update_user_fields(user["user_id"], {
        "salt": salt,
        "password_hash": pwd_hash,
        "updated_at": datetime.now().isoformat(),
    })
    # 改密后吊销该用户全部会话（除当前）——此处简单起见全部吊销，前端会跳登录页
    revoke_user_sessions(user["user_id"])


# ─── 积分核心 ───────────────────────────────────────────────
#
# 2026-10-12（Gate 2）：**这里原有的三把进程内锁已全部删除**
# （`_user_locks` / `_redeem_lock` / `_order_lock`，都是 threading.RLock）。
# 原因是它们只覆盖单进程：`main.run()` 一旦传了 workers、或起了第二个容器做
# 滚动发布，同一订单就会被两个进程同时入账（支付回调天然是并发重试的）。
#
# 现在"原子性"下沉到存储层，业务层不再需要任何锁：
#   file  后端 = 模块锁内整段完成（仍是单进程语义，但接口与 mongo 一致）
#   mongo 后端 = 条件 $inc（扣费）/ 唯一索引（幂等）/ 单文档 CAS（抢单）
# 详见 app/membership/store_contract.py 与 STORAGE_MIGRATION_PLAN.md §3.2。


def _apply_delta(user: dict, delta: int, kind: str, reason: str, ref: str = "",
                 log_id: Optional[str] = None) -> int:
    """写入积分变动 + 流水，返回新余额。delta 可正可负。

    这是**所有**积分变动的唯一出口（扣费/退款/入账/赠送/签到/兑换都经此），
    所以原子性与幂等只需在这一处保证。余额不足抛 MemberError(402)。

    ⚠️ `store.AlreadyApplied`（幂等命中）**不在这里翻译** —— 退款与订单入账对
    "已经入过账"的处理不同（一个是静默返回，一个是补状态），由各自调用处决定。
    """
    entry = {
        "id": log_id or f"pl_{uuid.uuid4().hex[:12]}",
        "user_id": user["user_id"],
        "delta": int(delta),
        "kind": kind,  # earn/spend/redeem/checkin/grant/refund/purchase
        "reason": reason,
        "ref": ref,
        "created_at": datetime.now().isoformat(),
    }
    try:
        # 流水上限（MAX_POINT_LOGS_PER_USER）是业务策略，由这里传下去
        return store.apply_point_delta(entry, trim_keep=MAX_POINT_LOGS_PER_USER)
    except store.UserNotFound as e:
        raise MemberError("用户不存在", 404) from e
    except store.InsufficientPoints as e:
        raise MemberError(f"积分不足（当前 {e.balance}，需要 {e.need}）", 402) from e


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


# 优惠码不可用的**枚举原因** → 用户文案。存储层只给枚举（那是数据事实），
# 文案是业务的事，所以在这一层翻译。
_CODE_ERRORS = {
    store.CodeUnavailable.NOT_FOUND: "优惠码不存在",
    store.CodeUnavailable.DISABLED: "优惠码已停用",
    store.CodeUnavailable.EXPIRED: "优惠码已过期",
    store.CodeUnavailable.EXHAUSTED: "优惠码已被使用",
    store.CodeUnavailable.ALREADY_USED: "您已使用过该优惠码",
}


def redeem(user: dict, code: str) -> dict:
    """兑换优惠码。

    2026-10-12（Gate 2）：占坑不再是"读码 → 校验 → 写回"，而是存储层的
    **一次原子更新**（mongo 侧把"存在/未停用/未过期/有余量/本人没兑过"五项
    全塞进查询条件，其中 `used_count < max_uses` 用 `$expr` 比较两个字段）。
    并发抢同一张单次码时只可能有一个成功 —— 不再依赖进程级锁。
    """
    code = (code or "").strip().upper()
    try:
        entry = store.claim_redeem_code(code, user["user_id"], user["username"])
    except store.CodeUnavailable as e:
        raise MemberError(_CODE_ERRORS.get(e.reason, "优惠码不可用")) from e

    try:
        balance = _apply_delta(user, entry["points"], "redeem", f"优惠码兑换 {code}", ref=code)
    except MemberError:
        # 理论上不会发生（兑换只加不减）；占坑回滚保平安
        store.release_redeem_code(code, user["user_id"])
        raise
    return {"added": entry["points"], "balance": balance, "code": code}


# ─── 每日签到 ───────────────────────────────────────────────

def checkin(user: dict) -> dict:
    if CHECKIN_BONUS <= 0:
        raise MemberError("签到功能未开启")
    today = datetime.now().strftime("%Y-%m-%d")
    # 原子签到：`(user_id, date)` 的唯一性由存储层保证（mongo 用 `_id` 复合主键，
    # file 用模块锁）。并发 8 个请求也只有 1 个能插入成功，其余收到 409 ——
    # 不再需要"读签到表 → 判断 → 写回"那段临界区。
    if not store.insert_checkin_unique(user["user_id"], today):
        raise MemberError("今天已经签到过啦", 409)
    balance = _apply_delta(user, CHECKIN_BONUS, "checkin", f"每日签到 {today}")
    return {"added": CHECKIN_BONUS, "balance": balance, "date": today}


def checkin_status(user: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    days = store.load_checkins().get(user["user_id"], [])
    return {"checked_in_today": today in days, "total_days": len(days), "bonus": CHECKIN_BONUS}


# ─── 积分购买（套餐 → 订单 → 入账）──────────────────────────
#
# 链路（与「谁收钱」无关，见 pay.py 的说明）：
#
#     用户选套餐 → create_order()  落一条 pending 订单（不碰积分）
#                 ↓
#     支付成功（真实回调 / 模拟）→ settle_order()  幂等入账
#
# 三个必须守住的点：
#   ① 下单**不**给积分 —— 积分只在 settle 时发，否则「下单即可白拿」。
#   ② settle 必须**幂等** —— 同一订单重复通知只能发一次（支付平台会重试，
#      用户也会连点）。判据用积分流水：ref == f"order:{order_id}"。
#   ③ 入账金额以**订单快照**为准，不信回调传来的数字（只用来校验一致性）。

ORDER_ID_PREFIX = "ord_"
# 订单状态常量（ORDER_PENDING / ORDER_PAID）定义在 store_contract —— 存储层
# （CAS 抢单）与业务层共用同一份，避免两处字面量各写一份而漂移。


def pay_channel_ready() -> bool:
    """真实支付通道是否就绪（当前通道已注册、配置齐全**且可出码**）。

    前端与 `/api/points/packs` 据此决定显示「购买」还是「支付通道接入中」。
    未接真实通道时为 False（商城只能走模拟支付）。

    ⚠️ 含 `mod.charge_problem()` 而不只是 `configured()` —— 后者刻意不含形态检查
    （回调不该因 pid 写错被拦），但**出码就绪**必须含，否则前端会给出一个
    点下去必然 503 的按钮。
    """
    mod = _pay.default_provider()
    return bool(mod and mod.configured() and not mod.charge_problem())


def pay_public_base_problem() -> str:
    """`PAY_PUBLIC_BASE_URL` 的形态检查（空串 = 没问题）。

    这个变量只有一个语义：**我们的公网根地址**（回调地址 = 它 + `/api/pay/notify/<通道>`）。
    把完整回调地址抄进来是本项目最难查的配置错 —— 因为 mpay **不校验 notify_url**，
    照样返回下单成功：表现成「二维码正常出、用户付了钱、积分永远不动」的**静默失败**。
    故宁可**响亮失败**（503 + 说清重复在哪一段），也不做"自动去掉重复后缀"的宽容化
    —— 宽容化会把这个坑变成"有时对有时错"，更难查。
    """
    base = PAY_PUBLIC_BASE_URL
    if not base:
        return ("服务端未配置 PAY_PUBLIC_BASE_URL（回调地址根），支付平台将无法回调；"
                "见 .env.example 的「真实支付通道」段")
    marker = "/api/pay/notify"
    if marker in base:
        return (f"PAY_PUBLIC_BASE_URL 应只填**根地址**，但当前值含 {marker!r}（{base!r}）"
                f"—— 拼出来会是 {base}/api/pay/notify/<通道>，重复了一段。"
                "⚠️ mpay 不校验 notify_url 照样回成功 ⇒ 会静默失败"
                "（二维码能出、付了钱、积分不动）")
    return ""


def notify_url_for(provider_id: str) -> str:
    """某通道的异步回调地址 —— **拼接只此一处**（出码与自检共用，避免两处拼法漂移）。"""
    if not PAY_PUBLIC_BASE_URL or not provider_id:
        return ""
    return f"{PAY_PUBLIC_BASE_URL}/api/pay/notify/{provider_id}"


def _est_chars(points: int) -> Optional[int]:
    """这些积分按**当前**计费单价约可合成多少字（不写死在套餐表里的原因见 packs.py）。"""
    if POINTS_PER_1000_CHARS <= 0:
        return None
    return points * 1000 // POINTS_PER_1000_CHARS


def list_packs() -> dict:
    """积分商城商品清单（含按当前单价折算的可合成字数）。"""
    items = []
    for p in _packs.sorted_packs():
        base = p["points"] - p["bonus_points"]
        item = dict(p)
        item["price_yuan"] = f"{p['price_fen'] / 100:.2f}"
        item["est_chars"] = _est_chars(p["points"])
        # 展示用百分比，不参与入账计算
        item["bonus_percent"] = round(p["bonus_points"] / base * 100, 1) if p["bonus_points"] and base > 0 else 0.0
        items.append(item)
    return {
        "packs": items,
        "currency": "CNY",
        # 定价锚点：1 积分 = ¥0.01（前端算「兑换比例」用，避免各自硬编码）
        "points_per_yuan": 100,
        "points_per_1000_chars": POINTS_PER_1000_CHARS,
        "mock_pay_enabled": MOCK_PAY,
        # 真实支付通道是否就绪（当前通道配好了没有）。前端据此决定按钮形态。
        "pay_channel_ready": pay_channel_ready(),
        # 当前生效的通道 id（"" = 未接真实支付）；供前端/自检展示，不参与业务判断。
        "pay_provider": _pay.describe()["enabled"],
    }


def create_order(user: dict, pack_id: str) -> dict:
    """按套餐下单，返回 pending 订单。**此处不发积分。**"""
    pack = _packs.get_pack(pack_id)
    if not pack:
        raise MemberError("套餐不存在", 404)
    order_id = f"{ORDER_ID_PREFIX}{uuid.uuid4().hex[:12]}"
    order = {
        "order_id": order_id,
        "user_id": user["user_id"],
        # 下单时把套餐信息**快照**进订单：套餐改名/调价后，历史订单仍然看得懂。
        "pack_id": pack["id"],
        "pack_name": pack["name"],
        "points": pack["points"],
        "amount_fen": pack["price_fen"],
        "status": ORDER_PENDING,
        "provider": "",
        "out_trade_no": order_id,   # 对接真实通道时用它做商户订单号
        "credit_log_id": "",
        "created_at": datetime.now().isoformat(),
        "paid_at": None,
    }
    store.insert_order(order)
    return order


def get_order(user: dict, order_id: str) -> dict:
    order = store.get_order(order_id)
    # 不区分「不存在」与「不是你的」——避免用订单号探测别人下过什么单。
    if not order or order.get("user_id") != user["user_id"]:
        raise MemberError("订单不存在", 404)
    return order


# ─── 订单过期（2026-10-15）──────────────────────────────────
#
# 为什么要「过期」这个概念：个人收款监听是**按金额匹配**的，同一面额在平台侧
# 同一时刻只能挂一张待付单。用户看到一堆"待支付"就会反复去点，点一次占一次；
# 过期状态让"这张单已经没用了"在界面上**看得见**，从而把用户劝去重新下单。
#
# ⚠️ 三条设计约束（别改）：
#   1. **expired 是展示态、不是终态**：入账路径（claim_order）必须同时接受
#      pending 与 expired，否则"踩着过期点付款"会变成钱到了积分不给。
#   2. **基准时间用 `pay_created_at`**（= 真正向平台出码的时刻）—— 平台侧的待付单元
#      就是那一刻产生的。订单刚建还没出码时回落 `created_at`（那时平台侧什么都没有，
#      纯展示判断）。
#   3. **阈值就是 `PAY_CHARGE_VALID_SECONDS`**，与前端承诺时长、复用窗口同源。
#      平台侧有效期与它不同值时，是**配置不一致**的问题（清单里有对齐步骤），
#      不在这里用两套阈值糊过去。
#
# 落库时机：`list_orders` 读列表时顺手 CAS 落库（惰性）。没有后台清理任务。

def order_expired(order: dict, now: Optional[datetime] = None) -> bool:
    """订单是否已过支付码有效期（只看时间，不看状态 —— 状态判定交给调用方）。"""
    base = order.get("pay_created_at") or order.get("created_at") or ""
    if not base:
        return False
    try:
        started = datetime.fromisoformat(base)
    except (TypeError, ValueError):
        return False
    age = ((now or datetime.now()) - started).total_seconds()
    return age >= PAY_CHARGE_VALID_SECONDS


def _sweep_expired(orders: list) -> list:
    """把 pending 且已过期的订单 CAS 落库成 expired，并就地改写返回的副本。

    抢不到（返回 None）说明并发下已被改成别的状态 —— 以**库里的最新状态**为准，
    不要用本地这份陈旧的 pending 覆盖它。
    """
    out = []
    for o in orders:
        if o.get("status") == ORDER_PENDING and order_expired(o):
            fresh = store.expire_order(o["order_id"])
            out.append(fresh or {**o, "status": ORDER_PENDING})
        else:
            out.append(o)
    return out


def list_orders(user: dict, offset: int = 0, limit: int = 20) -> dict:
    orders = [o for o in store.load_orders().values() if o.get("user_id") == user["user_id"]]
    orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    total = len(orders)
    page = _sweep_expired(orders[offset: offset + max(1, min(limit, 100))])
    return {"total": total, "offset": offset, "orders": page}


def _check_amount(order: dict, amount_fen: Optional[int], policy: str) -> None:
    """回调金额一致性校验（**策略化**）。

    入账金额一律以**订单快照**为准，这里只回答一个问题：「收到的钱够不够？」
    —— 不信回调传来的数字（否则被篡改的回调能改价）。

      - `strict`：必须**完全相等**。标准支付接口的常态（多付少付都说明对不上账）。
      - `ge`    ：**不低于**订单金额。个人收款监听会对金额做微调（同一张收款码
                  靠「金额唯一」区分多笔待付订单，通常向上加几分钱），严格相等会全拒。
      - `off`   ：不校验（只认 out_trade_no + 验签；最松，不建议）。

    策略由**通道自己声明**（`mod.AMOUNT_POLICY`），`PAY_AMOUNT_CHECK` 可全局覆盖。
    """
    if amount_fen is None or policy == "off":
        return
    got, want = int(amount_fen), int(order["amount_fen"])
    if policy == "ge":
        if got < want:
            raise MemberError(f"回调金额 {got} 分低于订单金额 {want} 分，拒绝入账", 400)
        return
    if got != want:
        raise MemberError(
            f"支付金额与订单不一致（订单 {want} 分，收到 {got} 分）", 400
        )


def settle_order(order_id: str, provider: str, amount_fen: Optional[int] = None,
                 out_trade_no: str = "", amount_policy: str = "strict") -> dict:
    """支付成功入账（**幂等**）。真实回调与模拟支付共用这一条路径。

    2026-10-12（Gate 2）改造后的时序 —— 这是"同一订单只入账一次"的关键：

      1. 读订单（不存在 → 404）、校验支付金额、确认用户还在；
      2. **CAS 抢单**：`pending → paid` 的**单文档原子更新**。抢不到 ⇒ 已入账过，
         直接返回 already_paid。并发回调（平台重推、用户连点）里**只有一个抢得到**
         —— 不再依赖进程级锁，多 worker / 多容器下同样成立；
      3. 抢到才发积分：`apply_point_delta(ref="order:{id}", kind="purchase")`。
         若该流水已存在（上次崩在"已入账、未改状态"之间），`(user_id, ref, kind)`
         的**唯一索引**会挡下并抛 `AlreadyApplied` —— 此时状态已由第 2 步补成 paid，
         只需回填流水号即可。（原先这段是 `_has_order_credit()` 扫全表判断的补偿分支，
         现已由 CAS + 唯一索引覆盖，且没有 TOCTOU 窗口。）

    ⚠️ 残余窗口（已知、明确接受）：第 2 步抢到状态后、第 3 步发出积分前进程被杀
       ⇒ 订单 `paid` 但无 `purchase` 流水。**不静默修** —— 由 `tools/reconcile.py`
       报出来人工处置（见 STORAGE_MIGRATION_PLAN.md §6 与 Gate 4）。
    """
    order = store.get_order(order_id)
    if not order:
        raise MemberError("订单不存在", 404)
    _check_amount(order, amount_fen, amount_policy)
    user_id = order["user_id"]
    if not store.get_user(user_id):
        raise MemberError("订单对应用户不存在", 404)

    claimed = store.claim_order(order_id, provider, out_trade_no or order.get("out_trade_no") or "")
    if claimed is None:
        cur = store.get_order(order_id) or order
        u = store.get_user(user_id) or {}
        return {"order": cur, "added": 0, "balance": u.get("points", 0), "already_paid": True}

    log_id = f"pl_{uuid.uuid4().hex[:12]}"
    try:
        balance = _apply_delta(
            {"user_id": user_id}, claimed["points"], "purchase",
            f"购买{claimed['pack_name']}（{claimed['points']} 积分）",
            ref=f"order:{order_id}", log_id=log_id,
        )
    except store.AlreadyApplied:
        # 流水已存在 ⇒ 上次崩在"已入账、未改状态"之间；状态这次已补成 paid
        store.mark_order_credited(order_id, "")
        cur = store.get_order(order_id) or claimed
        u = store.get_user(user_id) or {}
        return {"order": cur, "added": 0, "balance": u.get("points", 0), "already_paid": True}

    store.mark_order_credited(order_id, log_id)
    return {"order": {**claimed, "credit_log_id": log_id}, "added": claimed["points"],
            "balance": balance, "already_paid": False}


def settle_by_out_trade_no(out_trade_no: str, provider: str,
                           amount_fen: Optional[int] = None,
                           amount_policy: str = "strict") -> dict:
    """按商户订单号找单并入账（真实支付回调入口）。

    真实通道只回传 out_trade_no、不回传我们的内部 order_id，而 out_trade_no
    默认就等于 order_id（用户也可能在对接后自定义过它）—— 两种都试。
    2026-10-12（Gate 2）：找单走 `orders.out_trade_no` **索引**，不再扫全表。
    """
    target = (out_trade_no or "").strip()
    if not target:
        raise MemberError("缺少商户订单号", 400)
    order = store.find_order(target)
    if not order:
        raise MemberError("订单不存在", 404)
    return settle_order(order["order_id"], provider, amount_fen=amount_fen,
                        out_trade_no=target, amount_policy=amount_policy)


def mock_pay(user: dict, order_id: str) -> dict:
    """模拟一次「支付成功」——仅用于商城演示，等价于支付平台推了一条成功回调。

    ⚠️ 只允许支付**自己的**订单（get_order 已校验归属）；整体开关见 MOCK_PAY。
    """
    if not MOCK_PAY:
        raise MemberError("模拟支付已关闭", 403)
    order = get_order(user, order_id)   # 归属校验
    return settle_order(order["order_id"], provider="mock", amount_fen=order["amount_fen"])


# ─── 真实支付：出码（下单）─────────────────────────────────
#
# 职责边界：service 只负责「组装通道上下文 + 落库 + 翻译错误」，
# **不知道**任何通道字段（通道差异全在 app/membership/pay_*.py）。

# 同一面额被占用时的用户文案。**平台无关**（放在 service 而不是 pay_mpay），
# 因为它是业务结果、不是通道细节：谁来占用（自己的旧单 / 别人 / 平台历史单）
# 对用户都是同一句话。`pay_mpay` 把平台的同类错误也翻译成这一句。
SAME_AMOUNT_BUSY_MSG = "该面额当前有一笔待支付的订单，请约 5 分钟后重试"


def _charge_recent(order: dict) -> bool:
    """订单里已存的出码信息是否还在复用窗口内。"""
    created = order.get("pay_created_at") or ""
    if not created:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(created)).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= age < PAY_CHARGE_REUSE_SECONDS


def _same_amount_blocker(order: dict) -> Optional[dict]:
    """**全局**找一单会占住同一面额的订单；没有就返回 None。

    为什么要**全局**而不是只看当前用户：个人收款监听是平台**按金额**匹配的，
    别人占着同一面额时我们同样下不进去 —— 只看自己会漏报，用户还是会撞到
    「可用金额偏移已用尽」。所以口径必须与平台一致：**按金额，跨用户**。

    「会占住」的判据照抄平台的事实：**已经出过码**（`pay_created_at` 存在）
    且仍在有效期内。只建单、没出码的订单在平台侧**什么都不占** —— 用
    `created_at` 当判据会把"用户自己一堆没付的旧单"误判成冲突，反而把正常下单堵死。
    已支付/已过期的单自然不占位。
    """
    amount = order.get("amount_fen")
    if amount is None:
        return None
    for other in store.load_orders().values():
        if other.get("order_id") == order.get("order_id"):
            continue
        if other.get("status") != ORDER_PENDING:
            continue
        if other.get("amount_fen") != amount:
            continue
        if other.get("pay_created_at") and not order_expired(other):
            return other
    return None


async def create_charge_for_order(user: dict, order_id: str,
                                  clientip: str = "", device: str = "pc") -> dict:
    """为 pending 订单向**当前通道**下单（出码）；窗口内重复调用复用已有结果。

    返回 `{"order": <订单>, "pay": {pay_type, pay_info, trade_no, valid_seconds, reused}}`，
    前端按 `pay_type` 渲染：
        `qrcode`    → 把 pay_info 渲染成二维码
        `jump`      → 跳转到 pay_info
        `urlscheme` → 唤起小程序（移动端）

    `valid_seconds` 随每次出码下发（复用与新建都有）：前端据此显示「请在 N 分钟内
    完成支付」，**不再硬编码时长** —— 否则改了平台有效期，界面承诺就成了一句谎话
    （2026-10-15 前的 bug：前端写 30 分钟、mpay 只认 5 分钟）。

    为什么把出码信息**存进订单**并做复用窗口：各通道对「同一商户订单号重复下单」
    的行为不一致（可能复用同一支付单，也可能报错）；而用户关掉二维码后又需要能重开
    —— 复用窗口同时满足这两点。

    ⚠️ 两道**同一面额**的闸门（2026-10-15）：
      ① 出码前**我们侧预检**：同一 `amount_fen` 已有"已出码且未过期"的待付单 ⇒ 直接
         回友好文案，不白跑一次平台往返。这是**主路径**（自己人能拦就自己拦）。
      ② 平台侧兜底：mpay 按金额匹配，别人（或历史）占着同一面额时会回
         「可用金额偏移已用尽（code=0）」—— 由 `pay_mpay` 翻译成同一句文案。
    不同面额互不影响（"非同一面额的可继续下单支付"）。
    """
    order = get_order(user, order_id)          # 归属校验（不存在/不是你的 → 404）
    if order.get("status") == ORDER_EXPIRED:
        raise MemberError("该订单已过期（支付码有效期已过），请重新下单", 409)
    if order.get("status") != ORDER_PENDING:
        raise MemberError("该订单已支付或已关闭，无需再次支付", 409)
    # 可能"看起来 pending，其实早过期了"（列表没被打开过就不会落库）—— 这里补一次 CAS
    if order_expired(order):
        store.expire_order(order_id)
        raise MemberError("该订单已过期（支付码有效期已过），请重新下单", 409)

    if order.get("pay_info") and _charge_recent(order):
        return {"order": order, "pay": {"pay_type": order.get("pay_type", ""),
                                        "pay_info": order.get("pay_info", ""),
                                        "trade_no": order.get("trade_no", ""),
                                        "valid_seconds": PAY_CHARGE_VALID_SECONDS,
                                        "reused": True}}

    if _same_amount_blocker(order):
        raise MemberError(SAME_AMOUNT_BUSY_MSG, 409)

    mod = _pay.default_provider()
    if mod is None:
        raise MemberError(_pay.provider_problem() or "支付通道未接入", 503)
    # 形态检查（缺项 + pid 之类）—— **在发请求之前**拦住，省一次白跑的往返。
    # 注意用 charge_problem() 而不是 configured()：后者只管"配没配"，
    # 「pid 填成商户编号」这种"配了但形态错"要靠它才拦得住。
    if (problem := mod.charge_problem()):
        raise MemberError(problem, 503)
    if (base_problem := pay_public_base_problem()):
        raise MemberError(base_problem, 503)

    ctx = {
        "notify_url": notify_url_for(mod.PROVIDER_ID),
        "return_url": PAY_RETURN_URL or None,
        "clientip": clientip,
        "device": device,
    }
    try:
        charge = await mod.create_charge(order, ctx)
    except _pay.PayChannelError as e:
        raise MemberError(f"下单失败：{e.message}", e.code)

    updated = store.update_order_fields(order_id, {
        "pay_type": charge.get("pay_type", ""),
        "pay_info": charge.get("pay_info", ""),
        "trade_no": charge.get("trade_no", ""),
        "pay_created_at": datetime.now().isoformat(),
    }) or order
    return {"order": updated, "pay": {**charge, "valid_seconds": PAY_CHARGE_VALID_SECONDS,
                                      "reused": False}}


# ─── 合成任务扣费钩子（MEMBER_ENFORCE / 记账模式） ──────────

def count_billable_chars(lines: list) -> int:
    """计费字数：剥掉行内 [pause:N] 后按字符数累加（与前端预估同口径）。

    [pause:N] 只控制合成时插多长静音，不是要念的正文；计入字数会让用户为
    停顿标记付费，且秒数写得越长字面越长、付费越多。MonoEditor 编辑器里
    显示的字数一直是这样剥掉后统计的，这里对齐它。
    """
    chars = 0
    for line in lines or []:
        if isinstance(line, dict):
            text = line.get("text") or ""
        elif isinstance(line, str):
            text = line
        else:
            continue
        chars += len(PAUSE_TOKEN_RE.sub("", text).strip())
    return chars


def estimate_task_cost(lines: list) -> int:
    """按实际字数线性计费，向上取整到 1 积分，最低 MIN_CHARGE。

        cost = max(MIN_CHARGE, ceil(chars * POINTS_PER_1000_CHARS / 1000))

    口径变更史（2026-09-28）：
      旧式 = ceil(chars / 1000) * 单价 —— 整千向上取整，不足 1000 字也按一
      整千收费（500 字与 1000 字同价），且 1000→1001 字直接翻倍。对短文本
      是 10 倍量级的溢价，也把「签到送 5 积分」这类小额赠送变成了摆设。
      改为线性后 **1000 字及以上的价格与旧口径完全一致**（收入中性），只有
      短任务变便宜。

    为什么保留 MIN_CHARGE：成本主要由**合成段数**（引擎调用次数）决定，而
    不随字数线性 —— 一段 20 字要 1 次调用，20 字拆成 4 段要 4 次，第三方
    引擎每次还有上传/导入固定开销。地板用来兜住这类短而碎的任务。

    POINTS_PER_1000_CHARS=0 表示不计费；空文本返回 0。
    """
    if POINTS_PER_1000_CHARS <= 0:
        return 0
    chars = count_billable_chars(lines)
    if chars <= 0:
        return 0
    # 整数运算向上取整，避开浮点：ceil(chars * P / 1000)
    exact = -(-chars * POINTS_PER_1000_CHARS // 1000)
    return max(MIN_CHARGE, exact)


def charge_for_task(user: dict, lines: list, task_id: str) -> dict:
    """提交任务时预扣积分。成本为 0 时返回 {"balance": 当前, "log_id": ""}。

    返回 log_id 供退款幂等：每个扣费记录只允许对应一笔退款。

    2026-10-12（Gate 2）：**余额预检已删除**。"先查余额、再扣费"之间有窗口
    （并发时会给出错误的"积分不足"）；现在余额是否够是 `$inc` 的**查询条件**，
    判断与扣减在同一次原子往返里完成。
    """
    cost = estimate_task_cost(lines)
    if cost <= 0:
        return {"balance": user.get("points", 0), "log_id": ""}
    log_id = f"pl_{uuid.uuid4().hex[:12]}"
    new_balance = _apply_delta(user, -cost, "spend", f"合成扣费 {task_id}",
                               ref=task_id, log_id=log_id)
    return {"balance": new_balance, "log_id": log_id}


def refund_task_charge(user_id: str, task_id: str, amount: int, charge_log_id: str = "") -> None:
    """任务失败/取消/中断时退还预扣积分。

    幂等：以 `f"{task_id}:{charge_log_id}"` 作为退款 ref —— 同一笔扣费只退一次。

    2026-10-12（Gate 2）：幂等判据从"先查流水、再退"（有 TOCTOU 窗口，并发两个
    退款请求可同时通过检查）换成**依赖唯一索引** —— 重复退款会撞
    `(user_id, ref, kind=refund)` 唯一索引并抛 AlreadyApplied。更硬，且没有窗口。
    """
    if amount <= 0:
        return
    if not store.get_user(user_id):
        return
    refund_ref = f"{task_id}:{charge_log_id or 'legacy'}"
    try:
        _apply_delta({"user_id": user_id}, amount, "refund", f"任务退款 {task_id}", ref=refund_ref)
    except store.AlreadyApplied:
        return   # 已经退过（或正在退）—— 静默返回，这是本函数的幂等语义


def rebalance_task_charge(user: dict, lines: list, task_id: str,
                          prev_charged: int, prev_log_id: str) -> dict:
    """编辑已扣费任务后按新字数多退少补。返回新扣费额与 log_id。"""
    new_cost = estimate_task_cost(lines)
    diff = new_cost - prev_charged
    log_id = prev_log_id
    if diff > 0:
        # 余额够不够由存储层的原子条件判断（不再"先查后写"）
        _apply_delta(user, -diff, "spend", f"任务改稿补扣 {task_id}", ref=task_id)
    elif diff < 0:
        # ⚠️ 这里的 ref **刻意留空**：改稿退差**不是幂等操作**（每改一次稿就该退一次），
        # 若填 task_id 会撞 `(user_id, ref, kind=refund)` 唯一索引，第二次改稿就退不了。
        # 任务号已写在 reason 里，信息没有丢。
        if store.get_user(user["user_id"]):
            _apply_delta(user, -diff, "refund", f"任务改稿退差 {task_id}", ref="")
    return {"charged": new_cost, "log_id": log_id}


# ─── 管理端 ─────────────────────────────────────────────────

def admin_grant_points(username_or_id: str, delta: int, reason: str) -> dict:
    user = store.find_user(username_or_id)
    if not user:
        raise MemberError("用户不存在", 404)
    if delta == 0:
        raise MemberError("变动值不能为 0")
    balance = _apply_delta(user, delta, "grant", reason or "管理员调整")
    return {"user": public_user(store.get_user(user["user_id"]) or user), "balance": balance}


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
    user = store.find_user(username_or_id)
    if not user:
        raise MemberError("用户不存在", 404)
    user = store.update_user_fields(user["user_id"], {"disabled": disabled}) or user
    if disabled:
        revoke_user_sessions(user["user_id"])  # 禁用即踢下线
    return public_user(user)


def admin_list_codes() -> list[dict]:
    codes = sorted(store.load_redeem_codes().values(), key=lambda c: c.get("created_at") or "", reverse=True)
    now = time.time()
    for c in codes:
        c["expired"] = bool(c.get("expires_ts") and c["expires_ts"] < now)
    return codes
