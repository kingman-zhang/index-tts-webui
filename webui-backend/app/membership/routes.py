"""会员模块 HTTP 路由：auth / users / points / admin。

鉴权约定：`Authorization: Bearer <token>` 或 `X-Auth-Token: <token>`。
合成任务扣费为「可选鉴权」：不带 token 的请求行为与旧版完全一致。
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import service
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
    """可选鉴权：无 token 或 token 失效都返回 None，不报错。"""
    return service.resolve_token(_extract_token(authorization, x_auth_token))


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
    raise HTTPException(e.code, e.message)


# ─── 请求体 ─────────────────────────────────────────────────

class RegisterBody(BaseModel):
    username: str
    password: str
    nickname: str = ""


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

@router.post("/api/auth/register")
def auth_register(body: RegisterBody):
    try:
        return service.register(body.username, body.password, body.nickname)
    except MemberError as e:
        _err(e)


@router.post("/api/auth/login")
def auth_login(body: LoginBody):
    try:
        return service.login(body.username, body.password)
    except MemberError as e:
        _err(e)

@router.post("/api/auth/email-code")
def auth_email_code(body: EmailCodeBody):
    """发送邮箱注册验证码（需服务端配置 SMTP_*；未配置返回 503）。"""
    try:
        return service.request_email_code(body.email)
    except MemberError as e:
        _err(e)


@router.post("/api/auth/register-email")
def auth_register_email(body: RegisterEmailBody):
    """邮箱 + 验证码 + 密码注册，成功即登录。"""
    try:
        return service.register_email(body.email, body.code, body.password, body.nickname)
    except MemberError as e:
        _err(e)


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
        _err(e)


@router.post("/api/users/me/password")
def change_password(body: PasswordBody, user: dict = Depends(get_current_user)):
    try:
        service.change_password(user, body.old_password, body.new_password)
        return {"ok": True, "message": "密码已修改，请重新登录"}
    except MemberError as e:
        _err(e)


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
        _err(e)


@router.get("/api/points/checkin")
def checkin_status(user: dict = Depends(get_current_user)):
    return service.checkin_status(user)


@router.post("/api/points/checkin")
def checkin(user: dict = Depends(get_current_user)):
    try:
        return service.checkin(user)
    except MemberError as e:
        _err(e)


# ─── 管理端（X-Admin-Token）────────────────────────────────

@router.post("/api/admin/members/codes")
def admin_create_codes(body: CodesBody, _: None = Depends(require_admin)):
    try:
        codes = service.create_redeem_codes(body.count, body.points, body.max_uses, body.expires_days, body.note)
        return {"codes": codes, "points": body.points, "max_uses": body.max_uses}
    except MemberError as e:
        _err(e)


@router.get("/api/admin/members/codes")
def admin_codes(_: None = Depends(require_admin)):
    return {"codes": service.admin_list_codes()}


@router.post("/api/admin/members/points/grant")
def admin_grant(body: GrantBody, _: None = Depends(require_admin)):
    try:
        return service.admin_grant_points(body.user, body.delta, body.reason)
    except MemberError as e:
        _err(e)


@router.get("/api/admin/members/users")
def admin_users(query: str = "", _: None = Depends(require_admin)):
    return {"users": service.admin_list_users(query)}


@router.post("/api/admin/members/disable")
def admin_disable(body: DisableBody, _: None = Depends(require_admin)):
    try:
        return {"user": service.admin_set_disabled(body.user, body.disabled)}
    except MemberError as e:
        _err(e)
