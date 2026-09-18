#!/usr/bin/env python3
"""会员管理 CLI：直接操作 data/members/ 下的 JSON 文件（无需后端运行）。

用法（在 webui-backend 目录下，venv 环境）：
    python tools/member_admin.py add-codes --points 100 --count 10 --max-uses 1 [--expires-days 30] [--note "活动赠送"]
    python tools/member_admin.py list-codes [--limit 20]
    python tools/member_admin.py grant --user 张三 --delta 500 --reason "客服补偿"
    python tools/member_admin.py users [--query 张]
    python tools/member_admin.py disable --user 张三 [--enable]

积分换算参考：MEMBER_POINTS_PER_1000_CHARS 默认 10 积分/千字 → 100 积分 ≈ 1 万字合成。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 让 `import app` 生效（tools/ 与 webui-backend/ 同级）
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.membership import service  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="会员管理 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-codes", help="批量生成优惠码")
    p.add_argument("--points", type=int, required=True, help="单码面值（积分）")
    p.add_argument("--count", type=int, default=1, help="生成数量（1-100）")
    p.add_argument("--max-uses", type=int, default=1, help="每码可用次数")
    p.add_argument("--expires-days", type=int, default=0, help="有效天数（0=永久）")
    p.add_argument("--note", default="", help="备注")

    p = sub.add_parser("list-codes", help="查看优惠码")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("grant", help="发放/扣减积分")
    p.add_argument("--user", required=True, help="用户名或 user_id")
    p.add_argument("--delta", type=int, required=True, help="变动值（可为负）")
    p.add_argument("--reason", default="管理员调整")

    p = sub.add_parser("users", help="用户列表")
    p.add_argument("--query", default="")

    p = sub.add_parser("disable", help="禁用/启用账号")
    p.add_argument("--user", required=True)
    p.add_argument("--enable", action="store_true", help="启用（默认禁用）")

    args = parser.parse_args()

    if args.cmd == "add-codes":
        codes = service.create_redeem_codes(args.count, args.points, args.max_uses, args.expires_days, args.note)
        print(f"已生成 {len(codes)} 个优惠码（{args.points} 积分/码，限用 {args.max_uses} 次）：")
        for c in codes:
            print(f"  {c}")
    elif args.cmd == "list-codes":
        codes = service.admin_list_codes()[: args.limit]
        if not codes:
            print("暂无优惠码")
        for c in codes:
            expire = "永久" if not c.get("expires_ts") else time.strftime("%Y-%m-%d", time.localtime(c["expires_ts"]))
            state = "停用" if c.get("disabled") else ("已过期" if c.get("expired") else "有效")
            print(f"  {c['code']}  {c['points']}分  {c.get('used_count', 0)}/{c.get('max_uses', 1)}次  "
                  f"至{expire}  [{state}]  {c.get('note', '')}")
    elif args.cmd == "grant":
        result = service.admin_grant_points(args.user, args.delta, args.reason)
        print(f"{result['user']['username']} 余额现为 {result['balance']} 积分")
    elif args.cmd == "users":
        users = service.admin_list_users(args.query)
        if not users:
            print("无匹配用户")
        for u in users:
            flag = " [禁用]" if u.get("disabled") else ""
            print(f"  {u['username']:<20} {u['nickname']:<16} {u['points']:>6} 分  注册 {u.get('created_at', '')[:10]}{flag}")
    elif args.cmd == "disable":
        result = service.admin_set_disabled(args.user, not args.enable)
        print(f"{result['username']} 已{'启用' if args.enable else '禁用'}")


if __name__ == "__main__":
    main()
