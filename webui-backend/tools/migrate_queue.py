#!/usr/bin/env python3
"""把**文件后端**的任务队列灌进 MongoDB（B1 队列上库的一次性迁移）。

什么时候必须跑
--------------
`QUEUE_STORE` 从 `file` 切到 `mongo` 之前跑一次 —— 否则切换后队列读到的是
**空的** `queue_tasks`（用户表现为「任务全没了」，而且正在排队的任务再也不会被执行）。

迁什么
------
| 源（`QUEUE_DIR` 下）        | 目标集合      | `_id`     |
|---|---|---|
| `queue/*.json`（跳过 `_queue_order.json`） | `queue_tasks` | `task_id` |

⚠️ **不迁** `queue/bak_001~006/` 之下的历史快照目录（它们不是任务文件，
   `load_all_tasks()` 只扫本层的 `*.json`）—— 留在磁盘不删。

顺带做的事：补 `seq`
-------------------
B1 起排队顺序改成每任务自带的 `seq` 字段（`_queue_order.json` 退役）。
老任务没有 `seq`，所以要按「`_queue_order.json` 里的顺序 → 其余按 `created_at`」
补一遍，且**从全局最大 `seq`+1 起递增**（不会与已有值撞号）。

- 迁移时**顺带**补（只影响灌进库的文档）；
- `--backfill-seq` 模式则**只**补并写回**文件**（不碰库）—— 用于「继续留在 file 模式，
  但不想让首次启动触发整队重编号」的场景。

语义
----
- **幂等**：全部 upsert（`save_task` = `replace_one(..., upsert=True)`），可反复跑。
- **只加不减**：**不删**目标库里已有、源文件里没有的任务 —— 迁移只做「补齐」。
  真要清空重灌，先 `drop` 掉 `queue_tasks` 再重跑。
- 读文件用 `queue_store_file`、写库用 `queue_store_mongo` —— 两个实现直接被调用，
  **与当前 `QUEUE_STORE` 取值无关**（迁移时它通常还是 `file`）。

用法
----
    cd webui-backend
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \\
        python tools/migrate_queue.py --dry-run        # 只统计，不写库
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \\
        python tools/migrate_queue.py                  # 真灌（幂等，可重复跑）
    ... python tools/migrate_queue.py --backfill-seq   # 只补 seq 并写回文件

退出码：0 = 成功（含 dry-run）；2 = 连不上数据库。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app import queue_store_file as fsrc  # noqa: E402
from app import queue_store_mongo as fdst  # noqa: E402
from app.config import MONGO_DB, QUEUE_DIR  # noqa: E402
from app.db import DBUnavailable, get_mongo_db  # noqa: E402


def assign_seq(tasks: list[dict], legacy_order: list[str]) -> tuple[int, int]:
    """给缺 `seq` 的任务补上（有 `seq` 的一律不动）。

    排序依据与 `queue_state.load_queue_order()` 一致：老 `_queue_order.json` 里的
    名次优先，其余按 `created_at`。新值从**全局最大 `seq` + 1** 起递增。
    返回 `(补了几个, 补之前的最大 seq)`。
    """
    hint = {tid: i for i, tid in enumerate(legacy_order)}
    base = max((t.get("seq") or 0 for t in tasks), default=0)
    missing = [t for t in tasks if t.get("seq") is None]
    missing.sort(key=lambda t: (hint.get(t.get("id"), len(hint)), t.get("created_at", "")))
    for task in missing:
        base += 1
        task["seq"] = base
    return len(missing), base - len(missing)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    backfill_only = "--backfill-seq" in sys.argv

    tasks = fsrc.load_all_tasks()
    legacy = fsrc.load_legacy_order()
    added, _ = assign_seq(tasks, legacy)

    print(f"队列迁移：文件（{QUEUE_DIR}） → {'文件（只补 seq）' if backfill_only else f'MongoDB（库 = {MONGO_DB}）'}")
    print(f"  源任务 {len(tasks)} 个；`_queue_order.json` 遗留顺序 {len(legacy)} 条；"
          f"本次补 seq {added} 个")
    queued = sum(1 for t in tasks if t.get("status") == "queued")
    print(f"  其中 queued（会被调度）{queued} 个")

    if dry_run:
        print("  （--dry-run：仅统计，未写任何东西）")
        return 0

    if backfill_only:
        for task in tasks:
            fsrc.save_task(task)
        print(f"✓ 已把 {len(tasks)} 个任务（含补好的 seq）写回文件；未碰数据库")
        return 0

    try:
        get_mongo_db().command("ping")
    except DBUnavailable as e:
        print(f"✗ 连不上 MongoDB：{e}", file=sys.stderr)
        return 2

    for task in tasks:
        fdst.save_task(task)
    print(f"✓ 已写入 queue_tasks：{len(tasks)} 个任务")
    print("  注：默认只加不减；库里已有的、文件里没有的任务**保持不动**。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
