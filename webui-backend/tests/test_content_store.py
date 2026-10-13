#!/usr/bin/env python3
"""内容存储门面（项目 / 术语表 / 收藏音色）的接口契约测试（自跑脚本，非 pytest）。

验证「按 `CONTENT_STORE` 选择后端」这套抽象是否成立：
  1. 门面契约：`BACKEND` / `_impl` / `_REQUIRED_API` 一致，磁盘细节不外泄；
  2. 项目：CRUD 往返、id 生成、owner 过滤、按 `updated_at` 倒序、摘要行数；
  3. 术语表：全局 / 每用户两层，合并语义（全局骨架 + 用户覆盖 + 空串停用）；
  4. 收藏音色：往返、去重、按用户隔离、共享那份；
  5. 中点码位变体展开（回归，存储改造不该碰它）；
  6. 非法后端**启动即报错**，不静默回退。

用法：
    python tests/test_content_store.py                            # file 后端
    TEST_CONTENT_STORE=mongo python tests/test_content_store.py    # mongo（独立测试库）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 后端由环境变量切换；默认 file（与生产默认一致）
TEST_BACKEND = (os.environ.get("TEST_CONTENT_STORE") or "file").strip().lower()

TMP = tempfile.mkdtemp(prefix="wb-content-test-")
os.environ["DATA_DIR"] = TMP
os.environ["CONTENT_STORE"] = TEST_BACKEND
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

from app import stores  # noqa: E402
from app.models import ProjectModel  # noqa: E402

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


def _import_stores_with(backend: str) -> subprocess.CompletedProcess:
    """在子进程里以指定 CONTENT_STORE 值 import 门面（后端在 import 期决定）。"""
    env = dict(os.environ)
    env["CONTENT_STORE"] = backend
    return subprocess.run(
        [sys.executable, "-c", "from app import stores"],
        cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True,
    )


def _prepare_mongo() -> None:
    """mongo 模式开跑前：清空测试库的内容集合 + 建索引。"""
    from app.config import MONGO_DB
    if MONGO_DB == "podcast":
        raise SystemExit("拒绝在库 'podcast' 上跑测试（那是生产库）—— 请设置 TEST_MONGO_DB")
    from app.db import get_mongo_db
    from tools.init_indexes import ensure_indexes
    db = get_mongo_db()
    db.command("ping")
    for name in ("projects", "glossary", "favorites"):
        db[name].drop()
    if ensure_indexes(db, verbose=False):
        raise SystemExit("测试库索引创建失败（见上方输出）")
    print(f"（后端 = mongo，库 = {MONGO_DB}：已清空并建好索引）\n")


def main():
    if TEST_BACKEND == "mongo":
        _prepare_mongo()

    # ── 门面契约 ──
    print(f"── 门面契约（后端 = {stores.BACKEND}）──")
    check("BACKEND 与环境选择一致", stores.BACKEND == TEST_BACKEND, repr(stores.BACKEND))
    check("实现模块与后端一致",
          stores._impl.__name__.endswith(f"stores_{TEST_BACKEND}"), stores._impl.__name__)
    missing = [n for n in stores._REQUIRED_API if not callable(getattr(stores, n, None))]
    check("_REQUIRED_API 全部可调用", not missing, f"缺: {missing}")
    leaked = [n for n in ("PROJECTS_DIR", "GLOSSARY_PATH", "GLOSSARY_USERS_DIR",
                          "FAVORITES_PATH", "project_path") if hasattr(stores, n)]
    check("磁盘细节不外泄", not leaked, f"外泄: {leaked}")

    # ── 项目 ──
    print(f"── 项目（后端 = {stores.BACKEND}）──")
    p1 = ProjectModel(name="我的播客", owner_id="u_alice",
                      lines=[{"speaker": "A", "text": "你好"}])
    pid1 = stores.save_project(p1)
    check("新建返回 id（proj_ 前缀）", pid1.startswith("proj_"), pid1)
    check("id / 时间戳回填到模型", p1.id == pid1 and bool(p1.created_at and p1.updated_at))
    got = stores.load_project(pid1)
    check("读回往返（name / owner / lines）",
          bool(got) and got.name == "我的播客" and got.owner_id == "u_alice"
          and len(got.lines) == 1)
    check("读不存在的项目 → None", stores.load_project("proj_nope") is None)

    time.sleep(0.01)   # 让 updated_at 有可辨差异（isoformat 微秒精度）
    stores.save_project(ProjectModel(name="Alice 第二个", owner_id="u_alice"))
    pid3 = stores.save_project(ProjectModel(name="Bob 的项目", owner_id="u_bob"))

    names_all = [x["name"] for x in stores.list_projects(None)]
    check("list(全部) 含 3 个", len(names_all) == 3, str(names_all))
    check("list 按 updated_at 倒序（新的在前）", names_all[0] == "Bob 的项目", str(names_all))

    alice = sorted(x["name"] for x in stores.list_projects("u_alice"))
    check("list(owner) 只返回该用户", alice == ["Alice 第二个", "我的播客"], str(alice))
    check("摘要带 line_count",
          {x["name"]: x["line_count"] for x in stores.list_projects(None)}.get("我的播客") == 1)

    check("delete 返回 True", stores.delete_project(pid3) is True)
    check("删除后读不到", stores.load_project(pid3) is None)
    check("delete 不存在的 → False", stores.delete_project(pid3) is False)

    # ── 术语表 ──
    print("── 术语表 ──")
    stores.save_global_glossary([
        {"original": "认知", "replacement": "认知"},
        {"original": "觉醒", "replacement": "觉醒"},
    ])
    check("全局库往返",
          [t["original"] for t in stores.load_global_glossary()] == ["认知", "觉醒"])

    check("未登录用户库为空", stores.load_user_glossary(None) == [])
    check("无记录用户库为空", stores.load_user_glossary("u_alice") == [])

    stores.save_user_glossary("u_alice", [
        {"original": "觉醒", "replacement": "觉醒（改）"},
        {"original": "元认知", "replacement": "元认知力"},
    ])
    check("用户库往返", len(stores.load_user_glossary("u_alice")) == 2)

    merged = stores.load_glossary("u_alice", with_source=True)
    by_key = {t["original"]: t for t in merged}
    check("合并：用户覆盖同名（source=user）",
          by_key["觉醒"]["replacement"] == "觉醒（改）" and by_key["觉醒"]["source"] == "user")
    check("合并：全局独有保留（source=global）", by_key["认知"]["source"] == "global")
    check("合并：用户独有追加在末尾", merged[-1]["original"] == "元认知")

    stores.save_user_glossary("u_bob", [{"original": "认知", "replacement": ""}])
    bob = [t["original"] for t in stores.load_glossary("u_bob")]
    check("空 replacement 停用内置词", "认知" not in bob, str(bob))

    # 中点码位变体展开：存储改造**不该**碰它（回归）
    terms = [{"original": "9・11", "replacement": "九幺幺"}]
    expanded = stores.expand_separator_variants(terms)
    check("中点变体展开产生多条", len(expanded) > 1, str(len(expanded)))
    check("展开不修改原列表", terms == [{"original": "9・11", "replacement": "九幺幺"}])
    out = stores.apply_glossary([{"text": "9・11 事件"}], expanded)
    check("apply_glossary 按序替换且不改入参",
          out[0]["text"] == "九幺幺 事件", out[0]["text"])

    # ── 收藏音色 ──
    print("── 收藏音色 ──")
    check("空收藏 → []", stores.load_favorites("u_alice") == [])
    saved = stores.save_favorites(["/a.wav", "/b.wav", "/a.wav", ""], "u_alice")
    check("保存去重 + 丢弃空串", saved == ["/a.wav", "/b.wav"], str(saved))
    check("读回一致", stores.load_favorites("u_alice") == ["/a.wav", "/b.wav"])
    check("按用户隔离", stores.load_favorites("u_bob") == [])
    stores.save_favorites(["/shared.wav"], None)
    check("共享那份独立", stores.load_favorites(None) == ["/shared.wav"])
    check("共享与用户互不影响", stores.load_favorites("u_alice") == ["/a.wav", "/b.wav"])

    # ── 非法后端 ──
    print("── 非法后端 ──")
    for bad in ("sqlite", "mongodb", "redis"):
        cp = _import_stores_with(bad)
        check(f"CONTENT_STORE={bad!r} → import 期报错",
              cp.returncode != 0, (cp.stderr or "")[-200:])
    cp = _import_stores_with("mongo")   # 合法值应可 import（懒连接，不真的连）
    check("CONTENT_STORE=mongo 可 import", cp.returncode == 0, (cp.stderr or "")[-300:])

    print(f"\n{'=' * 46}\nTOTAL {PASS + FAIL}  PASS {PASS}  FAIL {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
