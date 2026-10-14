"""支付通道层的**错误契约**与共享常量。

**为什么单独一个模块**：`pay.py`（门面）要 import 各通道实现，而通道实现又要抛
/引用这些 —— 若定义在门面里就会循环 import。所以契约单独放这里，门面与各通道
都从本模块取。（与 `store_contract.py` 同一套路。）

**分层约定**：通道层只给**结构化原因**，不产出面向用户的文案（那是 service 层的事）。
"""

from __future__ import annotations

# 视为「支付成功」的状态字面量。各通道在 normalize() 里把自家状态映射到这里。
#   ePay V1/V2 → "TRADE_SUCCESS"；通用/聚合 → "paid" / "success"
PAID_STATUSES = frozenset({"paid", "success", "trade_success", "TRADE_SUCCESS"})

# 回调金额校验策略（通道声明默认值，`PAY_AMOUNT_CHECK` 可全局覆盖）：
#   strict —— 必须与订单金额**完全相等**（标准支付接口的常态）
#   ge     —— 不低于订单金额（个人收款监听会对金额做微调，向上加几分钱区分订单）
#   off    —— 不校验金额（只认 out_trade_no + 验签；最松，不建议）
AMOUNT_POLICIES = ("strict", "ge", "off")


class PayChannelError(Exception):
    """通道侧失败：网络不通 / 下游报错 / 配置缺失 / 报文非法。

    由通道模块抛出，service 层 catch 后翻译成面向用户的 `MemberError`。
    `code` 沿用 HTTP 语义（默认 502 = 上游异常），让 service 可以直接透传。
    """

    def __init__(self, message: str, code: int = 502):
        super().__init__(message)
        self.message = message
        self.code = code
