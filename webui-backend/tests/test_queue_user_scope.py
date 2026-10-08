#!/usr/bin/env python3
"""队列「用户隔离」的单测（2026-10-08）。

背景（用户要求）：任务队列要做用户隔离，排序也要。

已经隔离的（本文件回归锁定）：
  - /api/queue 的 tasks 列表（各状态都按 member_id 过滤）
  - bulk-pause / bulk-resume / clear / reorder（见各自测试文件）

本文件锁的是**响应字段层的全局残留** —— 它们不含别人任务名，但会把别人的
task_id 直接吐给前端：
  1. queue_order 字段：原来返回全局队列（含别人的 id），现按成员过滤
  2. queued 字段：原来是全局计数（len(queue_order)），现只数自己的
  3. current 字段：原来是全局「正在合成的任务 id」，若是别人的则为 null
  4. paused 列表：它是在主循环外**重新遍历全表**收集的，主循环那条 continue
     管不到它 ⇒ 原来会把别人已暂停的任务整条塞进你的列表（越权泄露）
  5. 响应体瘦身（2026-10-08 晚）：列表不再返回 lines/params/silence/voices
     （线上 240 条任务时 lines 占 945 KB / 1.16 MB），但必须**浅拷贝后剔除** ——
     这些 dict 就是 qs.queue_tasks 里的实况对象，直接 pop 会毁掉内存任务。
另锁一个语义事实：queue_position 仍是**全局位次**（真实等待位次，不含别人信息）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-queue-user-scope-test-")
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
    qs.queue_tasks[task_id]["status"] = status
    qs.persist_task(task_id)


def main():
    u1 = service.register("隔离用户一", "pass123")["token"]
    u2 = service.register("隔离用户二", "pass123")["token"]
    a1 = {"Authorization": f"Bearer {u1}"}
    a2 = {"Authorization": f"Bearer {u2}"}

    print("── 构造：两人任务交错在同一条全局 queue_order 里 ──")
    m1 = submit("我-配音1", "mono", a1)
    m2 = submit("我-配音2", "mono", a1)
    p1 = submit("我-播客1", "podcast", a1)
    n1 = submit("别人-配音1", "mono", a2)
    n2 = submit("别人-播客1", "podcast", a2)
    qs.queue_order[:] = [m1, n1, m2, p1, n2]   # 全局执行序：我的和别人的交错
    qs.persist_queue_order()
    check("构造就绪：全局 5 个排队", len(qs.queue_order) == 5, str(qs.queue_order))

    print("── 用户一 GET /api/queue：三类字段都只能看到自己的 ──")
    r = client.get("/api/queue", headers=a1)
    body = r.json()
    got = {t["id"] for t in body["tasks"]}
    check("tasks 只含自己的 3 个", got == {m1, m2, p1}, str(got))
    check("不含别人的任何任务", n1 not in got and n2 not in got)
    check("queue_order 只含自己的排队 id（且保持全局相对序）",
          body["queue_order"] == [m1, m2, p1], str(body["queue_order"]))
    check("queued 只数自己的 = 3（旧代码返回全局 5）", body["queued"] == 3, str(body["queued"]))

    print("── 用户二看自己的：同样只看到 2 个 ──")
    r2 = client.get("/api/queue", headers=a2).json()
    check("tasks 只含 n1/n2", {t["id"] for t in r2["tasks"]} == {n1, n2})
    check("queue_order = [n1, n2]", r2["queue_order"] == [n1, n2], str(r2["queue_order"]))
    check("queued = 2", r2["queued"] == 2, str(r2["queued"]))

    print("── current 字段：正在合成的若是别人的任务，不能暴露 ──")
    set_status(n1, S.RUNNING)
    qs.current_task_id = n1
    check("用户一看到 current = null", client.get("/api/queue", headers=a1).json()["current"] is None,
          str(client.get("/api/queue", headers=a1).json()["current"]))
    check("用户二看到 current = n1", client.get("/api/queue", headers=a2).json()["current"] == n1)

    set_status(m1, S.RUNNING)
    qs.current_task_id = m1
    check("换成我的任务在跑：用户一看到 current = m1（自己的）",
          client.get("/api/queue", headers=a1).json()["current"] == m1)
    check("同一时刻用户二看到 current = null（不串台）",
          client.get("/api/queue", headers=a2).json()["current"] is None)

    print("── paused 列表：主循环外重新遍历的那份曾漏过滤 ──")
    set_status(m2, S.PAUSED)          # 我的暂停
    set_status(n2, S.PAUSED)          # 别人的暂停
    r = client.get("/api/queue", headers=a1).json()
    mine = {t["id"] for t in r["tasks"]}
    check("我能看到自己暂停的任务 m2", m2 in mine, str(mine))
    check("看不到别人暂停的任务 n2（旧代码会漏进来）", n2 not in mine, str(mine))
    check("paused 也不掺进 queue_order", r["queue_order"] == [p1], str(r["queue_order"]))
    r2 = client.get("/api/queue", headers=a2).json()
    check("用户二只看到自己暂停的 n2", {t["id"] for t in r2["tasks"]} == {n1, n2},
          str({t["id"] for t in r2["tasks"]}))

    print("── queue_position 仍是全局位次（真实等待位次，不含别人 id）──")
    r = client.get("/api/queue", headers=a1).json()
    pos = {t["id"]: t.get("queue_position") for t in r["tasks"]}
    check("p1 在全局第 4 位 ⇒ position = 4（别人占着中间的位置）",
          pos[p1] == 4, str(pos))

    print("── 列表瘦身：不回传重字段，且不能破坏内存中的原对象 ──")
    # 背景：线上 240 条任务时 /api/queue 响应体 1.16 MB，其中 lines 独占 945 KB（79%），
    # 而前端 QueuePanel 从不读取这几个字段 ⇒ 列表接口不再返回它们。
    r = client.get("/api/queue", headers=a1).json()
    omitted = {"lines", "params", "silence", "voices"}
    leaked = [t["id"] for t in r["tasks"] if omitted & set(t.keys())]
    check("列表响应不含 lines/params/silence/voices", not leaked, f"泄漏: {leaked}")

    keep = {"id", "status", "project_name", "kind"}
    thin = [t["id"] for t in r["tasks"] if not keep <= set(t.keys())]
    check("前端渲染依赖的轻量字段仍在（防止误删）", not thin, f"缺字段: {thin}")

    # ⚠️ 这是本次改动最关键的回归点：tasks 里的 dict 就是 qs.queue_tasks 的实况对象，
    # 若用 pop/del 剔除字段，会毁掉内存里的任务（合成与持久化都依赖 lines）。
    live = qs.queue_tasks[p1]
    check("内存中原对象仍带 lines（浅拷贝未破坏）",
          isinstance(live.get("lines"), list) and len(live["lines"]) == 1,
          f"lines={live.get('lines')!r}")
    check("内存中原对象仍带 params/voices/silence",
          all(k in live for k in ("params", "voices", "silence")),
          str(sorted(live.keys())))

    one = client.get(f"/api/queue/{p1}", headers=a1).json()
    check("单任务接口仍返回完整字段（含 lines）", "lines" in one)

    queue_routes.process_queue = _ORIG_PROCESS_QUEUE
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        queue_routes.process_queue = _ORIG_PROCESS_QUEUE
        shutil.rmtree(TMP, ignore_errors=True)
