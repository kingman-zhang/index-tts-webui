#!/usr/bin/env python3
"""队列任务名去重（2026-10-30）。

背景：项目名不改（默认 `未命名_YYYYMMDD`）时，队列里会出现一长串同名任务，
看不出谁是谁、下载下来也是同一个文件名。

本文件锁的行为（规则见 `app/routes/queue.py::_unique_project_name`）：
  1. 名字没被占用 ⇒ 原样使用；
  2. 已占用 ⇒ `{原名}_01`、`{原名}_02`…（两位序号，取第一个未占用的）；
  3. 占用范围 = 与列表可见范围一致：同一 kind（播客/配音各一套）、
     隔离开启时只看自己的 → 不同用户互不影响；
  4. 终态任务也算占用（它还在列表里，用户会看到重名）；
  5. 删掉 `_01` 后再提交 ⇒ 复用 `_01`（列表里已不存在该名字）；
  6. 下载名由任务名推导，与任务名一致。

    cd webui-backend && python tests/test_queue_name_unique.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-queue-name-unique-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MEMBER_ADMIN_TOKEN"] = "test-admin-token"
os.environ["MEMBER_REQUIRE_LOGIN"] = "1"   # 只开隔离与登录要求，不按字扣费
os.environ["MEMBER_ENFORCE"] = "0"
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


def submit(name: str, kind: str, auth: dict) -> dict:
    r = client.post("/api/queue/submit", json={
        "project_name": name,
        "kind": kind,
        "lines": [{"speaker": "A", "text": "字" * 40, "emotion": {"mode": 0}}],
        "voices": {}, "silence": {}, "params": {}, "glossary_enabled": False,
    }, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def stored_name(task_id: str) -> str:
    return qs.queue_tasks[task_id]["project_name"]


def main():
    u1 = service.register("命名用户一", "pass123")["token"]
    a1 = {"Authorization": f"Bearer {u1}"}
    u2 = service.register("命名用户二", "pass123")["token"]
    a2 = {"Authorization": f"Bearer {u2}"}

    print("── 1. 首次提交：原名不变 ──")
    r = submit("未命名_20261029", "mono", a1)
    check("首个任务用原名", stored_name(r["task_id"]) == "未命名_20261029", stored_name(r["task_id"]))
    check("响应里带回实际任务名", r.get("project_name") == "未命名_20261029", str(r))

    print("── 2. 同名再来两次：_01 / _02 ──")
    r = submit("未命名_20261029", "mono", a1)
    check("第二个 → _01", stored_name(r["task_id"]) == "未命名_20261029_01", stored_name(r["task_id"]))
    r = submit("未命名_20261029", "mono", a1)
    check("第三个 → _02", stored_name(r["task_id"]) == "未命名_20261029_02", stored_name(r["task_id"]))
    t3 = r["task_id"]

    print("── 3. 其他名字不受影响 ──")
    r = submit("另一个项目", "mono", a1)
    check("不同名字原样使用", stored_name(r["task_id"]) == "另一个项目", stored_name(r["task_id"]))
    r = submit("未命名_20261029_01", "mono", a1)
    check("连 _01 都被占用时再从 _01 起算",
          stored_name(r["task_id"]) == "未命名_20261029_01_01", stored_name(r["task_id"]))

    print("── 4. 跨 kind 互不影响（播客 / 配音各一套名字）──")
    r = submit("未命名_20261029", "podcast", a1)
    check("podcast 页首个任务用原名", stored_name(r["task_id"]) == "未命名_20261029",
          stored_name(r["task_id"]))

    print("── 5. 跨用户互不影响（隔离开启时）──")
    r = submit("未命名_20261029", "mono", a2)
    check("用户二的首个同名任务用原名", stored_name(r["task_id"]) == "未命名_20261029",
          stored_name(r["task_id"]))
    r = submit("未命名_20261029", "mono", a2)
    check("用户二的重名从 _01 起算", stored_name(r["task_id"]) == "未命名_20261029_01",
          stored_name(r["task_id"]))

    print("── 6. 终态任务仍占名字 ──")
    qs.queue_tasks[t3]["status"] = S.SUCCESS
    r = submit("未命名_20261029", "mono", a1)
    check("已完成任务的名字不会被抢走",
          stored_name(r["task_id"]) == "未命名_20261029_03", stored_name(r["task_id"]))
    t4 = r["task_id"]

    print("── 7. 删掉 _01 后复用第一个空位 ──")
    # 直接删掉用户一的 _01（等价于在队列里删掉那个任务）
    victim = [tid for tid, t in qs.queue_tasks.items()
              if t.get("project_name") == "未命名_20261029_01"
              and t.get("kind") == "mono"
              and t.get("member_id") == qs.queue_tasks[t4]["member_id"]]
    check("找到待删除的 _01", len(victim) == 1, str(victim))
    del qs.queue_tasks[victim[0]]
    r = submit("未命名_20261029", "mono", a1)
    check("空出来的 _01 被复用", stored_name(r["task_id"]) == "未命名_20261029_01",
          stored_name(r["task_id"]))

    print("── 8. 下载名跟随任务名 ──")
    for name in ("未命名_20261029", "未命名_20261029_01", "未命名_20261029_02"):
        dn = qs.audio_download_name({"project_name": name}, "q_x", "mono")
        check(f"下载名 = {name}.wav", dn == f"{name}.wav", dn)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        queue_routes.process_queue = _ORIG_PROCESS_QUEUE
        shutil.rmtree(TMP, ignore_errors=True)
