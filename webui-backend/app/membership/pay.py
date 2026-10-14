"""支付通道**门面**：通道注册表 + 按 provider 分发 + 契约校验。

## 为什么要门面（而不是各通道各写各的）

`service` / `routes` 只认「通道」这个抽象，**不认识任何具体通道的字段**：
验签怎么做、回调是 GET 还是 POST、应答体是 `success` 还是 JSON、金额能不能有误差
—— 全部由通道模块自己声明。于是：

- 换 / 加通道 = 新增一个通道模块 + 一份配置，`service` / `routes` **不动**；
- mpay（个人收款监听，**不是标准方案**）与将来接的标准支付（微信/支付宝官方）
  **天然解耦**，谁也不牵连谁 —— 这是用户 2026-10-14 明确要求的设计目标。

## 通道契约（`_REQUIRED_API`，**import 期 fail fast**）

| 名字 | 类型 | 含义 |
|---|---|---|
| `PROVIDER_ID` | str | 通道 id（回调 URL 最后一段）；`"*"` = 通配兜底 |
| `TITLE` | str | 人读名字（日志 / 自检） |
| `NOTIFY_METHODS` | tuple[str] | 回调接受的 HTTP 方法 |
| `NOTIFY_REPLY` | str | 成功应答体；`"json"` 特指"回 JSON"（老契约） |
| `NOTIFY_REPLY_FAIL` | str | 失败应答体（`NOTIFY_REPLY="json"` 时无意义） |
| `AMOUNT_POLICY` | str | 回调金额校验默认策略（strict/ge/off） |
| `configured()` | -> bool | 配置是否齐全（**只判"在不在"**，供回调/就绪判断用） |
| `config_problem()` | -> str | 未配好时**缺什么**（报错质量：直接指出下一步） |
| `charge_problem()` | -> str | **出码前**的形态检查（在 `config_problem()` 之上加形态约束）；空串 = 可出码 |
| `verify(payload)` | -> bool | 验签 |
| `normalize(payload)` | -> dict 或 None | 通道报文 → 内部事实 |
| `create_charge(order, ctx)` | async -> dict | 下单 / 出码 |

⚠️ `configured()` 与 `charge_problem()` 的**分工**（照抄会给回调埋坑）：
`configured()` 只回答「能不能跟这个通道说话」，**不含**形态类检查 ——
因为回调与出码依赖的东西不一样（回调验签只依赖 `key`）。
形态错了要拦住出码、但**不能连回调一起拦**，否则「配错期间到账的钱」会收不到回调。

## 当前已注册

- `pay_mpay.py` —— `mpay`（码支付 ePay V1 / MD5），个人收款监听
- `pay_generic.py` —— `*`（通用 HMAC-SHA256），历史/聚合支付占位

⚠️ 本文件**只做注册与转发**，不含任何通道的业务逻辑。
"""

from __future__ import annotations

import hmac
from typing import Optional

# 契约与共享常量：单独放 pay_contract，避免"门面 ↔ 通道"循环 import。
# 这里 re-export 一份，让调用方只 import 门面就够了。
from .pay_contract import (  # noqa: F401
    AMOUNT_POLICIES,
    PAID_STATUSES,
    PayChannelError,
)

from . import pay_generic
from . import pay_mpay

# `sign_params` 是通用通道的签名算法；门面 re-export 一份，
# 让 `pay.sign_params(...)`（历史调用点与 tests/test_points_purchase.py）继续可用。
from .pay_generic import sign_params  # noqa: F401


# ─── 契约 ───────────────────────────────────────────────────

_REQUIRED_API = (
    "PROVIDER_ID", "TITLE", "NOTIFY_METHODS",
    "NOTIFY_REPLY", "NOTIFY_REPLY_FAIL", "AMOUNT_POLICY",
    "configured", "config_problem", "charge_problem",
    "verify", "normalize", "create_charge",
)

_PROVIDERS: dict = {}    # PROVIDER_ID -> module
_WILDCARD = None         # PROVIDER_ID == "*" 的通道（唯一）


def _register(mod) -> None:
    """注册一个通道；契约不完整 / id 重复 / 通配冲突 ⇒ **import 期即失败**。"""
    missing = [n for n in _REQUIRED_API if not hasattr(mod, n)]
    if missing:
        raise RuntimeError(
            f"支付通道 {mod.__name__!r} 未实现以下契约：{missing}；"
            f"完整契约见 app/membership/pay.py 的 _REQUIRED_API。"
        )
    if mod.PROVIDER_ID == "*":
        global _WILDCARD
        if _WILDCARD is not None:
            raise RuntimeError(f"通配通道（PROVIDER_ID='*'）只能有一个，已有 {_WILDCARD.__name__!r}")
        _WILDCARD = mod
        return
    if mod.PROVIDER_ID in _PROVIDERS:
        raise RuntimeError(f"支付通道 id 重复：{mod.PROVIDER_ID!r}")
    _PROVIDERS[mod.PROVIDER_ID] = mod


_register(pay_generic)
_register(pay_mpay)


# ─── 查表 ───────────────────────────────────────────────────

def get_provider(provider_id: str):
    """按 id 取通道：先**精确匹配**，再落到通配通道；都没有则 None。

    ⚠️ 通配通道（`*`）让「任意 provider 名」都能被通用通道处理 —— 这是历史契约
    （接真实通道前，`/api/pay/notify/任意名` 都用同一密钥验签）。
    精确注册的 id 优先级**高于**通配，所以 `/notify/mpay` 一定走 mpay。
    """
    return _PROVIDERS.get((provider_id or "").strip()) or _WILDCARD


def default_provider():
    """当前生效的通道（下单用）。由 `.env` 的 `PAY_PROVIDER` 决定。

    未配置 → None；配了但 id 未注册 → 也是 None，但 `provider_problem()` 会说清楚
    （别静默当作"未接入"，那会让排查时找不到北）。
    """
    from . import service
    pid = (getattr(service, "PAY_PROVIDER", "") or "").strip()
    return _PROVIDERS.get(pid) if pid else None


def provider_problem() -> str:
    """当前通道配置的健康状况（自检 / 前端提示用）。空串 = 一切正常。

    「缺配置」优先报（否则「pid 填了商户编号」会把「MPAY_KEY 没填」盖掉）；
    两者都有则**一起报**（一次看全，免得改一个再跑一次）。
    """
    from . import service
    pid = (getattr(service, "PAY_PROVIDER", "") or "").strip()
    if not pid:
        return "未配置 PAY_PROVIDER（积分商城暂无真实支付通道，只能模拟支付）"
    mod = _PROVIDERS.get(pid)
    if mod is None:
        known = "、".join(sorted(_PROVIDERS)) or "(无)"
        return f"PAY_PROVIDER={pid!r} 不是已注册的通道；可选：{known}"
    return mod.config_problem() or mod.charge_problem()


def describe() -> dict:
    """自检快照（`/api/version` 用）。"""
    from . import service
    pid = (getattr(service, "PAY_PROVIDER", "") or "").strip()
    mod = _PROVIDERS.get(pid) if pid else None
    problem = provider_problem()
    # 回调地址是**通道无关**的一层：通道全配好了、地址填错照样收不到回调
    #（且 mpay 不校验 notify_url、照样回成功 ⇒ 「二维码能出、付了钱、积分不动」的
    # 静默失败）。所以并进同一个 problem，让自检一句话说清。
    base_problem = service.pay_public_base_problem()
    if base_problem:
        problem = f"{problem}；{base_problem}" if problem else base_problem
    return {
        "enabled": pid or "",
        "ready": bool(mod and mod.configured() and not mod.charge_problem()
                      and not base_problem),
        "problem": problem,
        "known": sorted(_PROVIDERS),
        # 实际拼出来的回调地址（取值域自检：直接看它，比对着 .env 猜快）
        "notify_url": service.notify_url_for(pid) if pid else "",
    }


# ─── 分发（路由层用）────────────────────────────────────────

def reply_spec(mod) -> dict:
    """通道的回调应答形态 —— 路由层据它决定回纯文本还是 JSON。

    `{"kind": "json"}`（老契约）或 `{"kind": "text", "ok": ..., "fail": ...}`。
    """
    if mod.NOTIFY_REPLY == "json":
        return {"kind": "json"}
    return {"kind": "text", "ok": mod.NOTIFY_REPLY, "fail": mod.NOTIFY_REPLY_FAIL}


def resolve_amount_policy(mod) -> str:
    """金额校验策略：环境变量 `PAY_AMOUNT_CHECK` **覆盖**通道默认值。

    ⚠️ 覆盖是给运维留的口子（排查 / 临时放宽），正常应由通道自己声明。
    """
    from . import service
    override = (getattr(service, "PAY_AMOUNT_CHECK", "") or "").strip()
    if not override:
        return mod.AMOUNT_POLICY
    if override not in AMOUNT_POLICIES:
        raise RuntimeError(
            f"PAY_AMOUNT_CHECK={override!r} 非法；可选：{'、'.join(AMOUNT_POLICIES)}"
        )
    return override


# ─── 历史兼容包装（老调用点与 tests 依赖，勿删）─────────────

def verify_notify(payload: dict, secret: str = "") -> bool:
    """**兼容包装**：老签名 `verify_notify(payload, secret)`（显式传密钥的通用形态）。

    新代码请直接用通道的 `verify()` —— 它自己知道该用哪个密钥。
    """
    if not secret:
        return False
    given = str(payload.get("sign") or "")
    if not given:
        return False
    return hmac.compare_digest(pay_generic.sign_params(payload, secret), given.lower())


def normalize_notify(provider: str, payload: dict) -> Optional[dict]:
    """**兼容包装**：老签名 `normalize_notify(provider, payload)`。新代码用 `mod.normalize()`。"""
    mod = get_provider(provider)
    return mod.normalize(payload) if mod is not None else None
