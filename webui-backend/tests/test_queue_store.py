#!/usr/bin/env python3
"""队列存储门面（`app/queue_store.py`）的接口契约测试（自跑脚本，非 pytest）。

验证「按 `QUEUE_STORE` 选择后端」这套抽象是否成立（B1 队列上库）：
  1. 门面契约：`BACKEND` / `_impl` / `_REQUIRED_API` 一致，磁盘细节不外泄；
  2. 任务 CRUD 往返：整份覆盖、`lines` 等重字段不丢、单主键 upsert；
  3. `load_all_tasks` 全量读出（mongo 不带 `_id`）；
  4. `load_legacy_order`（file 读 `_queue_order.json`；mongo 恒空）；
  5. 删除语义与字段级更新（mongo：软删除 + `created_ts` + 负载指纹；file：真删）；
  6. `queue_state` 的持久化函数确实**转发**到了门面（集成点）；
  7. 非法后端**启动即报错**，不静默回退。

用法：
    python tests/test_queue_store.py                            # file 后端
    TEST_QUEUE_STORE=mongo python tests/test_queue_store.py      # mongo（独立测试库）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 后端由环境变量切换；默认 file（与生产默认一致）
TEST_BACKEND = (os.environ.get("TEST_QUEUE_STORE") or "file").strip().lower()

TMP = tempfile.mkdtemp(prefix="wb-queue-test-")
os.environ["DATA_DIR"] = TMP
os.environ["QUEUE_STORE"] = TEST_BACKEND
if TEST_BACKEND == "mongo":
    # ⚠️ 绝不能用生产库名 —— 本测试会清空集合
    os.environ["MONGO_DB"] = os.environ.get("TEST_MONGO_DB", "podcast_test")
    if not os.environ.get("MONGO_URL"):
        # 本测试默认屏蔽 .env（保证完全离线）；mongo 模式需要连接串，所以在
        # import app.config **之前**把它读回来（config/db 是 import 期读环境变量的）。
        _env_file = Path(__file__).resolve().parents[1] / ".env"
        if _env_file.exists():
            for _line in _env_file.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                if _k.strip() == "MONGO_URL":
                    os.environ.setdefault("MONGO_URL", _v.strip().strip('"').strip("'"))
else:
    # 设成**空串**（而非 pop）：config._load_dotenv 是 setdefault 语义，
    # 空串占位可保证真实 .env / 环境里的连接串不会渗进来 → 完全离线。
    os.environ["MONGO_URL"] = ""

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


def _import_facade_with(backend: str) -> subprocess.CompletedProcess:
    """在子进程里以指定 QUEUE_STORE 值 import 门面（后端在 import 期决定）。"""
    env = dict(os.environ)
    env["QUEUE_STORE"] = backend
    return subprocess.run(
        [sys.executable, "-c", "from app import queue_store"],
        cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True,
    )


def _prepare_mongo() -> None:
    """mongo 模式开跑前：清空测试库的 queue_tasks + 建索引。"""
    from app.config import MONGO_DB
    if MONGO_DB == "podcast":
        raise SystemExit("拒绝在库 'podcast' 上跑测试（那是生产库）—— 请设置 TEST_MONGO_DB")
    from app.db import get_mongo_db
    from tools.init_indexes import ensure_indexes
    db = get_mongo_db()
    db.command("ping")
    db["queue_tasks"].drop()
    from tools.init_indexes import MEMBER_INDEXES, QUEUE_INDEXES
    if ensure_indexes(db, verbose=False, indexes=MEMBER_INDEXES + QUEUE_INDEXES):
        raise SystemExit("测试库索引创建失败（见上方输出）")
    print(f"（后端 = mongo，库 = {MONGO_DB}：已清空 queue_tasks 并建好索引）\n")


def task_doc(task_id: str, **over) -> dict:
    doc = {
        "id": task_id,
        "project_name": "测试任务",
        "kind": "mono",
        "lines": [{"speaker": "A", "text": "你好，世界", "emotion": {"mode": 0}}],
        "voices": {"A": "/tmp/ref.wav"},
        "silence": {"within_segment": 200},
        "params": {"speed": 1.0},
        "glossary_enabled": True,
        "status": "queued",
        "progress": 0,
        "created_at": "2026-10-13T10:00:00",
        "cancel_requested": False,
        "seq": 1,
    }
    doc.update(over)
    return doc


def main():
    if TEST_BACKEND == "mongo":
        _prepare_mongo()

    # ── 门面契约 ──
    print(f"── 门面契约（后端 = {queue_store.BACKEND}）──")
    check("BACKEND 与环境选择一致", queue_store.BACKEND == TEST_BACKEND, repr(queue_store.BACKEND))
    check("实现模块与后端一致",
          queue_store._impl.__name__.endswith(f"queue_store_{TEST_BACKEND}"),
          queue_store._impl.__name__)
    missing = [n for n in queue_store._REQUIRED_API if not callable(getattr(queue_store, n, None))]
    check("_REQUIRED_API 全部可调用", not missing, f"缺: {missing}")
    leaked = [n for n in ("QUEUE_DIR", "QUEUE_ORDER_FILE", "task_path", "queue_file")
              if hasattr(queue_store, n)]
    check("门面不泄露磁盘细节", not leaked, f"泄露: {leaked}")
    check("queue_state 不再暴露 queue_file（磁盘细节已下沉到 file 后端）",
          not hasattr(qs, "queue_file"))

    # ── 任务 CRUD ──
    print(f"── 任务 CRUD（后端 = {queue_store.BACKEND}）──")
    check("读不存在的任务 → None", queue_store.load_task("q_nope") is None)

    d1 = task_doc("q_alpha")
    queue_store.save_task(d1)
    got = queue_store.load_task("q_alpha")
    check("往返：基本字段", bool(got) and got.get("project_name") == "测试任务")
    check("往返：lines 正文不丢",
          got and got["lines"][0]["text"] == "你好，世界", str(got and got.get("lines")))
    check("往返：嵌套 dict 不丢",
          got and got["lines"][0]["emotion"] == {"mode": 0}
          and got["voices"] == {"A": "/tmp/ref.wav"})
    check("往返：seq 保留", got and got.get("seq") == 1, str(got and got.get("seq")))
    check("往返：返回的对象不含 Mongo 的 _id（mongo 后端）", not (got and "_id" in got))

    queue_store.save_task(task_doc("q_alpha", project_name="改过名", seq=7))
    again = queue_store.load_task("q_alpha")
    check("同 id 再存是**覆盖**（upsert 单主键）", again.get("project_name") == "改过名")
    all_docs = {d["id"] for d in queue_store.load_all_tasks()}
    check("upsert 不产生第二份", all_docs == {"q_alpha"}, str(all_docs))

    queue_store.save_task(task_doc("q_beta"))
    ids = [d["id"] for d in queue_store.load_all_tasks()]
    check("load_all_tasks 含两个任务", set(ids) == {"q_alpha", "q_beta"}, str(ids))
    check("load_all_tasks 不夹带 _id",
          all("_id" not in d for d in queue_store.load_all_tasks()))

    check("delete 已存在的 → True", queue_store.delete_task("q_beta") is True)
    check("删除后读不到", queue_store.load_task("q_beta") is None)
    check("delete 不存在的 → False", queue_store.delete_task("q_beta") is False)
    check("load_all_tasks 只剩 1 个", [d["id"] for d in queue_store.load_all_tasks()] == ["q_alpha"])

    # ── 删除语义 / 字段级更新（两后端的**实现**差异；对外语义一致）──
    print("── 删除语义 / 字段级更新 ──")
    if TEST_BACKEND == "mongo":
        from app import queue_store_mongo as _m
        from app.db import get_mongo_db
        coll = get_mongo_db()["queue_tasks"]

        # 软删除：库里留着（供统计），读接口看不到
        queue_store.save_task(task_doc("q_soft"))
        check("软删除：删除返回 True", queue_store.delete_task("q_soft") is True)
        raw = coll.find_one({"_id": "q_soft"}) or {}
        check("软删除：库里的文档仍在，且打了 deleted_at",
              "deleted_at" in raw, str(sorted(raw.keys())))
        check("软删除：load_task 看不到", queue_store.load_task("q_soft") is None)
        check("软删除：load_all_tasks 看不到",
              "q_soft" not in {d["id"] for d in queue_store.load_all_tasks()})
        check("重复删除 → False（不重复打标记）", queue_store.delete_task("q_soft") is False)

        # 🔴 关键回归：对已删除的任务再 save（含负载变化）不能让它复活
        queue_store.save_task(task_doc("q_soft", lines=[{"text": "改过了"}]))
        check("对已删除任务再 save 不会抹掉 deleted_at",
              "deleted_at" in (coll.find_one({"_id": "q_soft"}) or {}))
        check("……所以它仍然不可见", queue_store.load_task("q_soft") is None)

        # created_ts：BSON Date、只写一次、不出现在上层对象里
        queue_store.save_task(task_doc("q_ts"))
        ts1 = (coll.find_one({"_id": "q_ts"}) or {}).get("created_ts")
        queue_store.save_task(task_doc("q_ts", status="running", progress=0.5))
        ts2 = (coll.find_one({"_id": "q_ts"}) or {}).get("created_ts")
        check("created_ts 是 BSON Date（可参与时间聚合）", hasattr(ts1, "year"), repr(ts1))
        check("created_ts 不随每次 save 变动（$setOnInsert）", ts1 == ts2, f"{ts1} vs {ts2}")
        check("created_ts 不出现在上层对象里（后端自治字段）",
              "created_ts" not in (queue_store.load_task("q_ts") or {}))

        # 字段级更新：负载变了要写进去；没变则跳过重写（只写小字段）
        _m._payload_sig.clear()
        queue_store.save_task(task_doc("q_pay", lines=[{"text": "原文"}]))
        sig0 = _m._payload_sig.get("q_pay")
        queue_store.save_task(task_doc("q_pay", lines=[{"text": "原文"}], progress=0.9))
        check("负载未变 ⇒ 指纹不变（跳过重写）", _m._payload_sig.get("q_pay") == sig0)
        check("……且小字段照常更新",
              (queue_store.load_task("q_pay") or {}).get("progress") == 0.9)
        queue_store.save_task(task_doc("q_pay", lines=[{"text": "改后"}], progress=1.0))
        check("负载变了 ⇒ 指纹变", _m._payload_sig.get("q_pay") != sig0)
        check("……且库里确实写进去了",
              ((queue_store.load_task("q_pay") or {}).get("lines") or [{}])[0].get("text") == "改后")
    else:
        from app import queue_store_file as _f
        queue_store.save_task(task_doc("q_hard"))
        p = _f.task_path("q_hard")
        check("file 后端：删除前文件在", p.exists())
        check("file 后端：删除返回 True", queue_store.delete_task("q_hard") is True)
        check("file 后端：删除后文件真的没了（真删，不是软删）", not p.exists())
        check("file 后端：再删 → False", queue_store.delete_task("q_hard") is False)

    # ── queue_state 的转发（集成点）──
    print("── queue_state 转发（集成点）──")
    qs.queue_tasks["q_via_qs"] = task_doc("q_via_qs", project_name="经 queue_state 落库")
    qs.persist_task("q_via_qs")
    got = queue_store.load_task("q_via_qs")
    check("queue_state.persist_task 真的落到后端",
          bool(got) and got.get("project_name") == "经 queue_state 落库", str(got))
    qs.delete_persisted_task("q_via_qs")
    check("queue_state.delete_persisted_task 真的删掉",
          queue_store.load_task("q_via_qs") is None)
    qs.queue_tasks.pop("q_via_qs", None)

    # ── 遗留顺序提示 ──
    print("── load_legacy_order ──")
    if TEST_BACKEND == "file":
        QUEUE_ORDER_FILE.write_text(json.dumps(["q_alpha"]), encoding="utf-8")
        check("file 后端读得到 _queue_order.json",
              queue_store.load_legacy_order() == ["q_alpha"], str(queue_store.load_legacy_order()))
        QUEUE_ORDER_FILE.unlink()
        check("文件缺失 → []", queue_store.load_legacy_order() == [])
    else:
        check("mongo 后端恒返回 []", queue_store.load_legacy_order() == [])

    # ── 非法后端 ──
    print("── 非法后端 ──")
    for bad in ("sqlite", "redis", "mongodb"):
        cp = _import_facade_with(bad)
        check(f"QUEUE_STORE={bad!r} → import 期报错", cp.returncode != 0, (cp.stderr or "")[-200:])
    cp = _import_facade_with("mongo")   # 合法值应可 import（懒连接，不真的连）
    check("QUEUE_STORE=mongo 可 import", cp.returncode == 0, (cp.stderr or "")[-300:])

    print(f"\n{'=' * 46}\nTOTAL {PASS + FAIL}  PASS {PASS}  FAIL {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
