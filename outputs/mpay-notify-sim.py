#!/usr/bin/env python3
"""模拟 mpay 回调，端到端验证**入账链路**（不需要真的付钱，也不需要 mpay 调我们）。

在 webui-backend 目录下跑：
    cd webui-backend
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
        ~/.workbuddy/binaries/python/envs/default/bin/python ../outputs/mpay-notify-sim.py

它用**真实 .env 里的 MPAY_KEY** 给回调报文签名，然后按 ePay V1 的形态
（GET + query，应答纯文本 success）打给 `/api/pay/notify/mpay`，
覆盖这些场景：

  ① 正常付款              → 200 "success" + 积分到账 + 订单转 paid
  ② 同一笔重推            → 200 "success" 但**不重复发积分**（幂等）
  ③ 金额被微调（多几分）  → 200 "success" + 到账（通道声明 ge 策略）
  ④ 金额少于订单          → 拒收（4xx）且**不退化成入账**
  ⑤ 签名篡改              → 401 且不入账
  ⑥ 非成功状态（已关闭）  → 200 "success" 但不入账
  ⑦ POST 载体             → 同样受理（便于手工重放）
  ⑧ 出码端点（真实打 mpay）→ 打印下游真实报错，验证「后端→mpay」这段是通的

数据目录隔离到临时目录（DATA_DIR 覆盖），**不碰** data/ 里任何真实数据。
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "webui-backend"

# ⚠️ 必须在 import app.* 之前：config._load_dotenv 是 setdefault 语义，
#    真实环境变量优先，所以这里能压过 .env。
_TMP = tempfile.mkdtemp(prefix="mpay-notify-sim-")
os.environ["DATA_DIR"] = _TMP
os.environ["MEMBER_STORE"] = "file"
os.environ["SESSION_STORE"] = "file"

sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient   # noqa: E402
from app.main import app                    # noqa: E402
from app.membership import service as svc   # noqa: E402
from app.membership import pay_mpay         # noqa: E402
from app.membership import pay as pay_mod   # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   [{detail}]" if detail else ""))


def sign(params: dict) -> str:
    """照 ePay V1：排除 sign/sign_type 与空值 → ASCII 升序 → 末尾裸接 KEY → md5。"""
    key = (svc.MPAY_KEY or "")
    items = [f"{k}={params[k]}" for k in sorted(params)
             if k not in ("sign", "sign_type") and params[k] not in (None, "")]
    return hashlib.md5(("&".join(items) + key).encode("utf-8")).hexdigest()


def notify_body(out_trade_no: str, money_yuan: str, trade_status="TRADE_SUCCESS",
                sign_ok=True, trade_no="T2026101400001") -> dict:
    p = {
        "pid": svc.MPAY_PID,
        "trade_no": trade_no,
        "out_trade_no": out_trade_no,
        "type": svc.MPAY_TYPE,
        "name": "积分充值",
        "money": money_yuan,
        "trade_status": trade_status,
        "sign_type": "MD5",
    }
    p["sign"] = sign(p)
    if not sign_ok:
        p["sign"] = "0" * 32
    return p


def balance(c: TestClient, h: dict) -> int:
    return c.get("/api/points/balance", headers=h).json()["points"]


def main() -> int:
    print(f"数据目录（隔离）: {_TMP}")
    print(f"MPAY_PID = {svc.MPAY_PID!r}   KEY = len({len(svc.MPAY_KEY or '')})   "
          f"TYPE = {svc.MPAY_TYPE!r}   金额策略 = ge")
    print(f"注册通道: {json.dumps(pay_mod.describe(), ensure_ascii=False)}")
    print()

    if not svc.MPAY_KEY:
        print("MPAY_KEY 未配置，无法验签，终止。")
        return 2

    c = TestClient(app)
    reg = svc.register("mpay_sim", "secret123", "联调")
    h = {"Authorization": f"Bearer {reg['token']}"}

    def fresh_order(pack="p1000"):
        o = c.post("/api/points/orders", json={"pack_id": pack}, headers=h).json()["order"]
        return o

    base = balance(c, h)
    print(f"初始积分: {base}（含注册赠送）")

    # ── ① 正常付款 ─────────────────────────────────────────
    print("\n① 正常付款（金额一致）")
    o1 = fresh_order()
    r = c.get("/api/pay/notify/mpay", params=notify_body(o1["out_trade_no"], "10.00"))
    check("HTTP 200", r.status_code == 200, f"status={r.status_code}")
    check("应答体是纯文本 success", r.text == "success", f"body={r.text[:80]!r}")
    b1 = balance(c, h)
    check("积分 +1000 到账", b1 == base + 1000, f"{base} → {b1}")
    st = c.get(f"/api/points/orders/{o1['order_id']}", headers=h).json()["order"]["status"]
    check("订单转 paid", st == "paid", f"status={st}")
    paid_at = c.get(f"/api/points/orders/{o1['order_id']}", headers=h).json()["order"].get("paid_at")
    check("paid_at 有值", bool(paid_at), f"{paid_at}")

    # ── ② 重推（幂等）──────────────────────────────────────
    print("\n② 同一笔重推 3 次")
    codes, bodies = [], []
    for _ in range(3):
        rr = c.get("/api/pay/notify/mpay", params=notify_body(o1["out_trade_no"], "10.00"))
        codes.append(rr.status_code); bodies.append(rr.text)
    check("每次都 200 success（让通道别再重推）",
          all(x == 200 for x in codes) and all(b == "success" for b in bodies), f"{codes}")
    b2 = balance(c, h)
    check("积分**没有**重复发放", b2 == b1, f"{b1} → {b2}")

    # ── ③ 金额被微调（ge 策略）─────────────────────────────
    print("\n③ 金额被微调：下单 10.00，回调 10.03")
    o3 = fresh_order()
    r = c.get("/api/pay/notify/mpay", params=notify_body(o3["out_trade_no"], "10.03"))
    check("HTTP 200 success（ge 放行）", r.status_code == 200 and r.text == "success",
          f"status={r.status_code} body={r.text[:60]!r}")
    b3 = balance(c, h)
    check("按**订单快照**发 1000（不是按多付的钱）", b3 == b2 + 1000, f"{b2} → {b3}")

    # ── ④ 少付 ─────────────────────────────────────────────
    print("\n④ 少付：下单 10.00，回调 0.01")
    o4 = fresh_order()
    r = c.get("/api/pay/notify/mpay", params=notify_body(o4["out_trade_no"], "0.01"))
    check("被拒收（非 200 success）", not (r.status_code == 200 and r.text == "success"),
          f"status={r.status_code} body={r.text[:60]!r}")
    b4 = balance(c, h)
    check("没有入账", b4 == b3, f"{b3} → {b4}")
    st4 = c.get(f"/api/points/orders/{o4['order_id']}", headers=h).json()["order"]["status"]
    check("订单仍为 pending", st4 == "pending", f"status={st4}")

    # ── ⑤ 签名篡改 ─────────────────────────────────────────
    print("\n⑤ 签名错误")
    o5 = fresh_order()
    r = c.get("/api/pay/notify/mpay", params=notify_body(o5["out_trade_no"], "10.00", sign_ok=False))
    check("401 拒绝", r.status_code == 401, f"status={r.status_code}")
    check("应答体 fail", r.text == "fail", f"body={r.text[:60]!r}")
    check("没有入账", balance(c, h) == b4, f"{b4} → {balance(c, h)}")

    # ── ⑥ 非成功状态 ───────────────────────────────────────
    print("\n⑥ 非成功状态 TRADE_CLOSED")
    o6 = fresh_order()
    r = c.get("/api/pay/notify/mpay", params=notify_body(o6["out_trade_no"], "10.00",
                                                        trade_status="TRADE_CLOSED"))
    check("200 success（确认收到、避免无意义重推）",
          r.status_code == 200 and r.text == "success", f"status={r.status_code}")
    check("但**不**入账", balance(c, h) == b4, f"{b4} → {balance(c, h)}")

    # ── ⑦ POST 载体 ────────────────────────────────────────
    print("\n⑦ POST 载体（手工重放用）")
    o7 = fresh_order()
    body = notify_body(o7["out_trade_no"], "10.00", trade_no="T2026101400007")
    r = c.post("/api/pay/notify/mpay", data=body)
    check("POST 也受理且 200 success", r.status_code == 200 and r.text == "success",
          f"status={r.status_code} body={r.text[:60]!r}")
    check("积分到账", balance(c, h) == b4 + 1000, f"{b4} → {balance(c, h)}")

    # ── ⑧ 出码端点真实打 mpay ──────────────────────────────
    print("\n⑧ 出码端点 POST /api/points/orders/{id}/pay（真打 mpay）")
    o8 = fresh_order()
    r = c.post(f"/api/points/orders/{o8['order_id']}/pay", headers=h)
    print(f"  HTTP {r.status_code}  body={json.dumps(r.json(), ensure_ascii=False)[:300]}")
    check("走到 mpay 并把下游报错翻译回来了（不是 500 / 不是静默成功）",
          r.status_code in (400, 401, 403, 502, 503) and "mpay" in r.text,
          f"status={r.status_code}")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
