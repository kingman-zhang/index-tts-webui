#!/usr/bin/env python3
"""mpay_v2（ePay V1）支付通道单测：通道契约 / 签名 / 验签 / 归一化 / 出码 / 回调。

覆盖的**不可退让点**（改这块照这张单子核）：
  ① 签名规则 = ePay V1（MD5、末尾**裸接** KEY、排除 sign/sign_type/空值）
     —— 用**硬编码向量**锁死，防止实现和参照实现一起写错
  ② 回调是 **GET + query**，且必须回**纯文本 `success`**（不是 JSON）
  ③ 「金额会被微调」⇒ 策略 `ge`：**多付放行、少付拒绝**，入账一律按订单快照
  ④ 幂等：同一订单重复回调只入账一次
  ⑤ 通道未配置 → 503（**不放行**；不提供"没配密钥就当验签通过"的降级）
  ⑥ 出码信息落库 + 窗口内复用（不重复向下游下单）
  ⑦ 通道可插拔：provider 未知名落到通配通道（历史契约），mpay 精确优先

用法：~/.workbuddy/binaries/python/envs/default/bin/python tests/test_pay_mpay.py
隔离：临时 DATA_DIR，不污染真实数据。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

TMP = tempfile.mkdtemp(prefix="wb-mpay-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_REG_BONUS"] = "500"
os.environ["MEMBER_CHECKIN_BONUS"] = "0"
os.environ.pop("PAY_NOTIFY_SECRET", None)
# 接入真实支付后必须关掉模拟支付（见 .env.example 的告警）
os.environ["MEMBER_MOCK_PAY"] = "0"
# ── 真实支付通道：mpay ──
os.environ["PAY_PROVIDER"] = "mpay"
os.environ["PAY_PUBLIC_BASE_URL"] = "https://api.example.com"
os.environ["PAY_RETURN_URL"] = "https://example.com/account"
os.environ["PAY_CHARGE_VALID_SECONDS"] = "300"   # 支付码有效期（＝平台侧「识别有效期」，2026-10-15 统一 5 分钟）
os.environ["MPAY_BASE_URL"] = "https://mpay.test"
os.environ["MPAY_PID"] = "1001"
os.environ["MPAY_KEY"] = "testkey"
os.environ["MPAY_TYPE"] = "wxpay"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 默认值回归不得受真实 .env 干扰（同 test_points_purchase.py 的手法）
_path_exists = Path.exists
with patch.object(Path, "exists", lambda p: False if p.name == ".env" else _path_exists(p)):
    from app.main import app  # noqa: E402
from app.membership import pay, pay_generic, pay_mpay, service, store  # noqa: E402
from app.membership.pay_contract import PayChannelError  # noqa: E402

client = TestClient(app)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def balance(token: str) -> int:
    return client.get("/api/points/balance", headers=auth(token)).json()["points"]


def md5_ref(params: dict, key: str) -> str:
    """**独立**的 ePay V1 签名参照实现（故意与被测实现分开写）。

    与 pay_mpay.sign_v1 的区别只有实现路径（这里显式 list/sorted），
    规则同源；规则本身的正确性由下面的**硬编码向量**保证。
    """
    items = []
    for k in sorted(params):
        if k in ("sign", "sign_type"):
            continue
        v = params[k]
        if v is None or v == "":
            continue
        items.append(f"{k}={v}")
    return hashlib.md5(("&".join(items) + key).encode("utf-8")).hexdigest()


def new_order(token: str) -> dict:
    return client.post("/api/points/orders", json={"pack_id": "p1000"},
                       headers=auth(token)).json()["order"]


def notify_params(order: dict, money: str = "10.00", status: str = "TRADE_SUCCESS",
                  key: str = "testkey") -> dict:
    p = {
        "pid": "1001", "trade_no": "T20260001", "out_trade_no": order["out_trade_no"],
        "type": "wxpay", "name": order["pack_name"], "money": money, "trade_status": status,
    }
    p["sign"] = pay_mpay.sign_v1(p, key)
    return p


# ─── [1] 通道注册表 ─────────────────────────────────────────
print("[1] 通道注册表（可插拔）")
check("mpay 已注册且精确命中", pay.get_provider("mpay") is pay_mpay)
check("未知名落到通配通道", pay.get_provider("whatever") is pay_generic)
check("空 id 也落通配", pay.get_provider("") is pay_generic)
_desc = pay.describe()
check("当前通道 = mpay", _desc["enabled"] == "mpay", str(_desc))
check("通道就绪", _desc["ready"] is True, str(_desc))
check("已注册通道含 mpay", "mpay" in _desc["known"], str(_desc))
check("describe 无问题字符串", _desc["problem"] == "", _desc["problem"])
check("service.pay_channel_ready() 为真", service.pay_channel_ready() is True)

# ─── [2] 签名规则（硬编码向量）────────────────────────────────
print("[2] 签名规则（ePay V1 / MD5）")
_p = {"pid": "1001", "type": "wxpay", "out_trade_no": "T1", "money": "10.00", "sign_type": "MD5"}
check("签名 = 硬编码向量", pay_mpay.sign_v1(_p, "testkey") == "393f37ca013a02f82089660223ed7dc8",
      pay_mpay.sign_v1(_p, "testkey"))
check("与独立参照实现一致", pay_mpay.sign_v1(_p, "testkey") == md5_ref(_p, "testkey"))
_q = {"pid": "7", "type": "wxpay", "out_trade_no": "ord_x", "money": "0.01",
      "trade_status": "TRADE_SUCCESS", "sign_type": "MD5"}
check("sign_type 不参与签名", pay_mpay.sign_v1(_q, "k") == "ccfcbab2711c9c6ba686346bee291cdb",
      pay_mpay.sign_v1(_q, "k"))
check("空值不参与签名",
      pay_mpay.sign_v1({"a": "1", "b": ""}, "k") == md5_ref({"a": "1", "b": ""}, "k"))
check("sign 自身不参与签名",
      pay_mpay.sign_v1({"a": "1", "sign": "xxx"}, "k") == md5_ref({"a": "1"}, "k"))
check("KEY 是裸接（不是 &key=）",
      pay_mpay.sign_v1({"a": "1"}, "k") == hashlib.md5(b"a=1k").hexdigest())
check("★与通用 HMAC 形态确实不同（证明两条通道没串）",
      pay_mpay.sign_v1(_p, "testkey") != pay_generic.sign_params(_p, "testkey"))

# ─── [3] 验签 ───────────────────────────────────────────────
print("[3] 验签")
_np = {"pid": "1001", "trade_no": "T20260001", "out_trade_no": "ord_abc", "type": "wxpay",
       "name": "体验包", "money": "10.00", "trade_status": "TRADE_SUCCESS"}
_np["sign"] = pay_mpay.sign_v1(_np, "testkey")
check("正确签名通过", pay_mpay.verify(_np) is True)
check("含中文商品名的签名 = 硬编码向量", _np["sign"] == "8b4d591bb897074fe37b2a7420faafb5", _np["sign"])
check("签名大写也通过", pay_mpay.verify({**_np, "sign": _np["sign"].upper()}) is True)
check("篡改金额被拒", pay_mpay.verify({**_np, "money": "0.01"}) is False)
check("缺 sign 被拒", pay_mpay.verify({"out_trade_no": "x"}) is False)

# ─── [4] 归一化 ─────────────────────────────────────────────
print("[4] 归一化（money 元 → 分）")
_f = pay_mpay.normalize({"out_trade_no": "ord_1", "money": "10.00",
                         "trade_status": "TRADE_SUCCESS", "trade_no": "T1"})
check("10.00 元 → 1000 分", _f["amount_fen"] == 1000, str(_f))
check("0.01 元 → 1 分", pay_mpay.normalize({"out_trade_no": "o", "money": "0.01"})["amount_fen"] == 1)
check("trade_status 原样带出", _f["trade_status"] == "TRADE_SUCCESS")
check("trade_no 带出", _f["trade_no"] == "T1")
check("缺 out_trade_no → None", pay_mpay.normalize({"money": "1.00"}) is None)
check("缺 money → None", pay_mpay.normalize({"out_trade_no": "o"}) is None)
check("money 非法 → None", pay_mpay.normalize({"out_trade_no": "o", "money": "abc"}) is None)

# ─── [5] 未配置 ⇒ 拒收不放行 ─────────────────────────────────
print("[5] 通道未配置 ⇒ 拒收不放行")
_saved_key = service.MPAY_KEY
service.MPAY_KEY = ""
check("configured() = False", pay_mpay.configured() is False)
check("config_problem 指出缺哪项", "MPAY_KEY" in pay_mpay.config_problem(), pay_mpay.config_problem())
check("pay_channel_ready() 也变 False", service.pay_channel_ready() is False)
_r = client.get("/api/pay/notify/mpay",
                params={"out_trade_no": "x", "money": "1.00", "trade_status": "TRADE_SUCCESS"})
check("★未配置回调 503", _r.status_code == 503, str(_r.status_code))
check("★503 应答体是纯文本 fail（不是 JSON）", _r.text == "fail", repr(_r.text))
check("未配置时无法出码（503）",
      client.post("/api/points/orders/ord_x/pay", headers=auth("t")).status_code in (401, 404, 503))
service.MPAY_KEY = _saved_key
check("恢复配置后就绪", pay_mpay.configured() is True)

# ─── [6] 下单（create_charge，MockTransport 打桩）────────────
print("[6] 下单 create_charge（打桩，不打真实网关）")


def _order():
    return {"order_id": "ord_1", "out_trade_no": "ord_1", "amount_fen": 1000, "pack_name": "体验包"}


def _ctx():
    return {"notify_url": "https://api.example.com/api/pay/notify/mpay",
            "return_url": "https://example.com/account", "clientip": "1.2.3.4", "device": "pc"}


def _charge(payload: dict, capture: dict | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(req.url)
            capture["form"] = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        return httpx.Response(200, json=payload)
    async def _go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await pay_mpay.create_charge(_order(), _ctx(), client=c)
    return asyncio.run(_go())


_cap: dict = {}
_r1 = _charge({"code": 1, "msg": "success", "trade_no": "T999", "qrcode": "weixin://wxpay/bizpayurl?pr=abc"}, _cap)
check("qrcode → pay_type=qrcode", _r1["pay_type"] == "qrcode" and _r1["pay_info"].startswith("weixin://"), str(_r1))
check("trade_no 带出", _r1["trade_no"] == "T999", str(_r1))
check("下单打到 /mapi.php", _cap["url"].endswith("/mapi.php"), _cap["url"])
check("下单参数 pid/type/money/out_trade_no 正确",
      _cap["form"]["pid"] == "1001" and _cap["form"]["type"] == "wxpay"
      and _cap["form"]["money"] == "10.00" and _cap["form"]["out_trade_no"] == "ord_1",
      str(_cap["form"]))
check("下单带 notify_url / sign_type=MD5 / clientip / return_url",
      _cap["form"]["notify_url"].endswith("/api/pay/notify/mpay")
      and _cap["form"]["sign_type"] == "MD5" and _cap["form"]["clientip"] == "1.2.3.4"
      and _cap["form"]["return_url"] == "https://example.com/account", str(_cap["form"]))
check("下单签名自洽（同一 KEY 可验）", pay_mpay.sign_v1(_cap["form"], "testkey") == _cap["form"]["sign"])

_r2 = _charge({"code": 1, "payurl": "https://pay.example.com/x"})
check("payurl → pay_type=jump", _r2["pay_type"] == "jump" and _r2["pay_info"].startswith("https://"), str(_r2))
_r3 = _charge({"code": 1, "urlscheme": "weixin://dl/business/?ticket=x"})
check("urlscheme → pay_type=urlscheme", _r3["pay_type"] == "urlscheme", str(_r3))

try:
    _charge({"code": 0, "msg": "商户不存在"})
    check("code=0 抛错", False, "未抛")
except PayChannelError as e:
    check("★code=0 抛 PayChannelError 且带下游原因", "商户不存在" in e.message, e.message)

try:
    _charge({"code": 1})
    check("成功但无可支付内容 → 抛错", False, "未抛")
except PayChannelError as e:
    check("成功但无可支付内容 → 抛错", "未返回可支付内容" in e.message, e.message)


def _charge_status(status: int):
    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(status, text="boom"))
        ) as c:
            return await pay_mpay.create_charge(_order(), _ctx(), client=c)
    return asyncio.run(_go())


try:
    _charge_status(500)
    check("HTTP 500 抛错", False, "未抛")
except PayChannelError as e:
    check("★HTTP 500 抛 PayChannelError", "HTTP 500" in e.message, e.message)

# ─── [6b] 收银台回查：把 jump 升级成可直接渲染的收款码（2026-10-15）──
# 背景：微信个人收款通道的 pay_page='page'，被 ePay V1 兜底成「收银台页 URL」，
# 于是我们永远只拿到 jump，用户必须多开一个页签再扫码。收银台页本身是从
# `/api/cashier/pay-order` 取数的 ⇒ 同源取回即可在本页直接渲染。
print("[6b] 收银台回查（jump → 可直接渲染的收款码）")
_CASHIER = "https://mpay.test/api/cashier/pay-order"


def _charge2(post_payload: dict, get_payload=None, get_status: int = 200, get_capture: dict | None = None):
    """POST /mapi.php 与 GET /api/cashier/pay-order **分别**打桩。"""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            if get_capture is not None:
                get_capture["url"] = str(req.url)
            return httpx.Response(get_status, json=get_payload if get_payload is not None else {})
        return httpx.Response(200, json=post_payload)
    async def _go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await pay_mpay.create_charge(_order(), _ctx(), client=c)
    return asyncio.run(_go())


_gcap: dict = {}
_rc = _charge2({"code": 1, "trade_no": "T1", "payurl": "https://mpay.test/payment/T1"},
               {"code": 200, "data": {"presentation": {"pay_params": {"qrcode": "wxp://f2f0abc"}}}},
               get_capture=_gcap)
check("★payurl + 收银台有收款码内容 ⇒ 升级成 qrcode（用户少跳一步）",
      _rc["pay_type"] == "qrcode" and _rc["pay_info"] == "wxp://f2f0abc", str(_rc))
check("升级后 trade_no 仍带出", _rc["trade_no"] == "T1", str(_rc))
check("★回查打到 /api/cashier/pay-order 且带 pay_no",
      _gcap.get("url", "").startswith(_CASHIER) and "pay_no=T1" in _gcap.get("url", ""),
      str(_gcap))

_ri = _charge2({"code": 1, "trade_no": "T1", "payurl": "https://mpay.test/payment/T1"},
               {"code": 200, "data": {"presentation": {"pay_params": {"qrcode_image": "https://mpay.test/qr.png"}}}})
check("★只配了收款码图片 ⇒ pay_type=image（图片地址**不能**当二维码内容编码）",
      _ri["pay_type"] == "image" and _ri["pay_info"].endswith(".png"), str(_ri))

_rj = _charge2({"code": 1, "trade_no": "T1", "payurl": "https://mpay.test/payment/T1"}, {})
check("★收银台取不到码 ⇒ 降级回 jump（体验优化不得堵住付款路径）",
      _rj["pay_type"] == "jump" and _rj["pay_info"] == "https://mpay.test/payment/T1", str(_rj))

_rj2 = _charge2({"code": 1, "trade_no": "T1", "payurl": "https://mpay.test/payment/T1"},
                {"code": 500, "msg": "boom"}, get_status=500)
check("收银台 HTTP 500 同样降级回 jump（不把优化点变成故障点）",
      _rj2["pay_type"] == "jump", str(_rj2))

_rj3 = _charge2({"code": 1, "trade_no": "T1", "payurl": "https://mpay.test/payment/T1"},
                {"code": 200, "data": None})
check("收银台返回空 data 也安全降级（不缺键崩溃）", _rj3["pay_type"] == "jump", str(_rj3))

_seen_get = {"n": 0}


def _handler_q_only(req: httpx.Request) -> httpx.Response:
    if req.method == "GET":
        _seen_get["n"] += 1
        return httpx.Response(200, json={})
    return httpx.Response(200, json={"code": 1, "trade_no": "T1", "qrcode": "weixin://direct"})


async def _go_q_only():
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler_q_only)) as c:
        return await pay_mpay.create_charge(_order(), _ctx(), client=c)


_rq = asyncio.run(_go_q_only())
check("平台已直接给 qrcode ⇒ 不再多打一次收银台（省一次往返）",
      _rq["pay_type"] == "qrcode" and _seen_get["n"] == 0, f"{_rq} get={_seen_get['n']}")

try:
    _charge({"code": 0, "msg": "当前通道可用金额偏移已用尽"})
    check("★「可用金额偏移已用尽」应抛错", False, "未抛")
except PayChannelError as e:
    check("★平台占用文案被翻译成用户可照做的一句（不是通道故障文案）",
          e.message == service.SAME_AMOUNT_BUSY_MSG and e.code == 409,
          f"code={e.code} msg={e.message!r}")

try:
    _charge({"code": 0, "msg": "商户不存在"})
    check("其它平台错误仍保留原文（别把不同原因混成一句）", False, "未抛")
except PayChannelError as e:
    check("其它平台错误仍保留原文（别把不同原因混成一句）",
          "商户不存在" in e.message and "code=0" in e.message, e.message)

# ─── [7] 出码端点（HTTP 端到端）──────────────────────────────
print("[7] 出码端点")
t1 = service.register("alice", "secret123")["token"]
t2 = service.register("bob", "secret123")["token"]

_order1 = new_order(t1)
_ctx_seen: dict = {}


async def _fake_charge(order, ctx, client=None):
    _ctx_seen.clear()
    _ctx_seen.update(ctx)
    return {"trade_no": "T-fake", "pay_type": "qrcode",
            "pay_info": "weixin://wxpay/bizpayurl?pr=fake1"}


with patch.object(pay_mpay, "create_charge", _fake_charge):
    _resp = client.post(f"/api/points/orders/{_order1['order_id']}/pay", headers=auth(t1))
check("出码 200", _resp.status_code == 200, f"{_resp.status_code} {_resp.text[:200]}")
_pay = _resp.json()["pay"]
check("pay_type/pay_info 正确",
      _pay["pay_type"] == "qrcode" and _pay["pay_info"].startswith("weixin://"), str(_pay))
check("首次 reused=False", _pay["reused"] is False, str(_pay))
check("★出码响应下发 valid_seconds（界面时长与平台有效期同源）",
      _pay.get("valid_seconds") == 300, str(_pay))
check("★复用窗口由有效期派生、且严格小于有效期（超窗复用＝必然漏单）",
      service.PAY_CHARGE_REUSE_SECONDS
      == service.PAY_CHARGE_VALID_SECONDS - service.PAY_CHARGE_REUSE_MARGIN_SECONDS
      and 0 < service.PAY_CHARGE_REUSE_SECONDS < service.PAY_CHARGE_VALID_SECONDS,
      f"reuse={service.PAY_CHARGE_REUSE_SECONDS} valid={service.PAY_CHARGE_VALID_SECONDS}")
check("出码信息已落库", store.get_order(_order1["order_id"])["pay_info"] == _pay["pay_info"])
check("★notify_url 由 PAY_PUBLIC_BASE_URL 拼出",
      _ctx_seen["notify_url"] == "https://api.example.com/api/pay/notify/mpay", str(_ctx_seen))
check("return_url 下发给通道", _ctx_seen["return_url"] == "https://example.com/account", str(_ctx_seen))

_calls = {"n": 0}


async def _counting_charge(order, ctx, client=None):
    _calls["n"] += 1
    return {"trade_no": "T-x", "pay_type": "qrcode", "pay_info": "weixin://x"}


with patch.object(pay_mpay, "create_charge", _counting_charge):
    _resp2 = client.post(f"/api/points/orders/{_order1['order_id']}/pay", headers=auth(t1))
check("★窗口内复用（不重复向下游下单）",
      _resp2.status_code == 200 and _calls["n"] == 0
      and _resp2.json()["pay"]["reused"] is True, f"calls={_calls['n']}")
check("复用响应同样下发 valid_seconds（两条路径不能有一处漏）",
      _resp2.json()["pay"].get("valid_seconds") == 300, str(_resp2.json()["pay"]))

check("别人出码 → 404",
      client.post(f"/api/points/orders/{_order1['order_id']}/pay", headers=auth(t2)).status_code == 404)
check("未登录出码 → 401",
      client.post(f"/api/points/orders/{_order1['order_id']}/pay").status_code == 401)

# ─── [8] 回调（GET + 纯文本 success）─────────────────────────
print("[8] 回调（GET + query + 纯文本 success）")
_before = balance(t1)
_resp = client.get("/api/pay/notify/mpay", params=notify_params(_order1))
check("回调 200", _resp.status_code == 200, f"{_resp.status_code} {_resp.text[:200]}")
check("★应答体是纯文本 success（不是 JSON）", _resp.text == "success", repr(_resp.text))
check("余额 +1000", balance(t1) == _before + 1000, f"{_before} -> {balance(t1)}")
check("订单转 paid", store.get_order(_order1["order_id"])["status"] == "paid")

_resp_dup = client.get("/api/pay/notify/mpay", params=notify_params(_order1))
check("★重复回调幂等（余额不变 + 仍回 success）",
      balance(t1) == _before + 1000 and _resp_dup.text == "success",
      f"{balance(t1)} {_resp_dup.text!r}")

_order_bad = new_order(t1)
_bad = notify_params(_order_bad, key="wrongkey")
_rbad = client.get("/api/pay/notify/mpay", params=_bad)
check("★验签失败 401 + 纯文本 fail",
      _rbad.status_code == 401 and _rbad.text == "fail", f"{_rbad.status_code} {_rbad.text!r}")
check("验签失败不入账", balance(t1) == _before + 1000, str(balance(t1)))

_ro = client.get("/api/pay/notify/mpay",
                 params={"out_trade_no": _order_bad["out_trade_no"], "trade_status": "TRADE_SUCCESS"})
check("缺 money 字段 → 400 fail（签名本身也算错，故可能是 401）",
      _ro.status_code in (400, 401) and _ro.text == "fail", f"{_ro.status_code} {_ro.text!r}")

# ─── [9] 金额策略 ge（金额会被微调）──────────────────────────
print("[9] 金额策略 ge（金额会被微调：多付放行、少付拒绝）")
_o3 = new_order(t1)
_b3 = balance(t1)
_r3a = client.get("/api/pay/notify/mpay", params=notify_params(_o3, money="10.03"))
check("★多付 3 分仍入账（微调放行）",
      _r3a.status_code == 200 and balance(t1) == _b3 + 1000, f"{_r3a.status_code} {_r3a.text!r}")
check("★入账按订单快照（+1000，不是按收到金额折算）", balance(t1) == _b3 + 1000, str(balance(t1)))

_o4 = new_order(t1)
_b4 = balance(t1)
_r4 = client.get("/api/pay/notify/mpay", params=notify_params(_o4, money="9.97"))
check("★少付被拒 400 + fail", _r4.status_code == 400 and _r4.text == "fail",
      f"{_r4.status_code} {_r4.text!r}")
check("少付不入账", balance(t1) == _b4, str(balance(t1)))
check("少付后订单仍 pending", store.get_order(_o4["order_id"])["status"] == "pending")

# ─── [10] 非成功状态 ───────────────────────────────────────
print("[10] 非成功状态（已关闭 / 退款）")
_o5 = new_order(t1)
_b5 = balance(t1)
_r5 = client.get("/api/pay/notify/mpay", params=notify_params(_o5, status="TRADE_CLOSED"))
check("非 TRADE_SUCCESS：200 + success 但不入账",
      _r5.status_code == 200 and _r5.text == "success" and balance(t1) == _b5,
      f"{_r5.status_code} {_r5.text!r} {balance(t1)}")
check("订单仍 pending", store.get_order(_o5["order_id"])["status"] == "pending")

# ─── [11] 已付订单的边界 ────────────────────────────────────
print("[11] 已付订单边界")
check("已付订单再出码 → 409",
      client.post(f"/api/points/orders/{_order1['order_id']}/pay", headers=auth(t1)).status_code == 409)
check("已付订单再回调 → 仍 success（幂等）",
      client.get("/api/pay/notify/mpay", params=notify_params(_order1)).text == "success")

# ─── [12] 通配通道兜底（历史契约）────────────────────────────
print("[12] 通配通道兜底（历史契约不受影响）")
_r12 = client.post("/api/pay/notify/agg", json={"out_trade_no": "x"})
check("未配 PAY_NOTIFY_SECRET ⇒ 通配通道 503（不放行）", _r12.status_code == 503, str(_r12.status_code))
_saved_secret = service.PAY_NOTIFY_SECRET
service.PAY_NOTIFY_SECRET = "s3cret"
_p12 = {"out_trade_no": "nope", "amount_fen": 1000, "trade_status": "paid"}
_p12["sign"] = pay_generic.sign_params(_p12, "s3cret")
_r12b = client.post("/api/pay/notify/agg", json=_p12)
check("通配通道仍走 JSON 契约（订单不存在 → 404 JSON）",
      _r12b.status_code == 404 and _r12b.headers["content-type"].startswith("application/json"),
      f"{_r12b.status_code} {_r12b.headers.get('content-type')}")
service.PAY_NOTIFY_SECRET = _saved_secret

# ─── [13] 缺 PAY_PUBLIC_BASE_URL ⇒ 出码 503 且说明原因 ────────
print("[13] 缺 PAY_PUBLIC_BASE_URL")
_saved_base = service.PAY_PUBLIC_BASE_URL
service.PAY_PUBLIC_BASE_URL = ""
_o6 = new_order(t1)
_r13 = client.post(f"/api/points/orders/{_o6['order_id']}/pay", headers=auth(t1))
check("★未配公网地址 ⇒ 503 且 detail 指明缺什么",
      _r13.status_code == 503 and "PAY_PUBLIC_BASE_URL" in _r13.json().get("detail", ""),
      str(_r13.json()))
service.PAY_PUBLIC_BASE_URL = _saved_base

# ─── [14] update_order_fields 语义 ──────────────────────────
print("[14] update_order_fields 语义")
_o7 = new_order(t1)
_upd = store.update_order_fields(_o7["order_id"], {"note": "hi"})
check("字段写入且 status 不变", _upd["note"] == "hi" and _upd["status"] == "pending", str(_upd))
check("不存在订单返回 None", store.update_order_fields("ord_nope", {"a": 1}) is None)

# ─── [15] 模拟支付已关闭 ─────────────────────────────────────
print("[15] 模拟支付已关闭（MEMBER_MOCK_PAY=0）")
_r15 = client.post(f"/api/points/orders/{_o7['order_id']}/mock-pay", headers=auth(t1))
check("mock-pay → 403", _r15.status_code == 403, str(_r15.status_code))

# ─── [16] 套餐清单暴露的支付状态 ─────────────────────────────
print("[16] /api/points/packs 暴露的支付状态")
_pk = client.get("/api/points/packs").json()
check("pay_channel_ready = True", _pk["pay_channel_ready"] is True, str(_pk.get("pay_channel_ready")))
check("pay_provider = mpay", _pk["pay_provider"] == "mpay", str(_pk.get("pay_provider")))
check("mock_pay_enabled = False", _pk["mock_pay_enabled"] is False, str(_pk.get("mock_pay_enabled")))

# ─── [17] MPAY_PID 形态：**只拦出码，不拦回调** ───────────────
# 2026-10-14 实测：pid 填成商户编号（M2026…）会被 mpay 在发请求后拒；所以要在
# **发请求之前**拦住。但回调只依赖 KEY —— pid 错期间到账的钱必须还能记账。
print("[17] MPAY_PID 形态（必须纯数字）")
check("★契约含 charge_problem（两条通道都实现）",
      "charge_problem" in pay._REQUIRED_API
      and callable(getattr(pay_mpay, "charge_problem", None))
      and callable(getattr(pay_generic, "charge_problem", None)))

_saved_pid = service.MPAY_PID
service.MPAY_PID = "M20261014120400214497"          # 商户编号，不是 API 的 pid
_p17 = pay_mpay.charge_problem()
check("★charge_problem() 点名 MPAY_PID 且认得出填的是商户编号",
      "MPAY_PID" in _p17 and "商户编号" in _p17, _p17)
check("configured() 仍为真（回调只依赖 KEY，不该被 pid 形态拦）",
      pay_mpay.configured() is True)
check("config_problem() 不报它（「形态错」≠「缺项」，两者分工不同）",
      pay_mpay.config_problem() == "", pay_mpay.config_problem())
check("pay_channel_ready() 变 False（否则前端会给个点了必然 503 的按钮）",
      service.pay_channel_ready() is False)
_d17 = pay.describe()
check("自检 ready=False 且 problem 点名 MPAY_PID",
      _d17["ready"] is False and "MPAY_PID" in _d17["problem"], str(_d17))

_calls17 = {"n": 0}


async def _fake17(order, ctx, client=None):
    _calls17["n"] += 1
    return {"trade_no": "T", "pay_type": "qrcode", "pay_info": "weixin://x"}


_o17 = new_order(t1)
with patch.object(pay_mpay, "create_charge", _fake17):
    _r17 = client.post(f"/api/points/orders/{_o17['order_id']}/pay", headers=auth(t1))
check("★出码 503 且 detail 点名 MPAY_PID",
      _r17.status_code == 503 and "MPAY_PID" in _r17.json().get("detail", ""),
      f"{_r17.status_code} {_r17.json()}")
check("★形态错时**未向通道发请求**（省一次白跑的往返）", _calls17["n"] == 0, str(_calls17))

_b17 = balance(t1)
_r17n = client.get("/api/pay/notify/mpay", params=notify_params(_o17))
check("★pid 形态错**不影响回调**（错配期间到账的钱仍要能记账）",
      _r17n.status_code == 200 and _r17n.text == "success" and balance(t1) == _b17 + 1000,
      f"{_r17n.status_code} {_r17n.text!r} {_b17}->{balance(t1)}")
service.MPAY_PID = _saved_pid
check("恢复 pid 后重新就绪",
      pay_mpay.charge_problem() == "" and service.pay_channel_ready() is True)

# ─── [18] PAY_PUBLIC_BASE_URL 填成完整回调地址（静默失败源）────
# 这是最难查的一种：mpay **不校验 notify_url**、照样返回下单成功，表现成
# 「二维码正常出、用户付了钱、积分永远不动」。故宁可响亮失败，不自动去重。
print("[18] PAY_PUBLIC_BASE_URL 填成完整回调地址")
_saved_base2 = service.PAY_PUBLIC_BASE_URL
service.PAY_PUBLIC_BASE_URL = "https://api.example.com/api/pay/notify/mpay"
_bp18 = service.pay_public_base_problem()
check("★认出重复段并说清拼出来会重复", "PAY_PUBLIC_BASE_URL" in _bp18 and "重复" in _bp18, _bp18)

_calls18 = {"n": 0}


async def _fake18(order, ctx, client=None):
    _calls18["n"] += 1
    return {"trade_no": "T", "pay_type": "qrcode", "pay_info": "weixin://x"}


_o18 = new_order(t1)
with patch.object(pay_mpay, "create_charge", _fake18):
    _r18 = client.post(f"/api/points/orders/{_o18['order_id']}/pay", headers=auth(t1))
check("★出码 503 且 detail 说清重复在哪一段",
      _r18.status_code == 503
      and "PAY_PUBLIC_BASE_URL" in _r18.json().get("detail", "")
      and "重复" in _r18.json().get("detail", ""),
      f"{_r18.status_code} {_r18.json()}")
check("★地址形态错时也未发请求", _calls18["n"] == 0, str(_calls18))
_d18 = pay.describe()
check("自检 ready=False 且 problem 指出地址问题",
      _d18["ready"] is False and "PAY_PUBLIC_BASE_URL" in _d18["problem"], str(_d18))
check("自检 notify_url 原样暴露（一眼看出重复那一段）",
      _d18["notify_url"] == "https://api.example.com/api/pay/notify/mpay/api/pay/notify/mpay",
      _d18["notify_url"])
service.PAY_PUBLIC_BASE_URL = _saved_base2
check("恢复根地址后检查通过", service.pay_public_base_problem() == "")
check("notify_url_for 与测试期望同口径（拼接只此一处）",
      service.notify_url_for("mpay") == "https://api.example.com/api/pay/notify/mpay",
      service.notify_url_for("mpay"))

# ─── [19] MEMBER_MOCK_PAY 代码默认值（必须在安全那一侧）──────
# 2026-10-14 由 "1" 改为 "0"：默认开 = 忘写这一项就等于把免费入账的口子敞开。
# 默认值是 import 期读的，只能在**干净子进程**里验（并屏蔽真实 .env 干扰）。
print("[19] MEMBER_MOCK_PAY 代码默认值")


def _mock_pay_default(env_val: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "MEMBER_MOCK_PAY"}
    env["DATA_DIR"] = TMP
    if env_val is not None:
        env["MEMBER_MOCK_PAY"] = env_val
    code = (
        "from pathlib import Path\n"
        "_p = Path.exists\n"
        "Path.exists = lambda s: False if s.name == '.env' else _p(s)\n"
        "from app.membership import service\n"
        "print('MEMBER_MOCK_PAY_DEFAULT=' + str(service.MOCK_PAY))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND_ROOT),
                         env=env, capture_output=True, text=True)
    lines = [l.strip() for l in out.stdout.splitlines()]
    hit = next((l for l in reversed(lines) if l.startswith("MEMBER_MOCK_PAY_DEFAULT=")), "")
    return hit.split("=", 1)[1] if hit else f"ERR:{out.stderr[-160:]}"


_d19 = _mock_pay_default()
check("★不设 MEMBER_MOCK_PAY ⇒ 默认 False（关）", _d19 == "False", _d19)
check("显式给 0 ⇒ False", _mock_pay_default("0") == "False")
check("显式给 1 ⇒ True（演示环境仍有开关）", _mock_pay_default("1") == "True")

# ─── [20] 订单过期：落库 / 拒绝再出码 / ★★入账仍接受 ──────────
# 2026-10-15 用户需求 3：积分商城里一堆「待支付」能反复点，过了有效期的单
# 不该再让用户去支付。做法是引入 `expired` 状态（**惰性落库**，无后台任务）。
# ⚠️ 本段最重要的一条是最后那个 ★★：expired **只是展示态**，不是终态。
#    个人收款监听两侧有效期各配一份，只要差几秒就会出现「我们判过期、用户其实
#    付成功」；若入账路径不认 expired，那条回调会抢单失败 ⇒ **钱到了、积分不给**。
print("[20] 订单过期（落库 / 拒绝再出码 / 入账仍接受）")
_o20 = new_order(t1)
store.update_order_fields(_o20["order_id"], {
    "pay_type": "qrcode", "pay_info": "weixin://old",
    "pay_created_at": (datetime.now()
                       - timedelta(seconds=service.PAY_CHARGE_VALID_SECONDS + 30)).isoformat(),
})
check("sanity：order_expired 判为已过期", service.order_expired(store.get_order(_o20["order_id"])) is True)

_lo = client.get("/api/points/orders", headers=auth(t1)).json()
_e20 = next(o for o in _lo["orders"] if o["order_id"] == _o20["order_id"])
check("★列表把过期 pending 单标成 expired（不再显示「待支付」）", _e20["status"] == "expired", str(_e20))
check("★真的落库（不是只改了返回值）",
      store.get_order(_o20["order_id"])["status"] == "expired",
      str(store.get_order(_o20["order_id"])["status"]))
check("expired_at 已记录", bool(store.get_order(_o20["order_id"]).get("expired_at")))

_calls20 = {"n": 0}


async def _fake20(order, ctx, client=None):
    _calls20["n"] += 1
    return {"trade_no": "T", "pay_type": "qrcode", "pay_info": "weixin://x"}


with patch.object(pay_mpay, "create_charge", _fake20):
    _r20 = client.post(f"/api/points/orders/{_o20['order_id']}/pay", headers=auth(t1))
check("★过期单再出码 → 409 且文案说清要重新下单",
      _r20.status_code == 409 and "过期" in _r20.json().get("detail", ""),
      f"{_r20.status_code} {_r20.json()}")
check("★过期单不再向通道发请求（不给平台添一张占位单）", _calls20["n"] == 0, str(_calls20))

_b20 = balance(t1)
_r20n = client.get("/api/pay/notify/mpay", params=notify_params(_o20))
check("★★过期订单收到真实支付回调**仍然入账**（expired 必须是展示态、非终态）",
      _r20n.status_code == 200 and _r20n.text == "success" and balance(t1) == _b20 + 1000,
      f"{_r20n.status_code} {_r20n.text!r} {_b20}->{balance(t1)}")
check("过期单入账后转 paid", store.get_order(_o20["order_id"])["status"] == "paid")

# ─── [21] 同一面额被占用（需求 2）───────────────────────────
# 个人收款监听按**金额**匹配 ⇒ 同一面额同时刻只能挂一张待付单。我们自己先拦，
# 比让 mpay 回一句「可用金额偏移已用尽」体验好得多，也省一次白跑的往返。
# 判据必须与平台一致：**已出码 + 仍在有效期**（只建单没出码在平台侧什么都不占）。
print("[21] 同一面额被占用（我们侧预检）")
_o21a = new_order(t1)
store.update_order_fields(_o21a["order_id"], {
    "pay_type": "qrcode", "pay_info": "weixin://busy",
    "pay_created_at": datetime.now().isoformat(),
})
_o21b = new_order(t2)                      # 别的用户下的**同面额**单
_calls21 = {"n": 0}


async def _fake21(order, ctx, client=None):
    _calls21["n"] += 1
    return {"trade_no": "T", "pay_type": "qrcode", "pay_info": "weixin://x"}


with patch.object(pay_mpay, "create_charge", _fake21):
    _r21 = client.post(f"/api/points/orders/{_o21b['order_id']}/pay", headers=auth(t2))
check("★同一面额已被占用 ⇒ 409 + 用户可照做的文案（跨用户也算占用）",
      _r21.status_code == 409 and _r21.json().get("detail") == service.SAME_AMOUNT_BUSY_MSG,
      f"{_r21.status_code} {_r21.json()}")
check("★被占用时未向通道发请求", _calls21["n"] == 0, str(_calls21))

_o21c = client.post("/api/points/orders", json={"pack_id": "p3200"},
                    headers=auth(t2)).json()["order"]
with patch.object(pay_mpay, "create_charge", _fake21):
    _r21c = client.post(f"/api/points/orders/{_o21c['order_id']}/pay", headers=auth(t2))
check("★不同面额可继续下单支付（¥30 档不受 ¥10 档占用影响）",
      _r21c.status_code == 200 and _calls21["n"] == 1,
      f"{_r21c.status_code} {_r21c.text[:120]} calls={_calls21['n']}")

# 只建单、没出码的旧单在平台侧**什么都不占** —— 不能把它误判成冲突（否则正常下单被堵死）
_o21d = client.post("/api/points/orders", json={"pack_id": "p5750"},
                    headers=auth(t2)).json()["order"]
_o21e = client.post("/api/points/orders", json={"pack_id": "p5750"},
                    headers=auth(t2)).json()["order"]
with patch.object(pay_mpay, "create_charge", _fake21):
    _r21e = client.post(f"/api/points/orders/{_o21e['order_id']}/pay", headers=auth(t2))
check("★未出码的同面额旧单不算占用（判据是「已出码」而不是「建过单」）",
      _r21e.status_code == 200, f"{_r21e.status_code} {_r21e.text[:120]}")
check("sanity：同面额的两张未出码单都在", _o21d["order_id"] != _o21e["order_id"])

# ─── [22] 过期订单可正常重新下单（流程闭环）──────────────────
print("[22] 过期后重新下单")
_o22 = new_order(t1)
store.update_order_fields(_o22["order_id"], {"pay_created_at": (datetime.now() - timedelta(
    seconds=service.PAY_CHARGE_VALID_SECONDS + 1)).isoformat()})
_r22 = client.post("/api/points/orders", json={"pack_id": "p1000"}, headers=auth(t1))
check("★过期不影响重新下单（拿到新单）", _r22.status_code == 200, str(_r22.status_code))
check("新单状态为 pending", _r22.json()["order"]["status"] == "pending", str(_r22.json()["order"]))

print(f"\n=== mpay 支付通道：{PASS} 通过 / {FAIL} 失败 ===")
sys.exit(1 if FAIL else 0)
