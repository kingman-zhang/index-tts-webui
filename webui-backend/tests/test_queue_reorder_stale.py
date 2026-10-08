#!/usr/bin/env python3
"""拖拽排序「尽力重排」的单测（2026-10-08）。

背景（用户线上反馈）：在单人配音页拖动任务排序，返回
400 {"detail":"任务列表与 mono 类型的排队任务不匹配"}。

根因：前端 handleDrop 提交的是**拖拽开始那一刻**的 tasks 快照，而拖拽期间
load() 被 isDraggingRef 冻结（2s 轮询首行直接 return），后端队列却在持续推进
（3 并发）。线上取证：提交 22 个 id，后端当时只有 18 个 mono 排队任务 ——
多出的 4 个（2 个已 success、2 个已 running）都是在拖拽那几分钟里推进的。
旧代码要求集合精确相等 ⇒ 必然 400，「拖得越久越容易撞上」。

本文件锁 6 件事：
  1. new_order 混入已推进（running/success）的 id 时不再 400，其余按新顺序排好
  2. new_order 漏掉某些仍在排队的任务时，它们保持原相对顺序排在其后（rest）
  3. new_order 全是过期 id（无事可做）时返回 200 且顺序不变
  4. queue_order 是全局的 ⇒ 只重排当前用户自己的槽位，别人的任务位置不动
  5. 把别人的 id 塞进 new_order 也无法借它越权重排（被忽略）
  6. kind 隔离：拖 mono 不动 podcast 的位置
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-queue-reorder-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"
os.environ["MEMBER_ENFORCE"] = "1"
os.environ["MEMBER_POINTS_PER_1000_CHARS"] = "10"
os.environ["MEMBER_REG_BONUS"] = "5000"
os.environ["MEMBER_MIN_CHARGE"] = "1"
os.environ["TTS_URL"] = "http://127.0.0.1:59999"  # 无服务，秒拒

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import queue_state as qs  # noqa: E402
from app.routes import queue as queue_routes  # noqa: E402
from app.membership import service  # noqa: E402

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


async def _noop_process_queue():
    """阻断 worker：任务停在 queued，便于手工置成各种状态。"""
    return None


_ORIG_PROCESS_QUEUE = queue_routes.process_queue
queue_routes.process_queue = _noop_process_queue

S = qs.QueueTaskStatus


def submit(name: str, kind: str, auth: dict) -> str:
    r = client.post("/api/queue/submit", json={
        "project_name": name,
        "kind": kind,
        "lines": [{"speaker": "A", "text": "字" * 40, "emotion": {"mode": 0}}],
        "voices": {}, "silence": {}, "params": {}, "glossary_enabled": False,
    }, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def set_status(task_id: str, status: str) -> None:
    """模拟 worker 推进任务（绕过 worker）。"""
    qs.queue_tasks[task_id]["status"] = status
    qs.persist_task(task_id)


def reorder(kind: str, task_ids: list, auth: dict):
    return client.patch("/api/queue/reorder",
                        json={"task_ids": task_ids, "kind": kind}, headers=auth)


def queued_of(kind: str, member_id: str = "") -> list:
    """后端当前认定的该类型排队任务，按 queue_order 顺序 —— 即「生效顺序」。

    queue_order 是全局的，传 member_id 可只看某个人的任务。
    """
    out = []
    for tid in qs.queue_order:
        t = qs.queue_tasks.get(tid)
        if not t or t.get("status") != S.QUEUED or t.get("kind") != kind:
            continue
        if member_id and t.get("member_id") != member_id:
            continue
        out.append(tid)
    return out


def main():
    u1 = service.register("排序用户一", "pass123")["token"]
    u2 = service.register("排序用户二", "pass123")["token"]
    a1 = {"Authorization": f"Bearer {u1}"}
    a2 = {"Authorization": f"Bearer {u2}"}

    print("── 场景一：复现线上 400 —— 快照里混入拖拽期间已推进的 id ──")
    mono = [submit(f"配音-{i}", "mono", a1) for i in range(1, 6)]
    m1, m2, m3, m4, m5 = mono
    uid1 = qs.queue_tasks[m1]["member_id"]
    check("准备就绪：5 个 mono 排队", queued_of("mono", uid1) == mono, str(queued_of("mono", uid1)))

    # 拖拽期间：队首先跑起来（running），一个已经完成（success）
    set_status(m1, S.RUNNING)
    set_status(m2, S.SUCCESS)
    # 前端快照仍是旧的 5 个（含 m1/m2），并把顺序拖成 m5,m4,m3,m2,m1
    r = reorder("mono", [m5, m4, m3, m2, m1], a1)
    check("不再 400（旧代码在这里必然 400）", r.status_code == 200, r.text)
    check("只对仍在排队的 3 个生效，顺序为新序", queued_of("mono", uid1) == [m5, m4, m3], str(queued_of("mono", uid1)))
    check("running 的 m1 不受影响", qs.queue_tasks[m1]["status"] == S.RUNNING)
    check("success 的 m2 不受影响", qs.queue_tasks[m2]["status"] == S.SUCCESS)

    print("── 场景二：new_order 漏掉仍在排队的任务 ⇒ 保持原相对顺序排在其后 ──")
    # 当前排队 [m5,m4,m3]，只提交 [m3,m5]（m4 未提及，模拟拖拽期间新提交/漏掉）
    r = reorder("mono", [m3, m5], a1)
    check("200", r.status_code == 200, r.text)
    check("结果 = [m3, m5, m4]", queued_of("mono", uid1) == [m3, m5, m4], str(queued_of("mono", uid1)))

    print("── 场景三：new_order 全是过期 id ⇒ 200 且顺序不变 ──")
    before = queued_of("mono", uid1)
    r = reorder("mono", [m1, m2], a1)   # 两个都已不在排队
    check("200（无事可做也不报错）", r.status_code == 200, r.text)
    check("排队顺序不变", queued_of("mono", uid1) == before, str(queued_of("mono", uid1)))

    print("── 场景四：queue_order 全局 ⇒ 别人的槽位不能动 ──")
    n1 = submit("别人的配音", "mono", a2)
    # 手工构造交错：我的 m3 / 别人的 n1 / 我的 m5, m4
    qs.queue_order[:] = [m3, n1, m5, m4]
    qs.persist_queue_order()
    check("构造就绪：全局顺序 [m3, n1, m5, m4]",
          [tid for tid in qs.queue_order] == [m3, n1, m5, m4], str(qs.queue_order))

    r = reorder("mono", [m4, m3, m5], a1)
    check("200", r.status_code == 200, r.text)
    check("我的 3 个按新序重排，n1 仍在原槽位",
          qs.queue_order == [m4, n1, m3, m5], str(qs.queue_order))

    print("── 场景五：把别人的 id 塞进 new_order ⇒ 被忽略，不越权 ──")
    r = reorder("mono", [n1, m4, m3, m5], a1)   # n1 打头，试图把它挪走
    check("200", r.status_code == 200, r.text)
    check("n1 位置纹丝不动", qs.queue_order == [m4, n1, m3, m5], str(qs.queue_order))
    check("自己的顺序也没被 n1 带偏", queued_of("mono", uid1) == [m4, m3, m5], str(queued_of("mono", uid1)))

    print("── 场景六：kind 隔离 —— 拖 mono 不动 podcast ──")
    p1 = submit("播客任务", "podcast", a1)
    before_order = list(qs.queue_order)
    p1_idx = before_order.index(p1)
    r = reorder("mono", [m5, m4, m3], a1)
    check("200", r.status_code == 200, r.text)
    check("podcast 任务下标不变", qs.queue_order.index(p1) == p1_idx,
          f"{qs.queue_order.index(p1)} != {p1_idx}")
    check("mono 已重排", queued_of("mono", uid1) == [m5, m4, m3], str(queued_of("mono", uid1)))

    print("── 场景七：podcast 分支同样可用（同一函数两半行为一致）──")
    p2 = submit("播客任务二", "podcast", a1)
    r = reorder("podcast", [p2, p1], a1)
    check("200", r.status_code == 200, r.text)
    check("podcast 顺序 = [p2, p1]", queued_of("podcast") == [p2, p1], str(queued_of("podcast")))

    queue_routes.process_queue = _ORIG_PROCESS_QUEUE
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        queue_routes.process_queue = _ORIG_PROCESS_QUEUE
        shutil.rmtree(TMP, ignore_errors=True)
