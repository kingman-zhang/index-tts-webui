#!/usr/bin/env python3
"""队列「终态分页」的单测（2026-10-08）。

背景：队列没有条数上限、也没有自动清理，线上实测 240 条**全部是终态**；
每次轮询全量返回 ⇒ 历史任务反复挤占当前任务的带宽（实测同一响应的总耗时在
0.099s ↔ 8.68s 之间跳，TTFB 只有 63ms ⇒ 瓶颈是源站出口带宽，不是后端）。

本文件锁的行为：
  1. 终态按 **kind 分桶**各留最近 terminal_limit 条 —— 不是全局截断。
     全局截断会让 created_at 较新的那个 tab 把另一个 tab 的历史整片挤掉。
  2. **活跃任务（queued/running/syncing/paused）永不截断** —— 截掉它们用户就看不见
     自己的队列了；而且排队任务的拖拽/删除都依赖它在列表里。
  3. has_more / terminal_total 正确，供前端「加载更多」。
  4. 截断不能变成越权通道（terminal_total 只数自己的）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-queue-term-limit-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"
os.environ["MEMBER_ENFORCE"] = "1"
os.environ["MEMBER_POINTS_PER_1000_CHARS"] = "10"
os.environ["MEMBER_REG_BONUS"] = "5000"
os.environ["MEMBER_MIN_CHARGE"] = "1"
os.environ["TTS_URL"] = "http://127.0.0.1:59999"

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
    return None


_ORIG_PROCESS_QUEUE = queue_routes.process_queue
queue_routes.process_queue = _noop_process_queue

S = qs.QueueTaskStatus
TERMINAL = {"success", "failed", "cancelled", "interrupted"}


def submit(name: str, kind: str, auth: dict) -> str:
    r = client.post("/api/queue/submit", json={
        "project_name": name,
        "kind": kind,
        "lines": [{"speaker": "A", "text": "字" * 40, "emotion": {"mode": 0}}],
        "voices": {}, "silence": {}, "params": {}, "glossary_enabled": False,
    }, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def finish(task_id: str, ts: str) -> None:
    """置为终态并**显式写入 created_at** —— 同一秒内 submit 会撞时间，
    排序就不可复现了。"""
    t = qs.queue_tasks[task_id]
    t["status"] = S.SUCCESS
    t["created_at"] = ts
    qs.persist_task(task_id)


def get(auth: dict, **params) -> dict:
    r = client.get("/api/queue", headers=auth, params=params or None)
    assert r.status_code == 200, r.text
    return r.json()


def term_ids(body: dict, kind: str | None = None) -> list:
    return [t["id"] for t in body["tasks"]
            if t["status"] in TERMINAL and (kind is None or t.get("kind") == kind)]


def main():
    u1 = service.register("分页用户一", "pass123")["token"]
    u2 = service.register("分页用户二", "pass123")["token"]
    a1 = {"Authorization": f"Bearer {u1}"}
    a2 = {"Authorization": f"Bearer {u2}"}

    print("── 构造：30 mono 终态 + 30 podcast 终态 + 3 活跃 ──")
    mono = [submit(f"配音-{i}", "mono", a1) for i in range(30)]
    pod = [submit(f"播客-{i}", "podcast", a1) for i in range(30)]
    for i, tid in enumerate(mono):
        finish(tid, f"2026-10-08T12:{i:02d}:00")   # 递增 ⇒ 最后一个是 mono[-1]
    for i, tid in enumerate(pod):
        finish(tid, f"2026-10-08T13:{i:02d}:00")   # 整体比 mono 新
    q_id = submit("排队中", "mono", a1)
    r_id = submit("运行中", "podcast", a1)
    qs.queue_tasks[r_id]["status"] = S.RUNNING
    pa_id = submit("已暂停", "mono", a1)
    qs.queue_tasks[pa_id]["status"] = S.PAUSED
    active = {q_id, r_id, pa_id}
    check("构造就绪：60 条终态 + 3 条活跃", len(qs.queue_tasks) == 63, str(len(qs.queue_tasks)))

    print("── 默认 limit=50：本次每类只有 30 条 ⇒ 全量返回 ──")
    body = get(a1)
    check("terminal_total = 60", body["terminal_total"] == 60, str(body["terminal_total"]))
    check("has_more = False（30 < 50，没有截断）", body["has_more"] is False, str(body["has_more"]))
    check("活跃 3 条全在", active <= {t["id"] for t in body["tasks"]})

    print("── limit=10：按 kind 分桶各留 10 条 ──")
    body = get(a1, terminal_limit=10)
    m_term, p_term = term_ids(body, "mono"), term_ids(body, "podcast")
    check("mono 终态 = 10 条", len(m_term) == 10, str(len(m_term)))
    check("podcast 终态 = 10 条", len(p_term) == 10, str(len(p_term)))
    # 若写成全局截断，created_at 较新的 podcast 会占满 10 条 ⇒ mono 一条不剩。
    check("⚠️ 是分桶不是全局截断：两类历史都还在", len(m_term) == 10 and len(p_term) == 10,
          f"mono={len(m_term)} podcast={len(p_term)}")
    check("mono 保留的是**最近** 10 条（不是最早 10 条）",
          set(m_term) == set(mono[-10:]) and set(m_term) != set(mono[:10]),
          str(sorted(set(m_term) ^ set(mono[-10:]))[:3]))
    check("活跃任务不受 limit 影响，3 条全在",
          active <= {t["id"] for t in body["tasks"]})
    check("has_more = True（60 > 已返回 20）", body["has_more"] is True, str(body["has_more"]))
    check("terminal_total 仍报截断前的 60", body["terminal_total"] == 60, str(body["terminal_total"]))

    print("── limit=0：只回活跃任务 ──")
    body = get(a1, terminal_limit=0)
    check("不返回任何终态", not term_ids(body), str(term_ids(body))[:80])
    check("活跃 3 条仍在", active <= {t["id"] for t in body["tasks"]})
    check("has_more = True", body["has_more"] is True)

    print("── limit=500（上限）：全部返回 ──")
    body = get(a1, terminal_limit=500)
    check("60 条终态全回", len(term_ids(body)) == 60, str(len(term_ids(body))))
    check("has_more = False", body["has_more"] is False)

    print("── 超上限应被拒（否则 limit=99999 等于退回全量）──")
    r = client.get("/api/queue", headers=a1, params={"terminal_limit": 501})
    check("limit=501 ⇒ 422", r.status_code == 422, str(r.status_code))

    print("── 隔离：截断不能变成越权通道 ──")
    other = submit("别人的任务", "mono", a2)
    finish(other, "2026-10-08T14:00:00")   # 比我的都新
    body = get(a1, terminal_limit=500)
    check("用户一看不到用户二的任务", other not in {t["id"] for t in body["tasks"]})
    check("terminal_total 只数自己的 = 60", body["terminal_total"] == 60, str(body["terminal_total"]))
    body2 = get(a2, terminal_limit=500)
    check("用户二只看到自己的 1 条", body2["terminal_total"] == 1, str(body2["terminal_total"]))

    queue_routes.process_queue = _ORIG_PROCESS_QUEUE
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        queue_routes.process_queue = _ORIG_PROCESS_QUEUE
        shutil.rmtree(TMP, ignore_errors=True)
