"""会员模块（G2）：注册登录 / 用户资料 / 积分体系 / 优惠码 / 签到 / 扣费钩子。

用法：
    from .membership import router          # FastAPI 路由（挂到 app 即可）
    from .membership import service         # 业务层（扣费/退款钩子、CLI 复用）
"""

from __future__ import annotations

from .routes import get_current_user, get_optional_user, router

__all__ = ["router", "get_current_user", "get_optional_user"]
