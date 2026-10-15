"""存储层的**错误契约**与共享常量。

**为什么单独一个模块**：`store.py`（门面）要 import 后端实现，而后端实现又要抛
这些异常 —— 若把异常定义在门面里就会循环 import。所以契约单独放这里，
门面与各后端都从本模块取。

**分层约定**：存储层只给**结构化原因**，不产出面向用户的文案（那是 service 层的事）。
service 层 catch 这些异常后翻译成 `MemberError`。
"""

from __future__ import annotations

# ─── 幂等规则（资金安全的第一道防线）────────────────────────────
#
# 需要**幂等保证**的流水类型 —— 只有这两类会被"重复触发"：
#   refund   ：任务失败/取消/中断都可能重复调用退款（重试、重复投递）
#   purchase ：支付平台回调天然会重试
# 其余 kind 各有自己的防重手段：
#   checkin 靠 {user_id, date} 唯一索引、redeem 靠占坑（used_by 校验）、
#   spend 靠任务流程（task["points_charged"]）、earn/grant 靠调用方。
#
# ⚠️ **刻意不把 spend 纳入**：`routes/queue.py:retry_queue_task` 会**复用同一个
#    task_id** 重新提交，若唯一索引覆盖 spend，重试的扣费会被自己挡住。
IDEMPOTENT_KINDS = ("refund", "purchase")

# 流水的全部 kind（与 service 层注释一致，供对账/自检引用）
POINT_LOG_KINDS = ("earn", "spend", "redeem", "checkin", "grant", "refund", "purchase")

# 订单状态（service 与存储层共用，避免两处各写一份字面量）
#   pending = 待支付；paid = 已支付并已入账；expired = 已过支付码有效期
#
# ⚠️ **expired 是「展示态」，不是终态** —— 这是资金安全红线，别改成终态：
#   个人收款监听（mpay）的识别有效期与我们的有效期是**两侧各自的配置**，只要
#   不一致或网络延迟几秒，就会出现「我们判过期、用户其实付成功」。若 `claim_order`
#   只认 pending，那条回调就抢不到单 ⇒ **钱到了、积分不给**。
#   所以入账路径（claim_order）接受的来源状态是 pending **和** expired；
#   expired 只用于「列表展示 + 拦掉重复出码」，从前端看去就是「不能再去支付了」。
# 2026-10-15 之前这里写的是「刻意不做 closed/expired」—— 现在要做，但上面那条
# 约束是新增的、比"不做"更重要；没有任何后台清理任务，过期判定在 service 读时顺手落库。
ORDER_PENDING = "pending"
ORDER_PAID = "paid"
ORDER_EXPIRED = "expired"


# ─── 异常 ───────────────────────────────────────────────────

class StoreError(Exception):
    """存储层错误基类：service 层 catch 后翻译成面向用户的 MemberError。"""


class UserNotFound(StoreError):
    def __init__(self, user_id: str):
        super().__init__(f"用户不存在：{user_id}")
        self.user_id = user_id


class InsufficientPoints(StoreError):
    """余额不足 —— 由**原子判定**产出（不是"先查余额再扣"）。"""

    def __init__(self, balance: int, need: int):
        super().__init__(f"积分不足：当前 {balance}，需要 {need}")
        self.balance = balance
        self.need = need


class AlreadyApplied(StoreError):
    """幂等命中：同一 `(user_id, ref, kind)` 的流水已存在，本次**不重复入账**。"""

    def __init__(self, ref: str = "", kind: str = ""):
        super().__init__(f"该笔积分变动已入账过（ref={ref!r}, kind={kind!r}）")
        self.ref = ref
        self.kind = kind


class DuplicateUser(StoreError):
    """建号冲突：用户名或邮箱已被占用。`field` ∈ {"username", "email"}。"""

    def __init__(self, field: str, value: str = ""):
        super().__init__(f"{field} 已被占用：{value}")
        self.field = field
        self.value = value


class CodeUnavailable(StoreError):
    """优惠码不可用。`reason` 是**枚举**，翻译成用户文案是 service 层的事。"""

    NOT_FOUND = "not_found"
    DISABLED = "disabled"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"        # 用完（used_count 达 max_uses）
    ALREADY_USED = "already_used"  # 该用户已经兑过

    def __init__(self, reason: str, code: str = ""):
        super().__init__(f"优惠码不可用（{reason}）：{code}")
        self.reason = reason
        self.code = code
