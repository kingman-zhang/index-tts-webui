"""支付回调契约（**占位实现**，用于把「支付」这件事从业务里隔离出来）。

## 为什么单独一个文件

真实支付通道还没选型（用户侧只能走个人收款，候选是聚合支付/平台代收）。
但「下单 → 回调 → 入账」这条链路的**形状**与谁收钱无关：
任何通道最终都要给出「哪个订单、付了多少钱、付成功了吗」三个事实。

所以这里定义一个**归一化契约**：`normalize_notify()` 把通道各自的报文
翻译成 `{out_trade_no, amount_fen, trade_status}`；`verify_notify()` 负责验签。
将来接入真实通道，**只改这个文件**，service / routes 一行不动。

## 当前实现

- 通用签名：对参数按 key 升序拼 `k=v&...`，用 `PAY_NOTIFY_SECRET` 做
  HMAC-SHA256，取十六进制小写。这是很多聚合支付的通用形态，也是自建
  通道最容易实现的形态。
- ⚠️ **真实通道的签名串拼法各不相同**（有的带 appid、有的要 raw body、
  有的用 MD5 + 大写）。接哪个通道，就在这里加一个 provider 分支，
  **不要**在 service 层加 if。

## 安全边界（务必保留）

- 回调**不做 Bearer 鉴权**（支付平台不会带我们的 token），安全性完全依赖验签。
- 因此：`PAY_NOTIFY_SECRET` 未配置时，回调端点**一律拒收**（503），
  绝不能"没配密钥就当验签通过"——那等于把「凭 order_id 白拿积分」的公网入口敞开。
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

# 视为「支付成功」的状态字面量。真实通道各有各的写法，在 normalize_notify 里映射。
PAID_STATUSES = {"paid", "success", "trade_success", "TRADE_SUCCESS"}


def sign_params(params: dict, secret: str) -> str:
    """通用验签串：按 key 升序拼 `k=v` 用 `&` 连接，HMAC-SHA256 十六进制小写。

    跳过空值与 sign/sign_type 自身（后者是签名字段，不参与签名）。
    """
    items = []
    for k in sorted(params):
        if k in ("sign", "sign_type"):
            continue
        v = params[k]
        if v is None or v == "":
            continue
        items.append(f"{k}={v}")
    payload = "&".join(items).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_notify(payload: dict, secret: str) -> bool:
    """校验回调签名。secret 为空时恒 False（调用方应先拦在 503）。"""
    if not secret:
        return False
    given = str(payload.get("sign") or "")
    if not given:
        return False
    return hmac.compare_digest(sign_params(payload, secret), given.lower())


def normalize_notify(provider: str, payload: dict) -> Optional[dict]:
    """把通道报文归一化成内部事实。

    返回 `{"out_trade_no", "amount_fen", "trade_status"}`；
    报文缺少必需字段时返回 None（调用方按「报文不合法」拒收）。

    ⚠️ 目前只有一个通用形态（字段名与上面通用签名配套）。接入具体通道时，
    在这里按 provider 分支解析它的原始报文 —— 这是**唯一**允许出现
    通道差异的地方。
    """
    out_trade_no = str(payload.get("out_trade_no") or "").strip()
    if not out_trade_no:
        return None
    raw_amount = payload.get("amount_fen")
    if raw_amount is None:
        # 多数通道给的是「元」字符串（如 "50.00"）；也接受它，换算成分。
        yuan = payload.get("amount")
        if yuan is None:
            return None
        try:
            amount_fen = int(round(float(str(yuan)) * 100))
        except (TypeError, ValueError):
            return None
    else:
        try:
            amount_fen = int(raw_amount)
        except (TypeError, ValueError):
            return None
    return {
        "out_trade_no": out_trade_no,
        "amount_fen": amount_fen,
        "trade_status": str(payload.get("trade_status") or "").strip(),
    }
