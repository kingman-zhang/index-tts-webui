#!/usr/bin/env python3
"""排队顺序模型（B1：`_queue_order.json` → 每任务 `seq` 字段）的语义测试（自跑脚本）。

只测 `app/queue_state.py` 里那几段顺序逻辑，不碰 HTTP、不连库：

  1. `next_seq()`：全局最大值 +1，忽略缺 `seq` 的老任务；
  2. `persist_queue_order()`：顺序没变时**零写入**（提交/重试/恢复的常规路径）；
  3. `persist_queue_order()`：乱序时**槽位置换** —— 只在这组任务占用的 seq 值之间换主人，
     其他任务的 seq 一点不动；
  4. `persist_queue_order()`：有任务缺 `seq` 时整队重编号（一次性，供老数据迁移）；
  5. `load_queue_order()`：按 `seq` 重建；缺 `seq` 的按 `_queue_order.json` → `created_at` 补队尾；
  6. 幂等：连跑两次结果一致、第二次零写入。

用法：python tests/test_queue_seq_order.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="wb-queue-seq-")
os.environ["DATA_DIR"] = TMP
os.environ["QUEUE_STORE"] = "file"
os.environ["MONGO_URL"] = ""
os.environ["QUEUE_WORKER_ENABLED"] = "1"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app import queue_state as qs  # noqa: E402
from app import queue_store  # noqa: E402
from app.config import QUEUE_ORDER_FILE  # noqa: E402

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


def reset(tasks: list[tuple[str, dict]]) -> None:
    """清空内存态并塞入给定任务（`(task_id, 附加字段)`）。"""
    qs.queue_tasks.clear()
    qs.queue_order.clear()
    for tid, extra in tasks:
        doc = {"id": tid, "status": qs.QueueTaskStatus.QUEUED, "created_at": "2026-01-01T00:00:00"}
        doc.update(extra)
        qs.queue_tasks[tid] = doc


def seqs() -> dict:
    return {tid: t.get("seq") for tid, t in qs.queue_tasks.items()}


def count_saves(fn) -> tuple[int, object]:
    """执行 fn，返回 (写盘次数, fn 的返回值)。"""
    calls: list = []
    with patch.object(queue_store, "save_task", side_effect=lambda doc: calls.append(doc["id"])):
        out = fn()
    return len(calls), out


def main():
    # ── 1. next_seq ──
    print("── next_seq ──")
    reset([])
    check("空队列 → 1", qs.next_seq() == 1, str(qs.next_seq()))
    reset([("a", {"seq": 3}), ("b", {"seq": 7})])
    check("最大值 +1", qs.next_seq() == 8, str(qs.next_seq()))
    reset([("a", {"seq": 3}), ("b", {})])
    check("忽略缺 seq 的老任务", qs.next_seq() == 4, str(qs.next_seq()))

    # ── 2. 顺序没变 ⇒ 零写入 ──
    print("── persist_queue_order：顺序没变 ⇒ 零写入 ──")
    reset([("a", {"seq": 1}), ("b", {"seq": 2})])
    qs.queue_order[:] = ["a", "b"]
    n, _ = count_saves(qs.persist_queue_order)
    check("单调递增 ⇒ 不写盘", n == 0, f"写了 {n} 次")
    check("seq 未被改动", seqs() == {"a": 1, "b": 2}, str(seqs()))

    # ── 3. 乱序 ⇒ 槽位置换 ──
    print("── persist_queue_order：乱序 ⇒ 槽位置换 ──")
    reset([("a", {"seq": 1}), ("b", {"seq": 2}), ("c", {"seq": 9})])   # c 不在队列里
    qs.queue_order[:] = ["b", "a"]
    n, _ = count_saves(qs.persist_queue_order)
    check("只写被换过的两个", n == 2, f"写了 {n} 次")
    check("槽位换主人（b=1, a=2）", seqs()["b"] == 1 and seqs()["a"] == 2, str(seqs()))
    check("不在队列里的 c 一点不动", seqs()["c"] == 9, str(seqs()))
    check("置换后单调递增", [seqs()[t] for t in qs.queue_order] == [1, 2], str(seqs()))

    print("── 置换后重跑：幂等（零写入）──")
    n, _ = count_saves(qs.persist_queue_order)
    check("再跑一次零写入", n == 0, f"写了 {n} 次")

    # ── 4. 缺 seq ⇒ 整队重编号 ──
    print("── persist_queue_order：有任务缺 seq ⇒ 整队重编号 ──")
    reset([("x", {}), ("y", {"seq": 5}), ("t", {"seq": 100})])
    qs.queue_order[:] = ["x", "y"]
    n, _ = count_saves(qs.persist_queue_order)
    check("两个都写", n == 2, f"写了 {n} 次")
    check("从全局最大值 +1 起（101 / 102）",
          seqs()["x"] == 101 and seqs()["y"] == 102, str(seqs()))
    check("组内保持 queue_order 顺序且单调", seqs()["x"] < seqs()["y"])
    check("队列外的 t 不受影响", seqs()["t"] == 100, str(seqs()))

    # ── 5. load_queue_order 按 seq 重建 ──
    print("── load_queue_order：按 seq 重建 ──")
    reset([("a", {"seq": 5}), ("b", {"seq": 1}), ("c", {"seq": 3})])
    qs.load_queue_order()
    check("按 seq 升序", qs.queue_order == ["b", "c", "a"], str(qs.queue_order))

    print("── load_queue_order：非 queued 不进队列 ──")
    reset([("a", {"seq": 1}),
           ("done", {"seq": 2, "status": qs.QueueTaskStatus.SUCCESS}),
           ("run", {"seq": 3, "status": qs.QueueTaskStatus.RUNNING})])
    qs.load_queue_order()
    check("只剩 queued", qs.queue_order == ["a"], str(qs.queue_order))

    print("── load_queue_order：缺 seq 的老任务（有 _queue_order.json）──")
    reset([("A", {"seq": 1}),
           ("B", {"created_at": "2026-01-02T00:00:00"}),   # 缺 seq，且不在遗留顺序里
           ("C", {"created_at": "2026-01-03T00:00:00"})])  # 缺 seq，但在遗留顺序里
    QUEUE_ORDER_FILE.write_text('["C"]', encoding="utf-8")
    qs.load_queue_order()
    check("有 seq 的在前；缺 seq 的按「遗留顺序 → created_at」",
          qs.queue_order == ["A", "C", "B"], str(qs.queue_order))
    check("补写后全部带 seq", all(seqs()[t] is not None for t in qs.queue_order), str(seqs()))
    n, _ = count_saves(qs.load_queue_order)
    check("再跑一次幂等（零写入）", n == 0, f"写了 {n} 次")
    check("再跑一次顺序不变", qs.queue_order == ["A", "C", "B"], str(qs.queue_order))

    print("── load_queue_order：无 queued ⇒ 空队列、不写盘 ──")
    reset([("done", {"seq": 1, "status": qs.QueueTaskStatus.SUCCESS})])
    n, _ = count_saves(qs.load_queue_order)
    check("queue_order 为空", qs.queue_order == [], str(qs.queue_order))
    check("零写入", n == 0, f"写了 {n} 次")

    # ── 6. 持久化真源：seq 真的落到后端 ──
    print("── seq 落到后端（经 queue_state）──")
    reset([("p", {"seq": 2}), ("q", {"seq": 1})])
    qs.queue_order[:] = ["p", "q"]
    qs.persist_queue_order()
    check("落库的 seq 已换位",
          queue_store.load_task("p")["seq"] == 1 and queue_store.load_task("q")["seq"] == 2,
          str({tid: queue_store.load_task(tid)["seq"] for tid in ("p", "q")}))

    print(f"\n{'=' * 46}\nTOTAL {PASS + FAIL}  PASS {PASS}  FAIL {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
