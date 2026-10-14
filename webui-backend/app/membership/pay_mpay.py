"""mpay_v2（码支付）通道 —— ePay **V1** 协议 / MD5 签名。

对接方案与决策见 `PAY_MPAY_PLAN.md`；接口原文取自部署站点的内置文档页
（`https://mpay.lizigege.com/docs`）。协议要点速查：

| 环节 | 形态 |
|---|---|
| 下单 | `POST {base}/mapi.php`（form-urlencoded），成功判据 **`code == 1`** |
| 回调 | **GET + query**（本通道也容忍 POST，便于手工重放） |
| 成功状态 | `trade_status == "TRADE_SUCCESS"` |
| 应答 | **纯文本 `success`**（不是 JSON）；失败回 `fail` |
| 签名 | 排除 sign/sign_type/空值 → ASCII 升序拼 `k=v&...` → **末尾裸接 KEY** → md5 小写 |
| 金额 | 通道侧单位是**元**（字符串，2 位小数）；本模块换算成内部的**分** |

## 与「标准支付」的关系（重要）

mpay 是**个人收款监听**方案，不是标准支付接口：它没有平台级回调规范，靠监听个人
收款码的到账流水来确认支付。所以它被隔离成一个**独立通道模块**：
将来接标准支付（微信/支付宝官方）时，新开 `pay_wechat_official.py` 一类模块、
把 `.env` 的 `PAY_PROVIDER` 指过去即可 —— `service` / `routes` 一行不用改。

⚠️ 因此：**本模块之外的任何代码都不许出现 `MPAY_*` 或 ePay 字段名**。

## ⚠️ 金额会被微调（决定 AMOUNT_POLICY）

个人收款监听靠「金额唯一」区分同一张收款码上的多个待付订单，因此**回调金额可能
大于下单金额**。故本通道声明 `AMOUNT_POLICY = "ge"`（不低于即放行）。
（用户 2026-10-14 明确确认「回调金额会被微调」。）

## ⚠️ pid 必须是**纯数字**（决定 charge_problem 单列）

ePay V1 契约把 `pid` 标为 **Int**，而 mpay 后台还会另给一个**商户编号**
（形如 `M20261014120400214497`）—— 二者长得都像「账号」，极易填错。
实测把商户编号填进 `MPAY_PID` 会被 mpay 在**第一层**就拒掉：

```
{"code":0,"msg":"商户ID 必须是整数。"}
```

mpay 的错误阶梯（排查有用）：非整数 → `商户API凭证未开通`（**不区分**「商户不存在」
和「没开 API」）→ `签名验证失败`（**拿到这句就说明 pid 对了**）。

⇒ 形态检查放在 `charge_problem()`（**只管出码**），刻意**不**并进 `configured()`：
回调只依赖 `key`，pid 写错期间到账的钱不该连回调都收不到。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Optional

from .pay_contract import PayChannelError

PROVIDER_ID = "mpay"
TITLE = "码支付 mpay_v2（ePay V1 / MD5）"
NOTIFY_METHODS = ("GET", "POST")
NOTIFY_REPLY = "success"        # 纯文本应答（不是 JSON）
NOTIFY_REPLY_FAIL = "fail"
AMOUNT_POLICY = "ge"            # 金额会被微调 ⇒ 只校验「不低于」

# 下单请求超时（秒）。下单是用户点击后同步等待的，不能拖太久。
_CHARGE_TIMEOUT = 15.0
_CONNECT_TIMEOUT = 8.0


def _conf() -> dict:
    """通道配置。**每次现读** service 的常量 —— 测试改 `service.MPAY_*` 即生效，
    也避免「通道 ↔ service」循环 import。"""
    from . import service
    return {
        "base_url": (getattr(service, "MPAY_BASE_URL", "") or "").rstrip("/"),
        "pid": str(getattr(service, "MPAY_PID", "") or "").strip(),
        "key": getattr(service, "MPAY_KEY", "") or "",
        "type": (getattr(service, "MPAY_TYPE", "") or "wxpay").strip() or "wxpay",
    }


# `MPAY_PID` 必须是**纯数字**（ePay V1 契约标注为 Int）。
# ⚠️ 用 `re` 而不是 `str.isdigit()` —— 后者对全角数字（"１２３".isdigit() is True）
#    和上标数字都返回 True，而 mpay 只认 ASCII 数字。
_PID_RE = re.compile(r"^[0-9]+$")


def configured() -> bool:
    """通道**基础配置**是否齐全（`base_url` / `pid` / `key` 三项都在）。

    ⚠️ 刻意**不含** pid 形态检查 —— 回调只依赖 `key`：pid 写错期间到账的钱
    不该连回调都被拦掉（那会变成"钱到了、记不上"）。形态检查单列
    `charge_problem()`，只作用于出码与自检。
    """
    c = _conf()
    return bool(c["base_url"] and c["pid"] and c["key"])


def config_problem() -> str:
    """报错质量：直接指出**缺哪一项环境变量**，而不是笼统说"未配置"。"""
    c = _conf()
    missing = [name for name in ("base_url", "pid", "key") if not c[name]]
    if not missing:
        return ""
    envs = "、".join(f"MPAY_{name.upper()}" for name in missing)
    return f"mpay 通道缺少配置：{envs}（见 .env.example 的「真实支付通道」段）"


def _pid_problem(pid: str) -> str:
    """`MPAY_PID` 的形态检查（空串 = 没问题）。"""
    if not pid or _PID_RE.match(pid):
        return ""
    hint = ""
    if pid[:1].isalpha():
        hint = ("；你填的像是**商户编号**（形如 M20261014120400214497），"
                "那不是 API 的 pid —— 去 mpay 后台「API 信息」页找纯数字的那个")
    return f"MPAY_PID={pid!r} 不是纯数字（ePay V1 契约要求 Int）{hint}"


def charge_problem() -> str:
    """**出码**前的形态检查 = `config_problem()` + pid 形态。

    单列的原因：pid 形态只影响出码，且应当在**发请求之前**就拦住
    —— 否则白跑一次往返、拿回一句 `商户ID 必须是整数。`。
    """
    return config_problem() or _pid_problem(_conf()["pid"])


def sign_v1(params: dict, key: str) -> str:
    """ePay V1 签名：`md5(ASCII 升序拼 "k=v&k2=v2" + KEY)`，十六进制小写。

    ⚠️ 三处与「通用 HMAC 形态」（`pay_generic.sign_params`）**不同，别互相照抄**：

    1. 末尾是**裸接** KEY —— 没有 `&key=`、也没有任何分隔符；
    2. 算法是 **MD5**，不是 HMAC-SHA256；
    3. 参数值**不做 URL 编码**（直接拼原始值）。
    """
    items = []
    for k in sorted(params):
        if k in ("sign", "sign_type"):
            continue
        v = params[k]
        if v is None or v == "":
            continue
        items.append(f"{k}={v}")
    raw = "&".join(items) + key
    return hashlib.md5(raw.encode("utf-8")).hexdigest()   # noqa: S324（ePay 协议固定 MD5）


def verify(payload: dict) -> bool:
    """校验 ePay V1 回调签名（密钥为空恒 False，调用方应先拦在 503）。"""
    key = _conf()["key"]
    if not key:
        return False
    given = str(payload.get("sign") or "").lower()
    if not given:
        return False
    return hmac.compare_digest(sign_v1(payload, key), given)


def normalize(payload: dict) -> Optional[dict]:
    """把 ePay V1 回调报文归一化成内部事实。

    ⚠️ 通道侧金额字段是 **`money`（单位：元，字符串）**，这里换算成内部的**分**。
    """
    out_trade_no = str(payload.get("out_trade_no") or "").strip()
    if not out_trade_no:
        return None
    money = payload.get("money")
    if money is None or str(money).strip() == "":
        return None
    try:
        amount_fen = int(round(float(str(money).strip()) * 100))
    except (TypeError, ValueError):
        return None
    return {
        "out_trade_no": out_trade_no,
        "amount_fen": amount_fen,
        "trade_status": str(payload.get("trade_status") or "").strip(),
        "trade_no": str(payload.get("trade_no") or "").strip(),
    }


async def _post_charge(url: str, params: dict, client=None):
    """POST 下单请求。`client` 可注入 —— 测试用 `httpx.MockTransport` 打桩，
    避免打真实支付网关（与 `tests/test_engine_302ai.py` 的做法一致）。"""
    import httpx
    if client is not None:
        try:
            return await client.post(url, data=params)
        except httpx.HTTPError as e:
            raise PayChannelError(f"mpay 下单请求失败：{e!r}") from e
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_CHARGE_TIMEOUT, connect=_CONNECT_TIMEOUT),
            # ⚠️ 显式忽略环境里的 HTTP_PROXY/NO_PROXY：下单必须直连支付网关。
            #    若将来服务器出网**必须**走代理，这里要改成按配置决定。
            trust_env=False,
        ) as cli:
            return await cli.post(url, data=params)
    except httpx.HTTPError as e:
        raise PayChannelError(f"mpay 下单请求失败：{e!r}") from e


async def create_charge(order: dict, ctx: dict, client=None) -> dict:
    """向 mpay 下单（出码），返回支付呈现信息。

    `ctx` 由 service 组装（通道**不读**全局配置里的 URL，保持可测试）：
        notify_url  必填，公网可达的异步回调地址
        return_url  选填，付款后页面跳转地址
        clientip    选填，下单用户 IP（ePay V1 标为必填，缺省给 0.0.0.0）
        device      选填，pc / mobile / wechat …（缺省 pc）

    返回 `{"trade_no", "pay_type", "pay_info"}`：
        pay_type ∈ {"qrcode", "jump", "urlscheme"}，三者只会返回其一；
        pay_info 是**渲染所需的内容**（qrcode=二维码内容 / jump=跳转链接 /
        urlscheme=小程序跳转链接）。

    失败抛 `PayChannelError`（网络不通 / 下游 code != 1 / 返回不可解析）。
    """
    c = _conf()
    problem = charge_problem()
    if problem:
        raise PayChannelError(problem, 503)

    params = {
        "pid": c["pid"],
        "type": c["type"],
        "out_trade_no": order.get("out_trade_no") or order["order_id"],
        "notify_url": ctx.get("notify_url") or "",
        "name": (order.get("pack_name") or "积分充值")[:60],
        "money": f"{int(order['amount_fen']) / 100:.2f}",   # 分 → 元字符串（2 位小数）
        "clientip": ctx.get("clientip") or "0.0.0.0",
        "device": ctx.get("device") or "pc",
        "sign_type": "MD5",
    }
    if ctx.get("return_url"):
        params["return_url"] = ctx["return_url"]
    params["sign"] = sign_v1(params, c["key"])

    resp = await _post_charge(c["base_url"] + "/mapi.php", params, client)

    if resp.status_code != 200:
        raise PayChannelError(f"mpay 下单返回 HTTP {resp.status_code}：{resp.text[:200]!r}")
    try:
        js = resp.json()
    except ValueError as e:
        raise PayChannelError(f"mpay 下单返回非 JSON：{resp.text[:200]!r}") from e
    if not isinstance(js, dict):
        raise PayChannelError(f"mpay 下单返回格式异常：{js!r}")
    # ⚠️ ePay V1 是 code == 1 才算成功（V2 才是 0），别写反。
    if int(js.get("code", 0)) != 1:
        raise PayChannelError(f"mpay 下单失败：{js.get('msg') or js}（code={js.get('code')}）")

    for field, pay_type in (("qrcode", "qrcode"), ("payurl", "jump"), ("urlscheme", "urlscheme")):
        val = js.get(field)
        if val:
            return {
                "trade_no": str(js.get("trade_no") or ""),
                "pay_type": pay_type,
                "pay_info": str(val),
            }
    raise PayChannelError(f"mpay 下单成功但未返回可支付内容（qrcode/payurl/urlscheme 皆空）：{js!r}")
