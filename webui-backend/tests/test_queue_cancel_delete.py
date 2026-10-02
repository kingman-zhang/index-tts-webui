#!/usr/bin/env python3
"""队列「取消中」卡死修复的单测（2026-10-02）。

背景（真实事故）：调用 302.ai 出错 + 进程重启后，`kind=podcast` 的任务被
`load_persisted_tasks()` 永远留成 RUNNING（旧代码按 kind 分流，认为 podcast 能
跨重启恢复；实际只有带 tts_task_id 的 tts-server 轮询任务能）。前端对
`running + cancel_requested` 显示「取消中」，而 `DELETE` 对该状态只重复设
cancel_requested ⇒ 无人检查、删不掉、积分也退不回来。

本文件锁三件事：
  1. 启动恢复判据是 `tts_task_id`，不是 `kind`
  2. `DELETE` 对「状态 running 但 running_ids 里没有」的孤儿任务直接删除（并退积分）
  3. `DELETE` 对真有执行器的运行中任务仍然只标记取消（不能强杀已付费分段）
另含排队中取消的回归断言。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-queue-cancel-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"
os.environ["MEMBER_ENFORCE"] = "1"
os.environ["MEMBER_POINTS_PER_1000_CHARS"] = "10"
os.environ["MEMBER_REG_BONUS"] = "100"
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
    """阻断 worker：任务停在 queued，便于手工构造 running 状态。"""
    return None


_ORIG_PROCESS_QUEUE = queue_routes.process_queue


def block_worker():
    queue_routes.process_queue = _noop_process_queue


def unblock_worker():
    queue_routes.process_queue = _ORIG_PROCESS_QUEUE


def make_lines(n_chars: int) -> list:
    return [{"speaker": "A", "text": "字" * n_chars, "emotion": {"mode": 0}}]


def submit(payload: dict, headers: dict):
    return client.post("/api/queue/submit", json={
        "voices": {}, "silence": {}, "params": {}, **payload,
    }, headers=headers)


def balance(token: str) -> int:
    return client.get("/api/points/balance", headers={"Authorization": f"Bearer {token}"}).json()["points"]


def load_task(task_id: str, auth: dict) -> dict:
    return client.get(f"/api/queue/{task_id}", headers=auth).json()


def write_task_file(task: dict) -> None:
    qs.queue_file(task["id"]).write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")


def make_zombie(task_id: str) -> dict:
    """把排队中的任务改成「运行中 + 已请求取消 + 无执行器」的僵尸态。"""
    task = qs.queue_tasks[task_id]
    task["status"] = qs.QueueTaskStatus.RUNNING
    task["cancel_requested"] = True
    task["message"] = "正在取消，等待进行中的合成结束"
    task.pop("tts_task_id", None)          # 关键：没有 tts_task_id
    qs.running_ids.discard(task_id)        # 关键：没有执行器在管
    qs.persist_task(task_id)
    return task


def main():
    print("── 启动恢复判据：tts_task_id 而非 kind ──")
    # 造两个磁盘上的「运行中」任务，唯一差别是有没有 tts_task_id
    orphan = {
        "id": "q_orphan_probe", "project_name": "无归属运行中", "kind": "podcast",
        "lines": [], "voices": {}, "silence": {}, "params": {},
        "glossary_enabled": False, "status": qs.QueueTaskStatus.RUNNING,
        "cancel_requested": True, "message": "正在取消，等待进行中的合成结束",
        "created_at": "2026-10-02T10:00:00", "tts_task_id": None,
    }
    polled = {
        "id": "q_poll_probe", "project_name": "遗留轮询任务", "kind": "podcast",
        "lines": [], "voices": {}, "silence": {}, "params": {},
        "glossary_enabled": False, "status": qs.QueueTaskStatus.RUNNING,
        "cancel_requested": False, "message": "运行中",
        "created_at": "2026-10-02T10:01:00", "tts_task_id": "tts_legacy_1",
    }
    write_task_file(orphan)
    write_task_file(polled)
    qs.load_persisted_tasks()

    got_orphan = qs.queue_tasks.get("q_orphan_probe", {})
    got_polled = qs.queue_tasks.get("q_poll_probe", {})
    check("无 tts_task_id 的 running → interrupted",
          got_orphan.get("status") == qs.QueueTaskStatus.INTERRUPTED, str(got_orphan.get("status")))
    check("podcast 不再被当成可跨重启恢复（旧代码会留成 running）",
          got_orphan.get("status") != qs.QueueTaskStatus.RUNNING, str(got_orphan.get("status")))
    check("有 tts_task_id 的 running 保持 running（留给 resume_polling）",
          got_polled.get("status") == qs.QueueTaskStatus.RUNNING, str(got_polled.get("status")))
    # 收尾，避免影响后面的断言
    for tid in ("q_orphan_probe", "q_poll_probe"):
        qs.queue_tasks.pop(tid, None)
        qs.delete_persisted_task(tid)

    body = service.register("队列用户", "pass123")
    token = body["token"]
    auth = {"Authorization": f"Bearer {token}"}
    check("注册送 100", body["user"]["points"] == 100, str(body["user"]))

    block_worker()
    try:
        print("── 僵尸（running 但无执行器）必须能删掉，且退积分 ──")
        r = submit({"project_name": "僵尸任务", "kind": "podcast", "lines": make_lines(2000)}, auth)
        check("提交成功", r.status_code == 200, r.text)
        zombie_id = r.json()["task_id"]
        check("预扣后余额 80", balance(token) == 80, str(balance(token)))

        zombie = make_zombie(zombie_id)
        check("僵尸含预扣额", zombie.get("points_charged") == 20, str(zombie.get("points_charged")))
        check("僵尸不在 running_ids", zombie_id not in qs.running_ids)

        r = client.delete(f"/api/queue/{zombie_id}", headers=auth)
        check("删除僵尸 200", r.status_code == 200, r.text)
        check("响应标记 orphan", r.json().get("orphan") is True, r.text)
        check("响应是 deleted 不是 cancelling", "deleted" in r.json(), r.text)
        check("内存里已移除", zombie_id not in qs.queue_tasks)
        check("磁盘文件已删除", not qs.queue_file(zombie_id).exists())
        check("僵尸删除退了积分 → 回 100", balance(token) == 100, str(balance(token)))

        r = client.get(f"/api/queue/{zombie_id}", headers=auth)
        check("再查已 404", r.status_code == 404, r.text)

        print("── 真有执行器的运行中任务：只标记取消，不强杀 ──")
        r = submit({"project_name": "在跑任务", "kind": "podcast", "lines": make_lines(2000)}, auth)
        live_id = r.json()["task_id"]
        live = qs.queue_tasks[live_id]
        live["status"] = qs.QueueTaskStatus.RUNNING
        live["cancel_requested"] = False
        qs.running_ids.add(live_id)   # 关键：有执行器
        qs.persist_task(live_id)

        r = client.delete(f"/api/queue/{live_id}", headers=auth)
        check("返回 cancelling 而非 deleted", "cancelling" in r.json(), r.text)
        check("任务仍在内存", live_id in qs.queue_tasks)
        check("任务仍在 running_ids（未误杀）", live_id in qs.running_ids)
        check("已置 cancel_requested", qs.queue_tasks[live_id].get("cancel_requested") is True)
        check("状态未被改动", qs.queue_tasks[live_id].get("status") == qs.QueueTaskStatus.RUNNING)
        check("积分未退（任务还在跑）", balance(token) == 80, str(balance(token)))

        print("── 同一任务再点一次（前端「取消中」时的删除按钮）→ 仍不误杀 ──")
        r = client.delete(f"/api/queue/{live_id}", headers=auth)
        check("再次返回 cancelling", "cancelling" in r.json(), r.text)
        check("任务依旧存在", live_id in qs.queue_tasks)

        print("── 排队中取消的回归 ──")
        r = submit({"project_name": "排队任务", "kind": "mono", "lines": make_lines(3000)}, auth)
        queued_id = r.json()["task_id"]
        check("排队任务预扣后余额 50", balance(token) == 50, str(balance(token)))
        r = client.delete(f"/api/queue/{queued_id}", headers=auth)
        check("排队中取消返回 cancelled", "cancelled" in r.json(), r.text)
        check("排队取消退款 → 回 80", balance(token) == 80, str(balance(token)))
        check("已从 queue_order 移除", queued_id not in qs.queue_order)
    finally:
        qs.running_ids.clear()
        unblock_worker()

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
