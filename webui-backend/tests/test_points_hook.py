#!/usr/bin/env python3
"""会员扣费钩子单测（MEMBER_ENFORCE=1 收费模式）。

覆盖：未登录拒绝、预扣、余额不足 402、失败退款、编辑多退少补、取消退款、退款幂等。
TTS 指向本机必然拒绝连接的端口 → 任务提交后立即失败 → 触发退款路径。

时序说明：预扣断言必须在 worker 抢跑失败退款之前完成，故用
qs.current_task_id = "blocker" 让 process_queue 空转，任务保持 queued；
失败退款用独立任务在解除阻断后测。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-hook-test-")
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

client = TestClient(app)
auth_cache: dict = {}


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


PASS = 0
FAIL = 0


def make_lines(n_chars: int) -> list:
    return [{"speaker": "A", "text": "字" * n_chars, "emotion": {"mode": 0}}]


def submit(payload: dict, headers: dict):
    return client.post("/api/queue/submit", json={
        "voices": {}, "silence": {}, "params": {}, **payload,
    }, headers=headers)


def wait_terminal(task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/api/queue/{task_id}").json()
        if task.get("status") not in ("queued", "running", "syncing"):
            return task
        time.sleep(0.2)
    return task


def balance(token: str) -> int:
    return client.get("/api/points/balance", headers={"Authorization": f"Bearer {token}"}).json()["points"]


def main():
    body = client.post("/api/auth/register", json={"username": "付费用户", "password": "pass123"}).json()
    token = body["token"]
    auth = {"Authorization": f"Bearer {token}"}
    check("注册送 100", body["user"]["points"] == 100, str(body["user"]))

    print("── 未登录拒绝 ──")
    r = submit({"project_name": "t0", "kind": "mono", "lines": make_lines(100)}, {})
    check("ENFORCE=1 未登录提交 401", r.status_code == 401, r.text)

    print("── 预扣 / 不足 / 编辑调差（阻断 worker） ──")
    qs.current_task_id = "blocker"
    try:
        r = submit({"project_name": "t1", "kind": "mono", "lines": make_lines(5000)}, auth)
        check("登录提交成功", r.status_code == 200, r.text)
        t1 = r.json()["task_id"]
        check("预扣后余额 50", balance(token) == 50, str(balance(token)))
        task = client.get(f"/api/queue/{t1}").json()
        check("任务标记 member_id", task.get("member_id") == body["user"]["user_id"])
        check("任务记录预扣额", task.get("points_charged") == 50, str(task.get("points_charged")))

        r = submit({"project_name": "t2", "kind": "mono", "lines": make_lines(6000)}, auth)
        check("余额不足 402", r.status_code == 402, r.text)
        check("被拒后余额不变 50", balance(token) == 50)

        r = client.put(f"/api/queue/{t1}", json={
            "project_name": "t1", "kind": "mono", "lines": make_lines(2000),
            "voices": {}, "silence": {}, "params": {},
        }, headers=auth)
        check("改稿成功", r.status_code == 200, r.text)
        check("退差后余额 80（50+30）", balance(token) == 80, str(balance(token)))

        r = client.put(f"/api/queue/{t1}", json={
            "project_name": "t1", "kind": "mono", "lines": make_lines(11000),
            "voices": {}, "silence": {}, "params": {},
        }, headers=auth)
        check("补扣不足被拒 402（差额 90 > 余额 80）", r.status_code == 402, r.text)
        check("拒绝后余额仍 80", balance(token) == 80)

        r = client.delete(f"/api/queue/{t1}", headers=auth)
        check("排队中取消成功", r.status_code == 200, r.text)
        check("取消退款后回 100", balance(token) == 100, str(balance(token)))
    finally:
        qs.current_task_id = None

    print("── 失败自动退款（解除阻断，TTS 连接失败 → FAILED → 退款） ──")
    r = submit({"project_name": "t4", "kind": "mono", "lines": make_lines(3000)}, auth)
    t4 = r.json()["task_id"]
    # 注意：worker 会立刻抢跑并失败退款，这里不断言中间余额（存在竞态），只看终态
    task = wait_terminal(t4)
    check("任务失败终态", task.get("status") == "failed", str(task.get("status")))
    check("退款后余额回 100", balance(token) == 100, str(balance(token)))
    logs = client.get("/api/points/logs", headers=auth).json()["logs"]
    kinds = [e["kind"] for e in logs]
    check("流水含 spend+refund", "spend" in kinds and "refund" in kinds, str(kinds))
    check("任务预扣额清零", task.get("points_charged") in (0, None), str(task.get("points_charged")))

    print("── 退款幂等 ──")
    from app.queue_worker import refund_task_points
    fake = {"id": "q_fake", "member_id": body["user"]["user_id"],
            "points_charged": 30, "points_charge_log": "pl_fake", "status": "failed"}
    refund_task_points(fake)
    b1 = balance(token)
    refund_task_points(fake)
    b2 = balance(token)
    check("同 charge_log 只退一次", b1 == b2, f"{b1} vs {b2}")

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
