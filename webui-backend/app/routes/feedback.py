"""用户反馈 / 举报入口（`POST /api/feedback`）。

为什么不挂在 membership 里
--------------------------
反馈与积分账本无关：不进 `users` / `point_logs`，也不参与任何结算与对账。
独立集合 `feedback` 只是一个「收件箱」，运营/站长直接查库：

    db.feedback.find().sort({created_ts: -1}).limit(50)

鉴权
----
**必须登录**（入口在顶栏用户菜单里，那个菜单只在登录后出现）。
带 `user_id` 才能定位「是哪位用户的账号/订单出了问题」；匿名反馈只是一条
无法跟进的孤证，所以这里不给匿名入口。

存储
----
**只写 MongoDB**（库 `MONGO_DB`，集合 `feedback`，`_id = fb_<12hex>`）。

刻意不做「Mongo 不可用时落到本地文件」的兜底：反馈里常带联系方式与复现步骤，
分两处存会让「查一遍」变成「查两处 + 还得合并」。Mongo 不可用时直接回 503
并说明原因 —— **宁可让用户重试，也不静默丢件**。
（生产环境 `MEMBER_STORE=mongo`，这个库本来就在。）

字段设计（前端 `components/FeedbackDialog.tsx` 必须同口径）
----------------------------------------------------------
用户填 3 项，其余由服务端补：
    用户填：  category(类型) / content(详细描述) / contact(联系方式，选填)
    前端带：  page_url / user_agent（"在哪个页面出的问题"，用户说不清时最有用）
    服务端补：id / user_id / username / nickname / created_at / created_ts / ip / status
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..config import logger
from ..db import DBUnavailable, get_mongo_db
from ..membership.routes import get_current_user

router = APIRouter()

# 类型 → 中文标签。**只在这里定义一次**：前端同名常量与本表必须一致，
# 后台按 category 归类时不必再猜英文键是什么意思。
CATEGORIES: dict[str, str] = {
    "bug": "功能异常",
    "voice": "音色与音质",
    "points": "积分与订单",
    "content": "内容问题（版权/不当）",
    "idea": "功能建议",
    "other": "其他",
}

CONTENT_MIN = 5      # 少于 5 个字基本等于"没用"，挡在服务端（前端也挡一次）
CONTENT_MAX = 1000
CONTACT_MAX = 100

STATUS_NEW = "new"   # 只是初值；运营改不改都由人工决定，代码不自动流转


def _col():
    """`feedback` 集合句柄（懒取，连不上时由调用方翻译成 503）。"""
    return get_mongo_db()["feedback"]


def _now_iso() -> str:
    """无时区字面量，与本项目其它时间戳一致（容器已设 TZ=Asia/Shanghai）。"""
    return datetime.now().isoformat()


def _client_ip(request: Request) -> str:
    """真实客户端 IP（反代下优先 X-Forwarded-For 的第一段）。"""
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip") or ""
    if real:
        return real.strip()
    return request.client.host if request.client else ""


class FeedbackBody(BaseModel):
    category: str = "other"
    content: str
    contact: str = ""
    page_url: str = ""
    user_agent: str = ""


@router.post("/api/feedback")
def submit_feedback(
    body: FeedbackBody,
    request: Request,
    user: dict = Depends(get_current_user),
):
    category = (body.category or "").strip()
    if category not in CATEGORIES:
        raise HTTPException(
            400,
            f"反馈类型无效：{category or '（空）'}；可选 {', '.join(CATEGORIES)}",
        )

    content = (body.content or "").strip()
    if len(content) < CONTENT_MIN:
        raise HTTPException(400, f"请把问题写清楚一些（至少 {CONTENT_MIN} 个字）")
    if len(content) > CONTENT_MAX:
        raise HTTPException(400, f"描述过长（上限 {CONTENT_MAX} 字），请精简后重试")

    contact = (body.contact or "").strip()
    if len(contact) > CONTACT_MAX:
        raise HTTPException(400, f"联系方式过长（上限 {CONTACT_MAX} 字）")

    fid = f"fb_{uuid.uuid4().hex[:12]}"
    doc = {
        "_id": fid,
        "user_id": user.get("user_id", ""),
        "username": user.get("username", ""),
        "nickname": user.get("nickname", ""),
        "category": category,
        "category_label": CATEGORIES[category],
        "content": content,
        "contact": contact,
        # 用户说不清"哪个页面"时，这两个字段就是唯一线索；长度截断防止塞大对象
        "page_url": (body.page_url or "").strip()[:500],
        "user_agent": (body.user_agent or "").strip()[:300],
        "ip": _client_ip(request),
        "status": STATUS_NEW,
        "created_at": _now_iso(),
        # 另存 BSON Date：`created_at` 是无时区字符串，只能排序/展示，
        # 做「按天聚合」这类时间查询必须用真正的时间类型。
        "created_ts": datetime.now(),
    }

    try:
        _col().insert_one(doc)
    except DBUnavailable as e:
        logger.error("反馈无法入库（数据库不可用）：user=%s category=%s err=%s",
                     doc["user_id"], category, e)
        raise HTTPException(
            503,
            "反馈通道暂时不可用（数据库未就绪），请稍后再试；"
            "急需回复可发邮件到站长邮箱",
        ) from e
    except Exception as e:  # pymongo 各类连接/写失败 —— 一律翻译成 503，别报 500
        logger.error("反馈入库失败：user=%s category=%s err=%s", doc["user_id"], category, e)
        raise HTTPException(503, "反馈提交失败（服务暂时不可用），请稍后再试") from e

    logger.info("收到反馈：id=%s user=%s category=%s len=%d",
                fid, doc["user_id"], category, len(content))
    return {"ok": True, "id": fid, "message": "反馈已收到，感谢你帮我们改进"}
