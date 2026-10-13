#!/usr/bin/env python3
"""队列「按 kind 过滤」的单测（2026-10-10）。

背景：页面已按类型拆分（`/podcast` = 双人播客、`/dubbing` = 单人配音），队列面板
只展示本页类型；但 `GET /api/queue` 原先**总是**把两种类型（含各自的终态历史）
一起返回 ⇒ 每次轮询都白传另一半。列表接口唯一的消费方就是当前页的 QueuePanel。

本文件锁的行为：
  1. 带 kind ⇒ 只返回该类型（活跃 + 终态），另一种类型一条都不出现。
  2. terminal_total / has_more / queued / queue_order 都只算该类型。
  3. current：正在合成的若是另一类型，则对本页返回 null。
  4. 不带 kind ⇒ 行为与旧版完全一致（全部类型），保证向后兼容。
  5. kind 过滤不削弱数据隔离（依然只看得到自己的）。
  6. 未知 kind 值 ⇒ 空列表，不报错。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-queue-kind-scope-test-")
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
            if t["status"] in TERMINAL and (kind is None or (t.get("kind") or "podcast") == kind)]


def main():
    u1 = service.register("类型用户一", "pass123")["token"]
    a1 = {"Authorization": f"Bearer {u1}"}

    print("── 构造：3 mono 终态 + 2 podcast 终态 + 各 1 排队 + 1 mono 运行 ──")
    mono = [submit(f"配音-{i}", "mono", a1) for i in range(3)]
    pod = [submit(f"播客-{i}", "podcast", a1) for i in range(2)]
    for i, tid in enumerate(mono):
        finish(tid, f"2026-10-10T12:{i:02d}:00")
    for i, tid in enumerate(pod):
        finish(tid, f"2026-10-10T13:{i:02d}:00")
    mono_q = submit("配音排队", "mono", a1)
    pod_q = submit("播客排队", "podcast", a1)
    mono_r = submit("配音运行", "mono", a1)
    qs.queue_tasks[mono_r]["status"] = S.RUNNING
    qs.current_task_id = mono_r
    check("构造就绪：8 条任务", len(qs.queue_tasks) == 8, str(len(qs.queue_tasks)))

    print("── ?kind=mono：只回配音任务 ──")
    m = get(a1, kind="mono")
    ids = {t["id"] for t in m["tasks"]}
    check("返回的每一条都是 mono",
          all((t.get("kind") or "podcast") == "mono" for t in m["tasks"]),
          str(sorted({t.get("kind") for t in m["tasks"]})))
    check("不含任何 podcast 任务（2 终态 + 1 排队）",
          not ({pod_q} | set(pod)) & ids)
    check("3 条 mono 终态全在 + 排队/运行也在",
          set(mono) <= ids and {mono_q, mono_r} <= ids)
    check("terminal_total 只数 mono = 3", m["terminal_total"] == 3, str(m["terminal_total"]))
    check("has_more = False（3 < 50，未截断）", m["has_more"] is False, str(m["has_more"]))
    check("queued 只数 mono = 1", m["queued"] == 1, str(m["queued"]))
    check("queue_order 只含 mono 排队", m["queue_order"] == [mono_q], str(m["queue_order"]))
    check("current = 正在跑的 mono 任务", m["current"] == mono_r, str(m["current"]))

    print("── ?kind=podcast：只回播客任务 ──")
    p = get(a1, kind="podcast")
    pids = {t["id"] for t in p["tasks"]}
    check("返回的每一条都是 podcast",
          all((t.get("kind") or "podcast") == "podcast" for t in p["tasks"]))
    check("不含任何 mono 任务", not ({mono_q, mono_r} | set(mono)) & pids)
    check("terminal_total 只数 podcast = 2", p["terminal_total"] == 2, str(p["terminal_total"]))
    check("queued 只数 podcast = 1", p["queued"] == 1, str(p["queued"]))
    check("queue_order 只含 podcast 排队", p["queue_order"] == [pod_q], str(p["queue_order"]))
    check("⚠️ current 属另一类型 ⇒ 播客页应返回 null", p["current"] is None, str(p["current"]))

    print("── 不带 kind：与旧版一致（两种都回）──")
    allb = get(a1)
    aids = {t["id"] for t in allb["tasks"]}
    check("mono 与 podcast 都在", set(mono) <= aids and set(pod) <= aids)
    check("terminal_total = 5", allb["terminal_total"] == 5, str(allb["terminal_total"]))
    check("queued = 2", allb["queued"] == 2, str(allb["queued"]))
    check("current 仍报 mono_r", allb["current"] == mono_r, str(allb["current"]))

    print("── kind × terminal_limit：截断只作用于该类型 ──")
    m1 = get(a1, kind="mono", terminal_limit=1)
    check("mono limit=1 ⇒ 只回最近 1 条终态", len(term_ids(m1, "mono")) == 1, str(term_ids(m1)))
    check("terminal_total 仍报截断前的 3", m1["terminal_total"] == 3, str(m1["terminal_total"]))
    check("has_more = True", m1["has_more"] is True, str(m1["has_more"]))
    check("活跃任务不受 limit 影响", {mono_q, mono_r} <= {t["id"] for t in m1["tasks"]})

    print("── 隔离：kind 过滤不削弱越权防护 ──")
    u2 = service.register("类型用户二", "pass123")["token"]
    a2 = {"Authorization": f"Bearer {u2}"}
    other = submit("别人的配音", "mono", a2)
    finish(other, "2026-10-10T14:00:00")
    mo = get(a1, kind="mono")
    check("用户一看不到用户二的 mono 任务", other not in {t["id"] for t in mo["tasks"]})
    check("用户一 mono terminal_total 仍 = 3", mo["terminal_total"] == 3, str(mo["terminal_total"]))
    mo2 = get(a2, kind="mono")
    check("用户二只看到自己的 1 条", mo2["terminal_total"] == 1, str(mo2["terminal_total"]))

    print("── 未知 kind：空列表，不报错 ──")
    x = get(a1, kind="nope")
    check("tasks 为空", x["tasks"] == [], str(x["tasks"])[:60])
    check("terminal_total = 0", x["terminal_total"] == 0, str(x["terminal_total"]))
    check("has_more = False", x["has_more"] is False)

    queue_routes.process_queue = _ORIG_PROCESS_QUEUE
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        queue_routes.process_queue = _ORIG_PROCESS_QUEUE
        shutil.rmtree(TMP, ignore_errors=True)
