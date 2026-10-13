#!/usr/bin/env python3
"""端到端取证：`QUEUE_STORE=mongo` 下队列真的能跑（B1）。

`tests/test_queue_store.py` 只验门面本身的契约；这个脚本补的是**真实链路**：
用 `TestClient` 走 HTTP 接口（提交 / 列表 / 拖拽排序 / 重试 / 取消），
再**重启一次**（清空内存后重新 `load_persisted_tasks()`）确认任务从库里恢复、顺序不丢。

⚠️ 会清空 `TEST_MONGO_DB`（默认 `podcast_test`）的 `queue_tasks` 集合 ——
   拒绝在库 `podcast`（生产）上跑。

用法：
    cd webui-backend
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \\
        python ../outputs/verify-queue-store-mongo.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "webui-backend"
sys.path.insert(0, str(BACKEND_ROOT))

TMP = tempfile.mkdtemp(prefix="wb-verify-queue-")
os.environ["DATA_DIR"] = TMP
os.environ["QUEUE_STORE"] = "mongo"
os.environ["MONGO_DB"] = os.environ.get("TEST_MONGO_DB", "podcast_test")
os.environ["QUEUE_WORKER_ENABLED"] = "1"
os.environ["TTS_URL"] = "http://127.0.0.1:59999"      # 无服务，秒拒
os.environ["MEMBER_ENFORCE"] = "0"
os.environ["MEMBER_REQUIRE_LOGIN"] = "0"
if not os.environ.get("MONGO_URL"):
    for line in (BACKEND_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("MONGO_URL="):
            os.environ["MONGO_URL"] = line.partition("=")[2].strip().strip('"').strip("'")

from fastapi.testclient import TestClient  # noqa: E402

from app import queue_state as qs  # noqa: E402
from app import queue_store, queue_store_mongo  # noqa: E402
from app.config import MONGO_DB, QUEUE_STORE  # noqa: E402
from app.db import get_mongo_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import queue as queue_routes  # noqa: E402

PASS = 0
FAIL = 0
_ORIG_PROCESS_QUEUE = queue_routes.process_queue


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


async def _noop_process_queue():
    """拦住调度器：本脚本只验「状态存取」，不真去合成。"""


def lines(n: int = 40) -> list:
    return [{"speaker": "A", "text": "字" * n, "emotion": {"mode": 0}}]


def submit(client, name: str, kind: str) -> str:
    r = client.post("/api/queue/submit", json={
        "project_name": name, "kind": kind, "lines": lines(),
        "voices": {}, "silence": {}, "params": {}, "glossary_enabled": False,
    })
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def seq_in_db(task_id: str):
    doc = get_mongo_db()["queue_tasks"].find_one({"_id": task_id}, {"seq": 1})
    return None if doc is None else doc.get("seq")


def main() -> int:
    if MONGO_DB == "podcast":
        print("✗ 拒绝在库 'podcast'（生产）上跑 —— 请设置 TEST_MONGO_DB", file=sys.stderr)
        return 2

    db = get_mongo_db()
    db.command("ping")
    db["queue_tasks"].drop()
    print(f"队列后端 = {QUEUE_STORE}；库 = {MONGO_DB}（已清空 queue_tasks）\n")

    queue_routes.process_queue = _noop_process_queue   # 不真合成
    try:
        with TestClient(app) as client:
            print("── 提交 → 落库 ──")
            a = submit(client, "任务A", "mono")
            b = submit(client, "任务B", "podcast")
            check("两个任务都在库里", queue_store.load_task(a) and queue_store.load_task(b))
            check("seq 递增（A < B）", seq_in_db(a) < seq_in_db(b),
                  f"{seq_in_db(a)} / {seq_in_db(b)}")

            print("── 列表：顺序按 seq，queue_position 1/2 ──")
            body = client.get("/api/queue").json()
            ids = [t["id"] for t in body["tasks"]]
            check("列表含两个任务", set(ids) == {a, b}, str(ids))
            check("顺序按提交先后", ids == [a, b], str(ids))
            pos = {t["id"]: t["queue_position"] for t in body["tasks"]}
            check("queue_position = 1 / 2", pos[a] == 1 and pos[b] == 2, str(pos))

            print("── 拖拽排序 → seq 槽位置换（另一个任务不受影响）──")
            r = client.patch("/api/queue/reorder", json={"task_ids": [b, a]})
            check("reorder 200", r.status_code == 200, r.text)
            check("库里的 seq 也换了位", seq_in_db(a) == 2 and seq_in_db(b) == 1,
                  f"A={seq_in_db(a)} B={seq_in_db(b)}")
            body = client.get("/api/queue").json()
            check("列表顺序随之变化",
                  [t["id"] for t in body["tasks"]] == [b, a],
                  str([t["id"] for t in body["tasks"]]))

            print("── 重命名 / 编辑 → 库里的文档跟着变 ──")
            r = client.patch(f"/api/queue/{a}/name", json={"project_name": "任务A改名"})
            check("name 200", r.status_code == 200, r.text)
            check("库里已改名", queue_store.load_task(a)["project_name"] == "任务A改名")

            print("── 取消（排队中）→ 库里状态变 cancelled ──")
            r = client.delete(f"/api/queue/{a}")
            check("cancel 200", r.status_code == 200, r.text)
            check("库里状态 = cancelled",
                  queue_store.load_task(a)["status"] == "cancelled",
                  str(queue_store.load_task(a).get("status")))

            print("── 重试 → 回到队尾，seq 变成最大 ──")
            r = client.post(f"/api/queue/{a}/retry")
            check("retry 200", r.status_code == 200, r.text)
            check("库里状态 = queued 且 seq 最大",
                  queue_store.load_task(a)["status"] == "queued" and seq_in_db(a) > seq_in_db(b),
                  f"status={queue_store.load_task(a)['status']} seq A={seq_in_db(a)} B={seq_in_db(b)}")

            print("── 模拟重启：清空内存后从库里恢复 ──")
            qs.queue_tasks.clear()
            qs.queue_order.clear()
            qs.load_persisted_tasks()
            check("任务全部恢复", set(qs.queue_tasks) == {a, b}, str(sorted(qs.queue_tasks)))
            check("排队顺序按 seq 恢复（B 先，A 后）",
                  qs.queue_order == [b, a], str(qs.queue_order))
            check("改名与状态也恢复",
                  qs.queue_tasks[a]["project_name"] == "任务A改名"
                  and qs.queue_tasks[a]["status"] == "queued")

            print("── 删除「排队中」的任务 = 取消（记录保留，不算删除）──")
            r = client.delete(f"/api/queue/{b}")
            check("cancel 200", r.status_code == 200, r.text)
            raw_b = db["queue_tasks"].find_one({"_id": b}) or {}
            check("取消不是删除：库里仍在、且没打 deleted_at",
                  (queue_store.load_task(b) or {}).get("status") == "cancelled"
                  and "deleted_at" not in raw_b,
                  f"status={(queue_store.load_task(b) or {}).get('status')} keys={sorted(raw_b)}")

            print("── 删除「终态」任务 → 列表里没了，但库里留档（软删除）──")
            r = client.delete(f"/api/queue/{b}")
            check("delete 200", r.status_code == 200, r.text)
            raw = db["queue_tasks"].find_one({"_id": b}) or {}
            check("库里文档仍在（软删除，供统计/审计）", bool(raw), str(raw)[:120])
            check("……且打了 deleted_at", "deleted_at" in raw)
            check("……且带 created_ts（BSON Date，可做时间聚合）",
                  hasattr(raw.get("created_ts"), "year"), repr(raw.get("created_ts")))
            check("列表里已看不到它",
                  b not in [t["id"] for t in client.get("/api/queue").json()["tasks"]],
                  str([t["id"] for t in client.get("/api/queue").json()["tasks"]]))
            qs.queue_tasks.clear()
            qs.queue_order.clear()
            qs.load_persisted_tasks()
            check("清空内存再恢复，已删任务不会复活", b not in qs.queue_tasks,
                  str(sorted(qs.queue_tasks)))
    finally:
        queue_routes.process_queue = _ORIG_PROCESS_QUEUE
        db["queue_tasks"].drop()
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{'=' * 46}\nTOTAL {PASS + FAIL}  PASS {PASS}  FAIL {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
