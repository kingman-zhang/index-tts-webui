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

顺带做的事：补 `seq` 与 `created_ts`
------------------------------------
1. **`seq`**（B1 起）：排队顺序改成每任务自带的 `seq` 字段（`_queue_order.json` 退役）。
   老任务没有 `seq`，要按「`_queue_order.json` 里的顺序 → 其余按 `created_at`」补一遍，
   且**从全局最大 `seq`+1 起递增**（不会与已有值撞号）。
   - 迁移时**顺带**补（只影响灌进库的文档）；
   - `--backfill-seq` 模式则**只**补并写回**文件**（不碰库）—— 用于「继续留在 file 模式，
     但不想让首次启动触发整队重编号」的场景。

2. **`created_ts`**（BSON Date，仅 mongo 路径）：`created_at` 是**无时区**字符串，
   能范围查、**不能按天/周聚合**，所以库里另存一个真正的时间字段供统计。
   这里把它从 `created_at` 解析出来（naive 值按本地时区解释再转 UTC）**显式写进库**，
   而不是让后端用「迁移时刻」—— 否则历史任务的时间全部塌到迁移那一天。
   落库走 `$setOnInsert`，所以反复跑迁移不会改动已有文档的时间（幂等）。

语义
----
- **幂等**：全部 upsert（`save_task` 走 `$set` / `$setOnInsert`，可反复跑）。
- **只加不减**：**不删**目标库里已有、源文件里没有的任务 —— 迁移只做「补齐」。
  真要清空重灌，先 `drop` 掉 `queue_tasks` 再重跑。
- ⚠️ **不动已软删除的记录**：`save_task` 是 `$set`（不写 `deleted_at`），所以已经被
  标记删除的任务不会因为重跑迁移而复活。
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
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app import queue_store_file as fsrc  # noqa: E402
from app import queue_store_mongo as fdst  # noqa: E402
from app.config import MONGO_DB, QUEUE_DIR  # noqa: E402
from app.db import DBUnavailable, get_mongo_db  # noqa: E402


def parse_created_ts(task: dict) -> datetime | None:
    """把 `created_at`（无时区 ISO 字符串）解析成 UTC datetime，供 mongo 的 `created_ts`。

    ⚠️ `created_at` 是**本地时间且不带时区**，所以 naive 值要按**本地时区**解释再转 UTC
    （`astimezone()` 对 naive datetime 正是这个语义）。解析不了就返回 None ——
    调用方退回「不显式写」，让后端用插入时刻（宁可时间不准，也不能让迁移失败）。
    """
    raw = task.get("created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip()).astimezone(timezone.utc)
    except Exception:
        return None


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

    # `created_ts` 只为 mongo 准备：file 后端没有这个字段，而且 datetime 写不进 JSON。
    # 幂等性靠 `$setOnInsert` —— 见 save_task。
    created_ts = {t.get("id"): parse_created_ts(t) for t in tasks}
    n_ts = sum(1 for v in created_ts.values() if v is not None)

    print(f"队列迁移：文件（{QUEUE_DIR}） → {'文件（只补 seq）' if backfill_only else f'MongoDB（库 = {MONGO_DB}）'}")
    print(f"  源任务 {len(tasks)} 个；`_queue_order.json` 遗留顺序 {len(legacy)} 条；"
          f"本次补 seq {added} 个")
    print(f"  created_ts 可从 created_at 解析 {n_ts}/{len(tasks)} 个"
          f"（其余留空，由后端用插入时刻兜底）")
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
        ts = created_ts.get(task.get("id"))
        # ⚠️ 用新 dict，不改原对象 —— `task` 来自文件，别把 datetime 混进去
        fdst.save_task({**task, "created_ts": ts} if ts else task)
    print(f"✓ 已写入 queue_tasks：{len(tasks)} 个任务")
    print("  注：默认只加不减；库里已有的、文件里没有的任务**保持不动**。")
    print("      created_ts 走 $setOnInsert ⇒ 反复跑不会改动已有记录的时间（幂等）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
