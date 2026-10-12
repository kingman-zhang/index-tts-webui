#!/usr/bin/env python3
"""会话存储门面 / 两后端的接口契约测试（自跑脚本，项目惯例非 pytest）。

Gate 3 的验收目标：会话从账本存储里拆出来（`session_store`）之后，
**换后端（file ↔ redis）行为等价** —— 同一条断言跑两种后端都得绿。

用法：
    python tests/test_session_store.py                             # file 后端（默认）
    TEST_SESSION_STORE=redis python tests/test_session_store.py    # redis 后端

⚠️ redis 模式用**独立 key 前缀** `podcast-test-session`：Redis 是与其他项目**共用**的
   实例，绝不能用默认的 `podcast` 前缀（会踩生产/别人家的 key）。开跑前后各清一次该前缀，
   全程不用 FLUSHDB / FLUSHALL。
"""

from __future__ import annotations

import json as _json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 后端由环境变量切换；默认 file（与生产默认一致）
TEST_BACKEND = (os.environ.get("TEST_SESSION_STORE") or "file").strip().lower()

TMP = tempfile.mkdtemp(prefix="wb-session-test-")
os.environ["DATA_DIR"] = TMP
os.environ["SESSION_STORE"] = TEST_BACKEND

if TEST_BACKEND == "redis":
    # 独立前缀：防止踩到生产 key（redis_key() 会强制加上它）
    os.environ["REDIS_KEY_PREFIX"] = os.environ.get(
        "TEST_REDIS_KEY_PREFIX", "podcast-test-session")
    if not os.environ.get("REDIS_URL"):
        # 本测试默认屏蔽 .env（保证 file 模式完全离线）；redis 模式需要连接串，所以在
        # import app.config **之前**把它读回来（config / db 都是 import 期读环境变量的）。
        _env_file = Path(__file__).resolve().parents[1] / ".env"
        if _env_file.exists():
            for _line in _env_file.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                if _k.strip() == "REDIS_URL":
                    os.environ.setdefault("REDIS_URL", _v.strip().strip('"').strip("'"))
else:
    # 设成**空串**（而非 pop）：config._load_dotenv 是 setdefault 语义，
    # 空串占位可保证真实 .env 里的连接串不会渗进来 → 完全离线。
    os.environ["REDIS_URL"] = ""

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app import db  # noqa: E402
from app.config import REDIS_KEY_PREFIX  # noqa: E402
from app.membership import session_store  # noqa: E402

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


def _import_with(backend: str) -> subprocess.CompletedProcess:
    """在子进程里以指定 SESSION_STORE 值 import 门面（后端在 import 期决定）。"""
    env = dict(os.environ)
    env["SESSION_STORE"] = backend
    return subprocess.run(
        [sys.executable, "-c", "from app.membership import session_store"],
        cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True,
    )


def _cleanup_redis() -> int:
    """删掉测试前缀下的全部 key（SCAN，不用 KEYS）。"""
    r = db.get_redis()
    n = 0
    for k in list(r.scan_iter(match=f"{REDIS_KEY_PREFIX}:*", count=200)):
        r.delete(k)
        n += 1
    return n


def _prepare_redis() -> None:
    if not os.environ.get("REDIS_URL"):
        raise SystemExit("REDIS_URL 未配置 —— redis 模式需要连接串（.env 或环境变量）")
    if REDIS_KEY_PREFIX in ("", "podcast"):
        raise SystemExit(
            f"拒绝用前缀 {REDIS_KEY_PREFIX!r} 跑测试（那是生产前缀，会踩别人的 key）"
            f"—— 请用 TEST_REDIS_KEY_PREFIX 换一个")
    db.get_redis().ping()
    _cleanup_redis()
    print(f"（后端 = redis，前缀 = {REDIS_KEY_PREFIX}：已清空该前缀下的测试 key）\n")


def main():
    if TEST_BACKEND == "redis":
        _prepare_redis()

    print(f"── 门面契约（后端 = {session_store.BACKEND}）──")
    check("BACKEND 与环境选择一致", session_store.BACKEND == TEST_BACKEND, repr(session_store.BACKEND))
    # `__name__` 是完整包路径（app.membership.session_store_file），所以用 endswith
    check("实现模块与后端一致",
          session_store._impl.__name__.endswith(f"session_store_{TEST_BACKEND}"),
          session_store._impl.__name__)
    missing = [n for n in session_store._REQUIRED_API if not callable(getattr(session_store, n, None))]
    check("_REQUIRED_API 全部可调用", not missing, f"缺: {missing}")

    print("── 非法后端：启动即报错（不静默回退到 file）──")
    r = _import_with("bogus")
    check("非法 SESSION_STORE 在 import 期报错",
          r.returncode != 0 and "不是受支持的后端" in (r.stderr or ""),
          (r.stderr or "")[-300:])
    r = _import_with("mongo")
    check("把账本后端名写在 SESSION_STORE 上同样被拒",
          r.returncode != 0 and "不是受支持的后端" in (r.stderr or ""), (r.stderr or "")[-300:])

    print("── 基本读写 ──")
    session_store.put_session("tok_a", {"user_id": "u_a", "created_at": "2026-10-12T00:00:00"}, 3600)
    check("put → get 往返", (session_store.get_session("tok_a") or {}).get("user_id") == "u_a")
    check("get 不存在的 token → None", session_store.get_session("tok_missing") is None)
    check("count 反映一条会话", session_store.count_sessions() == 1,
          str(session_store.count_sessions()))

    print("── 覆盖写 ──")
    session_store.put_session("tok_a", {"user_id": "u_a", "extra": 1}, 3600)
    check("同 token 覆盖写生效", (session_store.get_session("tok_a") or {}).get("extra") == 1)
    check("覆盖后仍只有一条", session_store.count_sessions() == 1,
          str(session_store.count_sessions()))

    print("── 删除（幂等）──")
    check("delete 首次 True", session_store.delete_session("tok_a") is True)
    check("delete 再来 False（幂等）", session_store.delete_session("tok_a") is False)
    check("删后 get → None", session_store.get_session("tok_a") is None)
    check("删后 count 归零", session_store.count_sessions() == 0,
          str(session_store.count_sessions()))

    print("── 按用户吊销（改密 / 禁用账号踢下线）──")
    session_store.put_session("tok_b1", {"user_id": "u_b"}, 3600)
    session_store.put_session("tok_b2", {"user_id": "u_b"}, 3600)
    session_store.put_session("tok_c", {"user_id": "u_c"}, 3600)
    n = session_store.delete_user_sessions("u_b")
    check("返回删掉的条数 = 2", n == 2, f"实际 {n}")
    check("该用户会话全消失",
          all(session_store.get_session(t) is None for t in ("tok_b1", "tok_b2")))
    check("别的用户会话不受影响",
          (session_store.get_session("tok_c") or {}).get("user_id") == "u_c")
    check("对不存在的用户 → 0", session_store.delete_user_sessions("u_nobody") == 0)

    print("── 过期（file 靠 expires_ts 惰性清；redis 靠 TTL）──")
    before = session_store.count_sessions()
    session_store.put_session("tok_ttl", {"user_id": "u_d"}, 1)
    check("刚写入还在", session_store.get_session("tok_ttl") is not None)
    time.sleep(1.2)
    check("超过 TTL 后 get → None", session_store.get_session("tok_ttl") is None)
    check("过期的不计入 count", session_store.count_sessions() == before,
          f"{session_store.count_sessions()} vs {before}")

    if TEST_BACKEND == "redis":
        print("── redis 专属：key 前缀隔离 ──")
        keys = sorted(db.get_redis().scan_iter(match=f"{REDIS_KEY_PREFIX}:*", count=200))
        check("写入的 key 全部带测试前缀",
              bool(keys) and all(k.startswith(f"{REDIS_KEY_PREFIX}:") for k in keys), str(keys[:3]))
        check("没有裸 session: / user_sessions: 这种通用名",
              not any(k.startswith(("session:", "user_sessions:")) for k in keys), str(keys[:3]))
        check("反向索引在 user_sessions 下",
              f"{REDIS_KEY_PREFIX}:user_sessions:u_c" in keys, str(keys))
        check("会话 key 带 TTL（不是永久键）",
              db.get_redis().ttl(f"{REDIS_KEY_PREFIX}:session:tok_c") > 0,
              str(db.get_redis().ttl(f"{REDIS_KEY_PREFIX}:session:tok_c")))
        _cleanup_redis()
        print(f"（已清理测试前缀下的 key）")
    else:
        print("── file 专属：文件形态与历史一致（回滚可直接用）──")
        raw = _json.loads((Path(TMP) / "members" / "sessions.json").read_text(encoding="utf-8"))
        check("sessions.json 仍是 {token: {...}}", isinstance(raw, dict) and "tok_c" in raw,
              str(list(raw)[:3]))
        check("记录仍带 expires_ts（降级回旧代码时靠它判过期）",
              float((raw.get("tok_c") or {}).get("expires_ts") or 0) > time.time(),
              str(raw.get("tok_c")))
        check("过期条目已被惰性清出文件", "tok_ttl" not in raw, str(list(raw)))

    print(f"\n{'=' * 52}\n后端 = {TEST_BACKEND}   TOTAL {PASS + FAIL}   PASS {PASS}   FAIL {FAIL}\n{'=' * 52}")
    shutil.rmtree(TMP, ignore_errors=True)
    if TEST_BACKEND == "redis":
        db.close_all()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
