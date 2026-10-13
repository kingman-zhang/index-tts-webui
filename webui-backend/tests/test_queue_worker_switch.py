#!/usr/bin/env python3
"""`QUEUE_WORKER_ENABLED=0` 的行为测试（B1 的「防多实例互踩」开关，自跑脚本）。

这个开关是 B1 里**唯一**的多实例保险，所以必须钉住它的三条语义（见
QUEUE_MIGRATION_PLAN.md §5）：

  1. `app.config.QUEUE_WORKER_ENABLED` 解析正确（"0"/"false"/"off" 都算关）；
  2. `load_persisted_tasks()` **完全不加载** —— 存储里的任务一个都不进内存
     （否则第二个实例会把对方 `running` 的任务改判 interrupted，用户再点删除
     就会把对方正在写的任务从库里删掉）；
  3. `POST /api/queue/submit` 直接 **503**（收了任务也永远没人执行）。
     顺带确认：存储里的任务**没被删**（不加载 ≠ 破坏数据）。

⚠️ 开关是在 `app.config` **import 期**读的，且 `routes/queue.py` 把它 import 成
   模块级常量 ⇒ 必须在**本文件最顶部**设好环境变量再 import。

用法：python tests/test_queue_worker_switch.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-queue-off-")
os.environ["DATA_DIR"] = TMP
os.environ["QUEUE_STORE"] = "file"
os.environ["MONGO_URL"] = ""
# ⚠️ 必须在 import app.* 之前
os.environ["QUEUE_WORKER_ENABLED"] = "0"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException  # noqa: E402

from app import queue_state as qs  # noqa: E402
from app import queue_store, queue_store_file  # noqa: E402
from app.config import QUEUE_WORKER_ENABLED  # noqa: E402
from app.models import QueueTaskModel  # noqa: E402
from app.routes.queue import submit_to_queue  # noqa: E402

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


def main():
    print("── 开关解析 ──")
    check("QUEUE_WORKER_ENABLED=0 → False", QUEUE_WORKER_ENABLED is False, repr(QUEUE_WORKER_ENABLED))

    print("── 不加载：存储里有任务，内存里必须为空 ──")
    seeded = {
        "id": "q_seed", "project_name": "别人的在跑任务", "kind": "mono",
        "lines": [{"speaker": "A", "text": "你好"}], "voices": {}, "silence": {}, "params": {},
        "glossary_enabled": True, "status": qs.QueueTaskStatus.RUNNING,
        "progress": 0.5, "created_at": "2026-10-13T10:00:00", "cancel_requested": False,
        "seq": 1,
    }
    queue_store_file.save_task(seeded)
    qs.load_persisted_tasks()
    check("内存里没有任务", qs.queue_tasks == {}, str(list(qs.queue_tasks)))
    check("排队顺序为空", qs.queue_order == [], str(qs.queue_order))
    check("存储里的任务**没被删**（不加载 ≠ 破坏数据）",
          (queue_store.load_task("q_seed") or {}).get("status") == qs.QueueTaskStatus.RUNNING,
          str(queue_store.load_task("q_seed")))
    check("存储里的任务状态也**没被改判 interrupted**",
          queue_store.load_task("q_seed")["status"] != qs.QueueTaskStatus.INTERRUPTED)

    print("── 提交直接 503 ──")
    payload = QueueTaskModel(
        project_name="不该被收下", kind="mono",
        lines=[{"speaker": "A", "text": "你好"}], voices={},
    )
    try:
        asyncio.run(submit_to_queue(payload, user=None))
        check("抛 503", False, "竟然收了任务")
    except HTTPException as e:
        check("抛 503", e.status_code == 503, f"status={e.status_code}")
        check("错误信息说清原因", "QUEUE_WORKER_ENABLED" in str(e.detail), str(e.detail))
    check("内存仍为空", qs.queue_tasks == {}, str(list(qs.queue_tasks)))

    print(f"\n{'=' * 46}\nTOTAL {PASS + FAIL}  PASS {PASS}  FAIL {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
