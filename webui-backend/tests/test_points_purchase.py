#!/usr/bin/env python3
"""积分购买链路单测：注册礼包 / 套餐清单 / 下单 / 幂等入账 / 支付回调契约。

覆盖的**四个不可退让点**（改这块时照这张单子核）：
  ① 下单不发积分（只落 pending 订单）
  ② settle 幂等（重复通知只发一次）
  ③ 越权隔离（别人的订单 404）
  ④ 回调没配密钥一律 503（不提供"无密钥放行"降级）

用法：/Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python tests/test_points_purchase.py
隔离：临时 DATA_DIR，不污染真实数据。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="wb-purchase-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_REG_BONUS"] = "500"
os.environ["MEMBER_CHECKIN_BONUS"] = "0"
os.environ["MEMBER_POINTS_PER_1000_CHARS"] = "50"
os.environ["MEMBER_MOCK_PAY"] = "1"
os.environ.pop("PAY_NOTIFY_SECRET", None)
os.environ["MEMBER_ENFORCE"] = "1"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

# 默认值回归不得受真实 .env 干扰（同 test_membership.py 的手法）
_path_exists = Path.exists
with patch.object(Path, "exists", lambda p: False if p.name == ".env" else _path_exists(p)):
    from app.main import app  # noqa: E402
from app.membership import pay, service, store  # noqa: E402
from app.membership.service import MemberError  # noqa: E402

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


print("[1] 注册礼包 500")
r1 = service.register("alice", "secret123")
r2 = service.register("bob", "secret123")
t1, u1 = r1["token"], r1["user"]
t2 = r2["token"]
check("注册即到账 500 积分", u1["points"] == 500, str(u1))
check("第二个用户同样是 500", r2["user"]["points"] == 500, str(r2["user"]))

print("[2] 套餐清单（无需登录）")
resp = client.get("/api/points/packs")
check("清单 200", resp.status_code == 200, str(resp.status_code))
body = resp.json()
ids = [p["id"] for p in body["packs"]]
check("四档且按 order 升序", ids == ["p1000", "p3200", "p5750", "p12500"], str(ids))
p = next(x for x in body["packs"] if x["id"] == "p5750")
check("p5750 售价 5000 分（¥50.00）", p["price_fen"] == 5000 and p["price_yuan"] == "50.00", str(p))
check("p5750 到账 5750 积分", p["points"] == 5750, str(p))
check("赠送比例 15.0%", p["bonus_percent"] == 15.0, str(p))
check("est_chars 按当前单价现算 = 115000 字", p["est_chars"] == 5750 * 1000 // 50, str(p["est_chars"]))
check("模拟支付可用", body["mock_pay_enabled"] is True, str(body["mock_pay_enabled"]))
check("真实回调未启用（无密钥）", body["pay_channel_ready"] is False, str(body["pay_channel_ready"]))
check("兑换比例锚点 100 分/元", body["points_per_yuan"] == 100, str(body["points_per_yuan"]))
p32 = next(x for x in body["packs"] if x["id"] == "p3200")
check("p3200 赠送比例 6.7%", p32["bonus_percent"] == 6.7, str(p32["bonus_percent"]))
check("p1000 无赠送", p32 and next(x for x in body["packs"] if x["id"] == "p1000")["bonus_points"] == 0)

print("[3] 下单单发（不发积分）")
check("未登录下单 401", client.post("/api/points/orders", json={"pack_id": "p5750"}).status_code == 401)
before = balance(t1)
order = client.post("/api/points/orders", json={"pack_id": "p5750"}, headers=auth(t1)).json()["order"]
check("订单状态 pending", order["status"] == "pending", str(order["status"]))
check("订单金额快照 5000 分", order["amount_fen"] == 5000, str(order["amount_fen"]))
check("订单积分快照 5750", order["points"] == 5750, str(order["points"]))
check("out_trade_no 默认等于 order_id", order["out_trade_no"] == order["order_id"], str(order["out_trade_no"]))
check("★下单后积分不变", balance(t1) == before, f"{before} -> {balance(t1)}")
check("未知套餐 404", client.post("/api/points/orders", json={"pack_id": "nope"}, headers=auth(t1)).status_code == 404)

print("[4] 模拟支付入账 + 幂等")
pay1 = client.post(f"/api/points/orders/{order['order_id']}/mock-pay", headers=auth(t1)).json()
check("首次入账 +5750", pay1["added"] == 5750, str(pay1))
check("余额 500 + 5750 = 6250", pay1["balance"] == 6250, str(pay1))
check("首次入账 already_paid=False", pay1["already_paid"] is False, str(pay1))
pay2 = client.post(f"/api/points/orders/{order['order_id']}/mock-pay", headers=auth(t1)).json()
check("★重复支付不再发积分", pay2["added"] == 0, str(pay2))
check("★重复支付标记 already_paid=True", pay2["already_paid"] is True, str(pay2))
check("★重复支付余额不变", pay2["balance"] == 6250 and balance(t1) == 6250, str(pay2["balance"]))

print("[5] 流水与订单归属")
logs = client.get("/api/points/logs?limit=50", headers=auth(t1)).json()["logs"]
purch = [x for x in logs if x["kind"] == "purchase"]
check("流水出现 purchase 类型", len(purch) == 1, str([x["kind"] for x in logs]))
check("流水 ref 指向订单", bool(purch) and purch[0]["ref"] == f"order:{order['order_id']}", str(purch[:1]))
check("★他人订单模拟支付 404（不报 403，避免探测）",
      client.post(f"/api/points/orders/{order['order_id']}/mock-pay", headers=auth(t2)).status_code == 404)
mine = client.get("/api/points/orders", headers=auth(t1)).json()
check("本人订单列表 1 条", mine["total"] == 1, str(mine["total"]))
check("本人订单列表可见", mine["orders"][0]["order_id"] == order["order_id"])
check("★他人订单列表为空", client.get("/api/points/orders", headers=auth(t2)).json()["total"] == 0)

print("[6] 金额校验（防回调篡改）")
o2 = service.create_order(store.load_users()[u1["user_id"]], "p1000")
try:
    service.settle_order(o2["order_id"], "tamper", amount_fen=1)
    check("★金额不符被拒", False, "未抛错")
except MemberError as e:
    check("★金额不符被拒（400）", e.code == 400, f"{e.code} {e.message}")
check("金额不符后订单仍 pending",
      store.load_orders()[o2["order_id"]]["status"] == "pending")

print("[7] 真实支付回调契约")
check("★未配密钥回调 503", client.post("/api/pay/notify/agg", json={}).status_code == 503)
service.PAY_NOTIFY_SECRET = "s3cret"   # routes 每次请求现读，改这里即生效
o3 = service.create_order(store.load_users()[u1["user_id"]], "p1000")
bad = {"out_trade_no": o3["order_id"], "amount_fen": 1000, "trade_status": "paid", "sign": "deadbeef"}
check("★验签失败 401", client.post("/api/pay/notify/agg", json=bad).status_code == 401)
good = {"out_trade_no": o3["order_id"], "amount_fen": 1000, "trade_status": "paid"}
good["sign"] = pay.sign_params(good, "s3cret")
resp = client.post("/api/pay/notify/agg", json=good)
check("验签通过并入账", resp.status_code == 200 and resp.json()["balance"] == 7250, str(resp.json()))
resp2 = client.post("/api/pay/notify/agg", json=good)
check("★回调重复推送幂等", resp2.status_code == 200 and resp2.json()["already_paid"] is True, str(resp2.json()))
o4 = service.create_order(store.load_users()[u1["user_id"]], "p1000")
closed = {"out_trade_no": o4["order_id"], "amount_fen": 1000, "trade_status": "closed"}
closed["sign"] = pay.sign_params(closed, "s3cret")
rc = client.post("/api/pay/notify/agg", json=closed)
check("非成功状态确认收到但不入账", rc.status_code == 200 and rc.json().get("message") == "ignored", str(rc.json()))
check("非成功状态不改余额", balance(t1) == 7250, str(balance(t1)))
check("回调金额与订单不符被拒 400",
      client.post("/api/pay/notify/agg", json={**{k: v for k, v in good.items() if k != "sign"},
                  "amount_fen": 999, "sign": pay.sign_params({**{k: v for k, v in good.items() if k != "sign"}, "amount_fen": 999}, "s3cret")}).status_code == 400)

print("[8] 报文归一化的单位换算")
_fact = pay.normalize_notify("agg", {"out_trade_no": "x", "amount": "50.00", "trade_status": "paid"})
check("amount（元）→ 分", bool(_fact) and _fact["amount_fen"] == 5000, str(_fact))
check("缺 out_trade_no → None", pay.normalize_notify("agg", {"amount_fen": 1}) is None)

service.PAY_NOTIFY_SECRET = None

print(f"\n=== 积分购买链路：{PASS} 通过 / {FAIL} 失败 ===")
sys.exit(1 if FAIL else 0)
