"""会员模块 HTTP 路由：auth / users / points / orders / pay-notify / admin。

鉴权约定：`Authorization: Bearer <token>` 或 `X-Auth-Token: <token>`。
合成任务扣费为「可选鉴权」：不带 token 的请求行为与旧版完全一致。
**支付回调例外**：`/api/pay/notify/{provider}` 不带 Bearer（支付平台不会带
我们的 token），安全性完全依赖签名校验，详见 `pay.py`。
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from ..config import logger
from . import service
from . import pay as pay_mod
from .service import MemberError

router = APIRouter()

# ─── 依赖 ───────────────────────────────────────────────────

def _extract_token(authorization: Optional[str], x_auth_token: Optional[str]) -> Optional[str]:
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()
    if x_auth_token:
        return x_auth_token.strip()
    return None


def get_optional_user(
    authorization: Optional[str] = Header(None),
    x_auth_token: Optional[str] = Header(None),
) -> Optional[dict]:
    """可选鉴权：无 token 或 token 失效都返回 None，不报错。

    ⚠️ **唯一例外**（2026-10-15）：**带了 token 但会话存储不可用** ⇒ 抛 503，
    绝不返回 None。原因是这个 None 会在"要求登录"的端点被翻译成 **401**，
    而前端 `authFetch` 见 401 就清掉本地 token —— 一次 Redis 抖动 = 被登出
    （本地实测：4 次 `Timeout reading from socket`，每次都要重新登录；
    服务端会话其实还活着、TTL 30 天）。回 503 则前端不清 token，只表现为
    这一次请求失败。

    "不带 token" 的情形**保持原样返回 None**：合成提交等公开链路不能因为
    会话存储故障被一起打挂 —— 这是原设计刻意要保住的性质。
    """
    token = _extract_token(authorization, x_auth_token)
    try:
        return service.resolve_token(token)
    except service.SessionStoreUnavailable as e:
        if not token:
            # 本地会话存储故障不该牵连匿名请求（原语义）
            return None
        logger.warning("会话存储不可用，本请求回 503（不清前端登录态）：%s", e)
        raise HTTPException(503, "会话服务暂时不可用，请稍后重试") from e


def get_current_user(user: Optional[dict] = Depends(get_optional_user)) -> dict:
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def require_admin(x_admin_token: Optional[str] = Header(None)) -> None:
    """管理接口守卫：未配置 MEMBER_ADMIN_TOKEN 时整体禁用。"""
    expected = service.ADMIN_TOKEN
    if not expected:
        raise HTTPException(403, "管理接口未启用（未配置 MEMBER_ADMIN_TOKEN）")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(401, "管理令牌无效")


def _err(e: MemberError):
    """业务错误 → HTTP 应答。

    统一**返回** JSONResponse 而不是 raise HTTPException，为的是能多带一个机器可读的
    `code`（见 `MemberError.err_code`）—— 前端要按它决定「弹只有确认按钮的告知弹窗」
    还是「toast」，**不该去匹配中文文案**。输出形态与 HTTPException 等价
    （`{"detail": ...}`、状态码相同），只是多一个可选字段；老调用方读 detail
    完全不受影响。

    ⚠️ 调用点必须写成 `return _err(e)`。写成裸 `_err(e)` 会返回 None，FastAPI 会把
    None 序列化成 `200 null` —— 一个静默的假成功，排查起来很难看。
    """
    body: dict = {"detail": e.message}
    if e.err_code:
        body["code"] = e.err_code
    return JSONResponse(status_code=e.code, content=body)


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（反代下优先 X-Forwarded-For 的第一段）。"""
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip") or ""
    if real:
        return real.strip()
    return request.client.host if request.client else ""


async def _collect_payload(request: Request) -> dict:
    """从 query / JSON / 表单**三种载体**里收集回调参数。

    ⚠️ 必须按 content-type 分支：Starlette 的 `request.json()` 与 `request.form()`
    都会消费 body，两个都调会抛 "Stream consumed"。
    之所以支持三种载体：不同通道的回调形态不同 —— ePay V1 是 **GET + query**，
    通用/聚合形态多是 POST + JSON（接受哪些方法由通道的 `NOTIFY_METHODS` 声明）。
    """
    payload: dict = {k: v for k, v in request.query_params.items()}
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "回调报文不是合法 JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "回调报文格式不正确（应为 JSON 对象）")
        # 保留标量值（签名算法会把它们 str() 化）；嵌套结构无法参与签名，丢弃。
        payload.update({k: v for k, v in body.items() if not isinstance(v, (dict, list))})
    elif "form-urlencoded" in ctype or "multipart/form-data" in ctype:
        try:
            form = await request.form()
        except Exception as e:
            raise HTTPException(400, f"回调表单解析失败：{e}")
        for k, v in form.items():
            if isinstance(v, str):
                payload[k] = v
    return payload


# ─── 请求体 ─────────────────────────────────────────────────

class LoginBody(BaseModel):
    username: str  # 用户名或邮箱
    password: str


class EmailCodeBody(BaseModel):
    email: str


class RegisterEmailBody(BaseModel):
    email: str
    code: str
    password: str
    nickname: str = ""


class ProfileBody(BaseModel):
    nickname: Optional[str] = None
    bio: Optional[str] = None


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


class RedeemBody(BaseModel):
    code: str


class OrderBody(BaseModel):
    pack_id: str


class CodesBody(BaseModel):
    count: int = 1
    points: int
    max_uses: int = 1
    expires_days: int = 0
    note: str = ""


class GrantBody(BaseModel):
    user: str
    delta: int
    reason: str = ""


class DisableBody(BaseModel):
    user: str
    disabled: bool


# ─── auth ───────────────────────────────────────────────────

@router.post("/api/auth/login")
def auth_login(body: LoginBody):
    try:
        return service.login(body.username, body.password)
    except MemberError as e:
        return _err(e)

@router.post("/api/auth/email-code")
def auth_email_code(body: EmailCodeBody):
    """发送邮箱注册验证码（需服务端配置 SMTP_*；未配置返回 503）。"""
    try:
        return service.request_email_code(body.email)
    except MemberError as e:
        return _err(e)


@router.post("/api/auth/register-email")
def auth_register_email(body: RegisterEmailBody):
    """邮箱 + 验证码 + 密码注册，成功即登录。"""
    try:
        return service.register_email(body.email, body.code, body.password, body.nickname)
    except MemberError as e:
        return _err(e)


@router.post("/api/auth/logout")
def auth_logout(authorization: Optional[str] = Header(None), x_auth_token: Optional[str] = Header(None)):
    token = _extract_token(authorization, x_auth_token)
    if token:
        service.logout(token)
    return {"ok": True}


@router.get("/api/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    return {"user": user}


# ─── 用户资料 ───────────────────────────────────────────────

@router.patch("/api/users/me")
def update_me(body: ProfileBody, user: dict = Depends(get_current_user)):
    try:
        return {"user": service.update_profile(user, body.nickname, body.bio)}
    except MemberError as e:
        return _err(e)


@router.post("/api/users/me/password")
def change_password(body: PasswordBody, user: dict = Depends(get_current_user)):
    try:
        service.change_password(user, body.old_password, body.new_password)
        return {"ok": True, "message": "密码已修改，请重新登录"}
    except MemberError as e:
        return _err(e)


# ─── 积分 ───────────────────────────────────────────────────

@router.get("/api/points/balance")
def points_balance(user: dict = Depends(get_current_user)):
    return {"points": user.get("points", 0)}


@router.get("/api/points/logs")
def points_logs(offset: int = 0, limit: int = 20, user: dict = Depends(get_current_user)):
    return service.list_point_logs(user["user_id"], max(0, offset), limit)


@router.post("/api/points/redeem")
def points_redeem(body: RedeemBody, user: dict = Depends(get_current_user)):
    try:
        return service.redeem(user, body.code)
    except MemberError as e:
        return _err(e)


@router.get("/api/points/checkin")
def checkin_status(user: dict = Depends(get_current_user)):
    return service.checkin_status(user)


@router.post("/api/points/checkin")
def checkin(user: dict = Depends(get_current_user)):
    try:
        return service.checkin(user)
    except MemberError as e:
        return _err(e)


# ─── 积分购买（套餐 / 订单 / 模拟支付）──────────────────────

@router.get("/api/points/packs")
def points_packs():
    """积分商城套餐清单（无需登录 —— 未登录也能看到价格，登录后才能下单）。"""
    return service.list_packs()


@router.post("/api/points/orders")
def create_order(body: OrderBody, user: dict = Depends(get_current_user)):
    """按套餐下单，返回 pending 订单（**此时不发积分**）。"""
    try:
        return {"order": service.create_order(user, body.pack_id)}
    except MemberError as e:
        return _err(e)


@router.get("/api/points/orders")
def list_orders(offset: int = 0, limit: int = 20, user: dict = Depends(get_current_user)):
    return service.list_orders(user, max(0, offset), limit)


@router.get("/api/points/orders/{order_id}")
def get_order(order_id: str, user: dict = Depends(get_current_user)):
    """单条订单查询 —— 供前端**支付后轮询**判断这一单是否已入账。

    为什么不复用 `GET /api/points/orders`：轮询要的是「**这一单**好了没」，列表
    接口要先扫全量、排序、分页（单文件存储下是 O(全部订单)），且并发下单时
    还得靠 `limit=1` 猜哪条是刚才那单。单条查询语义明确、代价恒定。

    只读，无副作用：真正的入账只发生在回调（`settle_order`）里 —— 轮询**不**
    会也不能把订单改成已支付，所以前端再怎么轮询都造不出假账。
    """
    try:
        return {"order": service.get_order(user, order_id)}
    except MemberError as e:
        return _err(e)


@router.post("/api/points/orders/{order_id}/mock-pay")
def mock_pay(order_id: str, user: dict = Depends(get_current_user)):
    """模拟支付成功（商城演示用）。

    ⚠️ 与真实回调走**同一条入账路径**（`service.settle_order`），因此行为一致、
    同样幂等。整体开关 `MEMBER_MOCK_PAY`，接入真实支付后置 0。
    """
    try:
        return service.mock_pay(user, order_id)
    except MemberError as e:
        return _err(e)


@router.post("/api/points/orders/{order_id}/pay")
async def order_pay(order_id: str, request: Request,
                    user: dict = Depends(get_current_user)):
    """为 pending 订单向**当前支付通道**下单（出码）。

    返回 `{order, pay:{pay_type, pay_info, trade_no, reused}}`，前端按 `pay_type` 渲染：
        `qrcode` → 把 pay_info 渲染成二维码；`jump` → 跳转 pay_info；
        `urlscheme` → 唤起小程序（移动端）。
    ⚠️ 复用窗口内的重复调用会**复用**已有出码结果（不重复向下游下单）—— 见 service 注释。
    """
    try:
        return await service.create_charge_for_order(
            user, order_id, clientip=_client_ip(request))
    except MemberError as e:
        return _err(e)


# ─── 支付回调（无 Bearer；靠通道验签）───────────────────────
#
# 端点对**所有通道**通用：验签算法、接受的 HTTP 方法、报文载体、应答形态、
# 金额校验策略，全部由 `app/membership/pay_*.py` 的通道模块声明 —— 这里只做通用适配。
# 接新通道 = 新增一个通道模块 + 改 .env 的 PAY_PROVIDER，本文件不用动。
#
# 安全边界：回调**不带 Bearer**（支付平台不会带我们的 token），安全性完全依赖验签；
# 通道未配置密钥时一律 503（**不提供"没配密钥就当验签通过"的降级**）。

@router.api_route("/api/pay/notify/{provider}", methods=["GET", "POST"])
async def pay_notify(provider: str, request: Request):
    mod = pay_mod.get_provider(provider)
    if mod is None:
        raise HTTPException(404, f"未注册的支付通道：{provider}")
    if request.method not in mod.NOTIFY_METHODS:
        raise HTTPException(405, f"{mod.TITLE} 的回调不接受 {request.method}")

    spec = pay_mod.reply_spec(mod)

    def _out(ok: bool, status_code: int, body: dict):
        """按通道声明的应答形态回包（纯文本 `success` or JSON）。"""
        if spec["kind"] == "json":
            return JSONResponse(body, status_code=status_code)
        return PlainTextResponse(spec["ok"] if ok else spec["fail"], status_code=status_code)

    if not mod.configured():
        # ⚠️ 通道没配好时**不能放行** —— 那等于把「凭 order_id 白拿积分」的公网入口敞开。
        return _out(False, 503, {"ok": False, "error": mod.config_problem() or "通道未配置"})

    payload = await _collect_payload(request)
    if not mod.verify(payload):
        return _out(False, 401, {"ok": False, "error": "签名校验失败"})
    fact = mod.normalize(payload)
    if not fact:
        return _out(False, 400, {"ok": False, "error": "回调报文缺少必需字段"})

    if fact["trade_status"] not in pay_mod.PAID_STATUSES:
        # 非成功状态（已关闭 / 退款）：确认收到但**不**入账，回成功体避免无意义重推。
        return _out(True, 200, {"code": 0, "message": "ignored",
                                "trade_status": fact["trade_status"]})

    try:
        result = service.settle_by_out_trade_no(
            fact["out_trade_no"], provider,
            amount_fen=fact["amount_fen"],
            amount_policy=pay_mod.resolve_amount_policy(mod),
        )
    except MemberError as e:
        # 订单不存在 / 金额不符：回**失败体**（让通道重推 + 通道后台可见异常），并留痕。
        # 这类异常意味着「钱可能到了但没入账」，必须能在日志里查到。
        logger.error("支付回调入账失败：provider=%s out_trade_no=%s code=%s msg=%s",
                     provider, fact["out_trade_no"], e.code, e.message)
        return _out(False, e.code, {"ok": False, "error": e.message})

    logger.info("支付入账：provider=%s out_trade_no=%s amount_fen=%s already_paid=%s",
                provider, fact["out_trade_no"], fact["amount_fen"], result["already_paid"])
    return _out(True, 200, {"code": 0, "message": "ok",
                            "already_paid": result["already_paid"],
                            "balance": result["balance"]})


# ─── 管理端（X-Admin-Token）────────────────────────────────

@router.post("/api/admin/members/codes")
def admin_create_codes(body: CodesBody, _: None = Depends(require_admin)):
    try:
        codes = service.create_redeem_codes(body.count, body.points, body.max_uses, body.expires_days, body.note)
        return {"codes": codes, "points": body.points, "max_uses": body.max_uses}
    except MemberError as e:
        return _err(e)


@router.get("/api/admin/members/codes")
def admin_codes(_: None = Depends(require_admin)):
    return {"codes": service.admin_list_codes()}


@router.post("/api/admin/members/points/grant")
def admin_grant(body: GrantBody, _: None = Depends(require_admin)):
    try:
        return service.admin_grant_points(body.user, body.delta, body.reason)
    except MemberError as e:
        return _err(e)


@router.get("/api/admin/members/users")
def admin_users(query: str = "", _: None = Depends(require_admin)):
    return {"users": service.admin_list_users(query)}


@router.post("/api/admin/members/disable")
def admin_disable(body: DisableBody, _: None = Depends(require_admin)):
    try:
        return {"user": service.admin_set_disabled(body.user, body.disabled)}
    except MemberError as e:
        return _err(e)
