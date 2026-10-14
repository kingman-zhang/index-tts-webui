#!/usr/bin/env python3
"""mpay 通道联调冒烟脚本（**会真的在 mpay 侧生成待付订单**，但不会付款）。

在 webui-backend 目录下跑（会读同目录的真实 `.env`）：
    cd webui-backend
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
        ~/.workbuddy/binaries/python/envs/default/bin/python ../outputs/mpay-smoke.py

常用覆盖（**不改 .env**，只影响本次进程；真实环境变量优先于 .env）：
    --pid 1001                     试不同商户 ID
    --public-base https://xxx.com  试不同的回调根地址（注意：**只填根，不要带路径**）
    --cents 1                      下单金额（分）
    --raw-only                     只裸打接口，不走本项目模块

做三件事，逐层贴近真实链路：
  ① 打印生效配置 + 组装出的 notify_url（并检查有没有重复拼接）
  ② **裸打** /mapi.php（不走本项目代码）—— 看 mpay 原始返回，确认字段名
  ③ 走本项目 pay_mpay.create_charge() —— 确认解析逻辑与原始返回一致

刻意**不**碰本地账本（不建用户、不建订单），对 data/ 零副作用。
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "webui-backend"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cents", type=int, default=1, help="下单金额（分），默认 1（0.01 元）")
    ap.add_argument("--pid", default=None, help="覆盖 MPAY_PID")
    ap.add_argument("--key", default=None, help="覆盖 MPAY_KEY")
    ap.add_argument("--public-base", default=None, help="覆盖 PAY_PUBLIC_BASE_URL（只填根地址）")
    ap.add_argument("--return-url", default=None, help="覆盖 PAY_RETURN_URL")
    ap.add_argument("--raw-only", action="store_true", help="只做 ①②，不走本项目模块")
    return ap.parse_args()


def apply_overrides(a: argparse.Namespace) -> None:
    """必须在 import app.* 之前调用：config._load_dotenv 是 setdefault 语义，
    真实环境变量优先，所以这里先写 os.environ 就能压过 .env。"""
    pairs = [("MPAY_PID", a.pid), ("MPAY_KEY", a.key),
             ("PAY_PUBLIC_BASE_URL", a.public_base), ("PAY_RETURN_URL", a.return_url)]
    for k, v in pairs:
        if v is not None:
            os.environ[k] = v
            print(f"  [覆盖] {k} = {v if 'KEY' not in k else '(已设置)'}")


def show_conf(pay_mod, pay_mpay, service) -> dict:
    print("── ① 生效配置 ─────────────────────────────────────")
    c = pay_mpay._conf()
    print(f"  自检            : {json.dumps(pay_mod.describe(), ensure_ascii=False)}")
    print(f"  mpay base_url   : {c['base_url']}")
    print(f"  pid             : {c['pid']!r}  {'⚠️ 非纯数字，mpay 要求整数' if not str(c['pid']).isdigit() else '✓ 纯数字'}")
    print(f"  key             : {'已配置 len=%d' % len(c['key']) if c['key'] else '**未配置**'}")
    print(f"  type（收款方式）: {c['type']}")
    print(f"  PAY_PUBLIC_BASE_URL : {service.PAY_PUBLIC_BASE_URL!r}")
    notify = f"{service.PAY_PUBLIC_BASE_URL}/api/pay/notify/{pay_mpay.PROVIDER_ID}"
    print(f"  ⇒ notify_url    : {notify}")
    dup = notify.count("/api/pay/notify/")
    print(f"  ⇒ 路径段 '/api/pay/notify/' 出现 {dup} 次 {'⚠️ 重复拼接！' if dup > 1 else '✓'}")
    print(f"  PAY_RETURN_URL  : {service.PAY_RETURN_URL!r}")
    print(f"  金额策略        : {pay_mod.resolve_amount_policy(pay_mpay)}")
    print()
    return {"notify": notify, "conf": c}


def raw_call(pay_mpay, service, conf: dict, notify: str, cents: int) -> dict | None:
    """裸打 /mapi.php：完全照 ePay V1 规则拼参，不经过本项目解析。"""
    print("── ② 裸打 /mapi.php（不经本项目代码）───────────────")
    import httpx

    params = {
        "pid": conf["pid"],
        "type": conf["type"],
        "out_trade_no": f"smoke{int(time.time())}",
        "notify_url": notify,
        "return_url": service.PAY_RETURN_URL,
        "name": "积分充值",
        "money": f"{cents / 100:.2f}",
        "clientip": "0.0.0.0",
        "device": "pc",
        "sign_type": "MD5",
    }
    params["sign"] = pay_mpay.sign_v1(params, conf["key"])
    print(f"  请求 {conf['base_url']}/mapi.php")
    print(f"  参数（sign 脱敏）: { {k: v for k, v in params.items() if k != 'sign'} }")
    try:
        with httpx.Client(timeout=20.0, trust_env=False) as cli:
            r = cli.post(conf["base_url"] + "/mapi.php", data=params)
    except httpx.HTTPError as e:
        print(f"  ✗ 请求失败：{e!r}")
        return None
    print(f"  HTTP {r.status_code}  content-type={r.headers.get('content-type')!r}")
    print(f"  原始响应体: {r.text[:600]}")
    try:
        js = r.json()
    except ValueError:
        print("  ✗ 不是 JSON")
        return None
    print(f"  code={js.get('code')!r}  msg={js.get('msg')!r}")
    for f in ("qrcode", "payurl", "urlscheme", "trade_no", "url", "identity_required"):
        if js.get(f):
            print(f"  返回字段 {f!r} = {str(js[f])[:160]}")
    print()
    return js


async def via_module(pay_mpay, order: dict, ctx: dict) -> None:
    print("── ③ 走本项目 pay_mpay.create_charge() ─────────────")
    try:
        out = await pay_mpay.create_charge(order, ctx)
        print(f"  ✓ 成功: {json.dumps(out, ensure_ascii=False)[:400]}")
    except Exception as e:
        print(f"  ✗ {type(e).__name__}: {e}")
    print()


def main() -> int:
    args = parse_args()
    print("── ⓪ 本次覆盖 ─────────────────────────────────────")
    apply_overrides(args)
    print()

    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.membership import pay_mpay, service, pay as pay_mod

    info = show_conf(pay_mod, pay_mpay, service)
    conf = info["conf"]
    if not pay_mpay.configured():
        print("配置不全，终止：", pay_mpay.config_problem())
        return 2

    if raw_call(pay_mpay, service, conf, info["notify"], args.cents) is None:
        return 1

    if args.raw_only:
        return 0

    tag = f"smoke{int(time.time())}"
    order = {"order_id": tag, "out_trade_no": tag,
             "amount_fen": args.cents, "pack_name": "积分充值"}
    ctx = {"notify_url": info["notify"], "return_url": service.PAY_RETURN_URL,
           "clientip": "0.0.0.0", "device": "pc"}
    asyncio.run(via_module(pay_mpay, order, ctx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
