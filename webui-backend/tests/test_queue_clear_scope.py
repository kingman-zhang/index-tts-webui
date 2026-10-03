#!/usr/bin/env python3
"""「清空已完成任务」按 kind 隔离 + 终态集合的单测（2026-10-03）。

背景（用户反馈）：在双人播客页点「清空已完成任务」，把单人配音页的已完成任务
一起清掉了 —— 队列面板的 tab、统计、列表都已按 kind 分开，只有这个 DELETE 没有
传 kind，后端也就清全量。

顺带锁住同一函数里发现的第二个缺陷：终态集合写的是
(success, failed, cancelled)，漏了 interrupted。而前端「清空」按钮的可见条件用的是
`stats.success > 0 || stats.failed > 0`，其中 stats.failed 把 interrupted 也算进去
（与列表里「失败」筛选、以及单项删除按钮的终态判据一致）⇒ 只有中断任务时
按钮可见、点下去却清不掉，表现为「按钮坏了」。

本文件锁四件事：
  1. kind=podcast / kind=mono 只清各自类型
  2. 不带 kind 时清全量（旧行为回归，兼容任何未升级的调用方）
  3. interrupted 也算终态，必须被清掉
  4. 非终态（queued/paused/running）任何变体都不动它；且不越权清别人的任务
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-queue-clear-scope-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"
os.environ["MEMBER_ENFORCE"] = "1"
os.environ["MEMBER_POINTS_PER_1000_CHARS"] = "10"
os.environ["MEMBER_REG_BONUS"] = "1000"
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
    """阻断 worker：任务停在 queued，便于手工置成各种终态。"""
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


def finish(task_id: str, status: str) -> None:
    """把任务直接置成终态并落盘（绕过 worker）。"""
    task = qs.queue_tasks[task_id]
    task["status"] = status
    task["message"] = f"测试置为 {status}"
    qs.persist_task(task_id)


def ids() -> set:
    return set(qs.queue_tasks)


def main():
    u1 = service.register("清空用户一", "pass123")["token"]
    u2 = service.register("清空用户二", "pass123")["token"]
    a1 = {"Authorization": f"Bearer {u1}"}
    a2 = {"Authorization": f"Bearer {u2}"}

    print("── 用户一：四种终态 × 两种 kind，外加两个非终态 ──")
    p_success = submit("播客-成功", "podcast", a1)
    finish(p_success, S.SUCCESS)
    p_failed = submit("播客-失败", "podcast", a1)
    finish(p_failed, S.FAILED)
    p_interrupted = submit("播客-中断", "podcast", a1)
    finish(p_interrupted, S.INTERRUPTED)       # 旧代码漏掉的终态
    m_success = submit("配音-成功", "mono", a1)
    finish(m_success, S.SUCCESS)
    m_cancelled = submit("配音-已取消", "mono", a1)
    finish(m_cancelled, S.CANCELLED)
    m_interrupted = submit("配音-中断", "mono", a1)
    finish(m_interrupted, S.INTERRUPTED)
    p_queued = submit("播客-排队中", "podcast", a1)   # 非终态，任何情况都不该被清
    m_paused = submit("配音-暂停", "mono", a1)
    qs.queue_tasks[m_paused]["status"] = S.PAUSED
    qs.persist_task(m_paused)

    print("── 用户二：一个 podcast 终态（用于越权检查）──")
    other = submit("别人的播客-成功", "podcast", a2)
    finish(other, S.SUCCESS)

    before = ids()
    check("准备就绪：内存里共 9 个任务（用户一 8 + 用户二 1）", len(before) == 9, str(len(before)))

    print("── kind=podcast：只清播客的终态 ──")
    r = client.delete("/api/queue?kind=podcast", headers=a1)
    check("200", r.status_code == 200, r.text)
    check("cleared=3（成功/失败/中断）", r.json().get("cleared") == 3, r.text)

    left = ids()
    for tid, label in ((p_success, "播客-成功"), (p_failed, "播客-失败"), (p_interrupted, "播客-中断")):
        check(f"已清掉 {label}", tid not in left)
        check(f"磁盘文件已删 {label}", not qs.queue_file(tid).exists())
    for tid, label in ((m_success, "配音-成功"), (m_cancelled, "配音-已取消"), (m_interrupted, "配音-中断")):
        check(f"保留 {label}（kind 隔离）", tid in left)
    check("保留 播客-排队中（非终态）", p_queued in left)
    check("保留 配音-暂停（非终态）", m_paused in left)
    check("保留 别人的播客-成功（不越权）", other in left)

    print("── kind=mono：只清配音的终态 ──")
    r = client.delete("/api/queue?kind=mono", headers=a1)
    check("cleared=3（成功/取消/中断）", r.json().get("cleared") == 3, r.text)
    left = ids()
    for tid, label in ((m_success, "配音-成功"), (m_cancelled, "配音-已取消"), (m_interrupted, "配音-中断")):
        check(f"已清掉 {label}", tid not in left)
    check("保留 播客-排队中（非终态）", p_queued in left)
    check("保留 配音-暂停（非终态）", m_paused in left)
    check("保留 别人的播客-成功", other in left)

    print("── 再点一次 kind=podcast：无可清时 cleared=0，不报错 ──")
    r = client.delete("/api/queue?kind=podcast", headers=a1)
    check("cleared=0", r.json().get("cleared") == 0, r.text)
    check("非终态仍在", p_queued in ids() and m_paused in ids())

    print("── 不带 kind：清全量（旧行为回归）──")
    # 补两个终态，跨两种 kind 各一
    extra_p = submit("补-播客终态", "podcast", a1)
    finish(extra_p, S.SUCCESS)
    extra_m = submit("补-配音终态", "mono", a1)
    finish(extra_m, S.CANCELLED)
    # 用户二也补一个，用来验证不带 kind 时的越权边界
    other2 = submit("别人的播客-终态2", "podcast", a2)
    finish(other2, S.SUCCESS)

    r = client.delete("/api/queue", headers=a1)
    check("不带 kind 时 cleared=2（两个自己的终态）", r.json().get("cleared") == 2, r.text)
    left = ids()
    check("自己的两个终态已清", extra_p not in left and extra_m not in left)
    check("自己的非终态仍在", p_queued in left and m_paused in left)
    check("别人的终态仍在（隔离不受 kind 缺省影响）", other in left and other2 in left)

    print("── 用户二自查：自己的终态还在 ──")
    r = client.get("/api/queue", headers=a2)
    mine = {t["id"] for t in r.json()["tasks"]}
    check("用户二能看到自己的两个终态", {other, other2} <= mine, str(mine))

    queue_routes.process_queue = _ORIG_PROCESS_QUEUE
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        queue_routes.process_queue = _ORIG_PROCESS_QUEUE
        shutil.rmtree(TMP, ignore_errors=True)
