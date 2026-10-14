"""通用支付通道：ASCII 升序拼串 + HMAC-SHA256。

## 定位：通配通道（`PROVIDER_ID = "*"`）

门面 `pay.get_provider()` 先按 id 精确匹配，**匹配不到才落到这里**。用途有二：

1. **历史兼容** —— 项目在接真实通道前就定义了这套通用形态，
   `tests/test_points_purchase.py` 与旧 `pay.py` 的契约依赖它；
2. 将来接「形态恰好一致」的聚合支付 / ePay 上游时可直接复用。

⚠️ 它与 `pay_mpay.py` 是**两条互不引用的独立通道**。共享的只有
   `pay_contract.py`（常量/异常）与 `service.settle_order`（入账）。

## 配置

`PAY_NOTIFY_SECRET`（常量定义在 `service.py`，这里**延迟读取** —— 便于测试临时覆盖，
也避免「门面 ↔ service」循环 import）。未配置 ⇒ `configured()` 为 False ⇒ 回调端点
503（**绝不提供"没配密钥就当验签通过"的降级**：那等于把白拿积分的入口敞开）。

## 签名

按 key 升序拼 `k=v&...`，用 `PAY_NOTIFY_SECRET` 做 HMAC-SHA256，取十六进制小写。
这是很多聚合支付的通用形态。**真实通道的签名串拼法各不相同**（有的裸接 KEY、
有的用 MD5、有的要 raw body），所以接通道请照 `pay_mpay.py` 另开模块，不要往这里塞 if。
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from .pay_contract import PayChannelError

PROVIDER_ID = "*"          # 通配：精确匹配不到时落到本通道
TITLE = "通用 HMAC-SHA256（占位 / 聚合支付）"
NOTIFY_METHODS = ("POST",)
NOTIFY_REPLY = "json"      # 老契约：回 JSON（不是纯文本）
NOTIFY_REPLY_FAIL = "json"
AMOUNT_POLICY = "strict"


def _secret() -> str:
    """签名密钥。**每次现读** service 的常量 —— 测试改 `service.PAY_NOTIFY_SECRET`
    即生效，不需要重启进程（与改造前 `routes` 现读的行为一致）。"""
    from . import service   # 延迟 import：避免「门面 ↔ service」循环
    return getattr(service, "PAY_NOTIFY_SECRET", "") or ""


def configured() -> bool:
    return bool(_secret())


def config_problem() -> str:
    return "" if _secret() else "通用通道未配置 PAY_NOTIFY_SECRET（回调端点整体拒收）"


def charge_problem() -> str:
    """**出码**前的形态检查。通用通道没有额外的形态约束（它本就未实现下单）。"""
    return config_problem()


def sign_params(params: dict, secret: str) -> str:
    """通用验签串：按 key 升序拼 `k=v`，用 `&` 连接，HMAC-SHA256 十六进制小写。

    跳过空值与 sign/sign_type 自身（后者是签名字段，不参与签名）。
    参数值一律 `str()` 化后再拼 —— 所以 int/float 形态的取值（如 `amount_fen=1000`）
    与字符串形态算出的签名一致。
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


def verify(payload: dict) -> bool:
    """校验回调签名。密钥为空时恒 False（调用方应先拦在 503）。"""
    secret = _secret()
    if not secret:
        return False
    given = str(payload.get("sign") or "")
    if not given:
        return False
    return hmac.compare_digest(sign_params(payload, secret), given.lower())


def normalize(payload: dict) -> Optional[dict]:
    """把通道报文归一化成内部事实。

    返回 `{"out_trade_no", "amount_fen", "trade_status", "trade_no"}`；
    报文缺少必需字段时返回 None（调用方按「报文不合法」拒收）。
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
        "trade_no": str(payload.get("trade_no") or "").strip(),
    }


async def create_charge(order: dict, ctx: dict) -> dict:
    """通用通道**没有「下单」概念** —— 它只是报文归一化的占位形态。

    真实通道要在这里实现「调下游接口 / 生成收款码」。见 `pay_mpay.py` 的实现。
    """
    raise PayChannelError(
        "通用通道未实现下单（它是报文归一化的占位通道）；"
        "请在具体通道模块里实现 create_charge()，并把 PAY_PROVIDER 指过去",
        501,
    )
