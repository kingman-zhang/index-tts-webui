#!/usr/bin/env python3
"""一次性迁移脚本：把无主（owner_id/member_id 为空）的遗留数据划归指定用户。

背景：2026-09-21 数据隔离上线后，MEMBER_ENFORCE=1 / MEMBER_REQUIRE_LOGIN=1
环境下登录用户只能看到自己的项目存档与队列任务；无主遗留数据（隔离上线前
产生的项目、队列任务、本地上传音色）对登录用户不可见。用本脚本把它们划归
指定用户，避免"老项目找不到了"。

用法（在 webui-backend 目录下）：
    python tools/migrate_owners.py <用户名>
    python tools/migrate_owners.py diamondiamon        # 例：划归给某用户
    python tools/migrate_owners.py <用户名> --dry-run  # 只看会改什么，不落盘

范围：
  - data/projects/*.json           owner_id 为空的项目
  - data/queue/*.json              member_id 为空的任务
  - data/voices_meta.json          本地上传音色补归属记录（可 --include-voices-meta）
"""

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"


def find_user_id(username: str) -> str:
    users_file = DATA_DIR / "members" / "users.json"
    if not users_file.exists():
        sys.exit(f"找不到用户库: {users_file}")
    users = json.loads(users_file.read_text(encoding="utf-8"))
    for uid, u in users.items():
        if u.get("username") == username:
            print(f"找到用户: {username} -> {uid}")
            return uid
    sys.exit(f"用户不存在: {username}（现有: {[u.get('username') for u in users.values()]}）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("username", help="目标用户名")
    parser.add_argument("--dry-run", action="store_true", help="只显示将修改的文件，不落盘")
    parser.add_argument("--include-voices-meta", action="store_true",
                        help="同时把 data/voices/ 下无归属记录的音色文件补记给该用户")
    args = parser.parse_args()
    uid = find_user_id(args.username)
    changed = 0

    # 项目存档
    projects_dir = DATA_DIR / "projects"
    if projects_dir.exists():
        for f in sorted(projects_dir.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("owner_id"):
                continue
            print(f"[project] {f.name} -> {uid}")
            if not args.dry_run:
                data["owner_id"] = uid
                f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            changed += 1

    # 队列任务
    queue_dir = DATA_DIR / "queue"
    if queue_dir.exists():
        for f in sorted(queue_dir.glob("*.json")):
            if f.name == "_queue_order.json":
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("member_id"):
                continue
            print(f"[queue]    {f.name} -> {uid}")
            if not args.dry_run:
                data["member_id"] = uid
                f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            changed += 1

    # 本地上传音色归属
    voices_dir = DATA_DIR / "voices"
    meta_path = DATA_DIR / "voices_meta.json"
    if args.include_voices_meta and voices_dir.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        for f in sorted(voices_dir.iterdir()):
            if not f.is_file() or f.name in meta:
                continue
            print(f"[voice]    {f.name} -> {uid}")
            if not args.dry_run:
                meta[f.name] = {"owner_id": uid, "uploaded_at": "migrated"}
            changed += 1
        if not args.dry_run:
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'[dry-run] ' if args.dry_run else ''}共 {changed} 项"
          f"{'（未落盘）' if args.dry_run else '已划归 ' + uid}")


if __name__ == "__main__":
    main()
