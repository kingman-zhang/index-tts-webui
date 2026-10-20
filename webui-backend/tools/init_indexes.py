#!/usr/bin/env python3
"""创建 / 校验 MongoDB 账本的索引（**幂等，可重复跑**）。

为什么这是个正式脚本而不是"顺手在代码里建一下"
--------------------------------------------
**唯一索引是资金安全的第一道防线**：「先查后写」无论加什么锁都有窗口，
数据库唯一约束没有窗口。所以它是**上线前置步骤**，不是可选优化。
（见 STORAGE_MIGRATION_PLAN.md §3.2 / Gate 2 / Gate 4 检查清单。）

索引清单与理由
--------------
| 集合 | 键 | 唯一 | 作用 |
|---|---|---|---|
| `users` | `username` | ✅ | 并发同名注册只能成功一个 |
| `users` | `email` | ✅（partial） | 并发同邮箱注册；**无 email 的用户不参与索引** |
| `point_logs` | `user_id,ref,kind` | ✅（partial） | **幂等第一防线**：重复退款/重复入账被硬挡下 |
| `point_logs` | `user_id,created_at` | — | 某用户流水分页与裁剪 |
| `point_logs` | `created_at` | — | 对账/列表按时间倒序 |
| `orders` | `out_trade_no` | — | 真实回调只回传商户订单号，按它找单 |
| `projects` | `owner_id,updated_at` | — | 项目列表（某用户 + 最近更新倒序）|
| `queue_tasks` | `status,seq` | — | 调度取队首（按 seq）|
| `queue_tasks` | `status,deleted_at` | — | 统计「在跑多少」（排除软删除）|
| `queue_tasks` | `member_id,created_at` | — | 队列列表（某用户 + 时间倒序）|
| `queue_tasks` | `created_ts` | — | 「近一周任务量」等按时间范围 / 分天聚合的统计 |
| `feedback` | `created_ts` | — | 反馈收件箱按时间倒序翻页 |
| `feedback` | `user_id,created_ts` | — | 某位用户提交过哪些反馈 |

⚠️ `projects` 索引只在 `CONTENT_STORE=mongo` 时才纳入检查/创建（见 `active_indexes()`）——
   file 模式下 projects 集合根本不存在，硬要求它会让 `--check` 误报。
   `glossary` / `favorites` 用 `_id`（`GLOBAL` | user_id）做主键，无需额外索引。
⚠️ `queue_tasks` 的四个索引同理，只在 `QUEUE_STORE=mongo` 时纳入。
   `queue_tasks` 用 `_id = task_id` 做主键（主键索引 Mongo 自带）。
   `created_ts` 是**后端自治字段**（BSON Date，`$setOnInsert` 写入）——
   `created_at` 是无时区字符串，只能做范围查、不能做时间聚合，所以另存一个。

⚠️ `point_logs` 的唯一索引**刻意只覆盖 `refund` / `purchase`**（`partialFilterExpression`）：
   - 只有这两类会被"重复触发"（退款重试、支付平台回调重推）；
   - `spend` **绝不能**纳入 —— `routes/queue.py:retry_queue_task` 重试时**复用同一个
     `task_id`** 并重新预扣积分，把 `spend` 纳入唯一索引会让重试的扣费被自己挡住。
   （常量定义见 `app/membership/store_contract.py:IDEMPOTENT_KINDS`。）

用法
----
    cd webui-backend
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \\
        python tools/init_indexes.py            # 创建（已存在则跳过，定义不同会报错）
    ... python tools/init_indexes.py --check    # 只检查，缺索引则退出码 1（上线前自检）

退出码：0 = 全部就位；1 = 有缺失或创建失败；2 = 连不上数据库。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import CONTENT_STORE, MONGO_DB, QUEUE_STORE  # noqa: E402
from app.db import DBUnavailable, get_mongo_db  # noqa: E402
from app.membership.store_contract import IDEMPOTENT_KINDS  # noqa: E402

# (集合, 键, 选项) —— 选项里显式给 name，好让 --check 按名字比对
MEMBER_INDEXES: tuple[tuple[str, list[tuple[str, int]], dict], ...] = (
    ("users", [("username", 1)],
     {"unique": True, "name": "uniq_username"}),
    ("users", [("email", 1)],
     {"unique": True, "name": "uniq_email",
      # 只索引"非空字符串"的 email —— 用户名注册的用户没有 email 字段，
      # 不加这个条件会让多个无 email 用户互相冲突（null 重复）。
      "partialFilterExpression": {"email": {"$type": "string", "$gt": ""}}}),
    ("point_logs", [("user_id", 1), ("ref", 1), ("kind", 1)],
     {"unique": True, "name": "uniq_idempotent",
      # 见上方说明：只覆盖 refund / purchase（$or 是 partialFilterExpression
      # 明确支持的形式；$in 在部分版本上不被接受）。
      "partialFilterExpression": {
          "ref": {"$type": "string", "$gt": ""},
          "$or": [{"kind": "refund"}, {"kind": "purchase"}],
      }}),
    ("point_logs", [("user_id", 1), ("created_at", -1)],
     {"name": "user_recent"}),
    ("point_logs", [("created_at", -1)],
     {"name": "recent"}),
    ("orders", [("out_trade_no", 1)],
     {"name": "by_out_trade_no"}),
)

# 内容集合（`CONTENT_STORE=mongo` 才要求存在；见 stores_mongo.py）
#   - projects：按「谁的项目 + 最近更新」列表查询（stores_mongo 的 $sort updated_at）
#   - glossary / favorites：用 _id（GLOBAL | user_id）做主键，Mongo 自带主键索引
CONTENT_INDEXES: tuple[tuple[str, list[tuple[str, int]], dict], ...] = (
    ("projects", [("owner_id", 1), ("updated_at", -1)],
     {"name": "by_owner_recent"}),
)

# 队列集合（`QUEUE_STORE=mongo` 才要求存在；见 queue_store_mongo.py）
#   - by_status_seq：调度取队首用（`find_one_and_update({status:"queued"},
#     sort=[("seq",1)])`，B2 的形态；B1 用不到，但索引属于 schema，随数据模型一起声明）。
#   - by_status_deleted：统计「在跑多少」＝ `count({status:"running",
#     deleted_at:{$exists:False}})`。
#   - by_member_recent / by_created_ts：列表查询（某用户 + 时间倒序）与
#     「近一周任务量」这类**按 created_ts 的时间范围 / 分天聚合**统计。
QUEUE_INDEXES: tuple[tuple[str, list[tuple[str, int]], dict], ...] = (
    ("queue_tasks", [("status", 1), ("seq", 1)],
     {"name": "by_status_seq"}),
    ("queue_tasks", [("status", 1), ("deleted_at", 1)],
     {"name": "by_status_deleted"}),
    ("queue_tasks", [("member_id", 1), ("created_at", -1)],
     {"name": "by_member_recent"}),
    ("queue_tasks", [("created_ts", -1)],
     {"name": "by_created_ts"}),
)


# 反馈收件箱（`app/routes/feedback.py`）。**总是要求存在**：反馈只写 Mongo，
# 不随 `MEMBER_STORE` / `CONTENT_STORE` 的开关变化。
#   - by_created_ts：运营按时间倒序翻「最近的反馈」
#     （`db.feedback.find().sort({created_ts:-1})`）
#   - by_user_recent：某位用户提交过什么（排查「他说的那个问题」时先看这里）
FEEDBACK_INDEXES: tuple[tuple[str, list[tuple[str, int]], dict], ...] = (
    ("feedback", [("created_ts", -1)],
     {"name": "by_created_ts"}),
    ("feedback", [("user_id", 1), ("created_ts", -1)],
     {"name": "by_user_recent"}),
)


def active_indexes() -> tuple:
    """当前配置下**应当存在**的索引集合。

    内容 / 队列索引只在各自开关为 `mongo` 时才要求 —— 否则 file 模式下
    `--check` 会因为「集合不存在」而误报缺失。
    """
    extra: tuple = ()
    if CONTENT_STORE == "mongo":
        extra += CONTENT_INDEXES
    if QUEUE_STORE == "mongo":
        extra += QUEUE_INDEXES
    return MEMBER_INDEXES + extra + FEEDBACK_INDEXES


def _describe(keys: list, opts: dict) -> str:
    s = ", ".join(f"{k}:{v}" for k, v in keys)
    if opts.get("unique"):
        s += " unique"
    if opts.get("partialFilterExpression"):
        s += " partial"
    return s


def ensure_indexes(db, verbose: bool = True, indexes: tuple | None = None) -> int:
    """创建清单里的全部索引（幂等）。返回**失败个数**（0 = 全就位）。

    抽成函数是为了让测试也能用**同一份索引定义** —— 否则测试里另写一套，
    两边迟早漂移（那样唯一索引就白建了）。
    `indexes=None` 时按当前配置取 `active_indexes()`；测试要覆盖内容索引时显式传入。
    """
    failures = 0
    for coll_name, keys, opts in (indexes if indexes is not None else active_indexes()):
        coll = db[coll_name]
        name = opts["name"]
        if name in {i["name"] for i in coll.list_indexes()}:
            if verbose:
                print(f"  ✓ {coll_name}.{name:18s} ({_describe(keys, opts)}) 已存在")
            continue
        try:
            coll.create_index(keys, **opts)
            if verbose:
                print(f"  ➕ {coll_name}.{name:18s} ({_describe(keys, opts)}) 已创建")
        except Exception as e:   # 同名索引定义冲突等
            print(f"  ✗ {coll_name}.{name:18s} 创建失败：{type(e).__name__}: {e}",
                  file=sys.stderr)
            failures += 1
    return failures


def check_indexes(db, verbose: bool = True, indexes: tuple | None = None) -> int:
    """只检查不创建。返回**缺失个数**（0 = 全就位）。"""
    missing = 0
    for coll_name, keys, opts in (indexes if indexes is not None else active_indexes()):
        name = opts["name"]
        if name in {i["name"] for i in db[coll_name].list_indexes()}:
            if verbose:
                print(f"  ✓ {coll_name}.{name:18s} ({_describe(keys, opts)}) 已存在")
        else:
            print(f"  ✗ {coll_name}.{name:18s} ({_describe(keys, opts)}) **缺失**")
            missing += 1
    return missing


def main() -> int:
    check_only = "--check" in sys.argv
    try:
        db = get_mongo_db()
        db.command("ping")
    except DBUnavailable as e:
        print(f"✗ 连不上 MongoDB：{e}", file=sys.stderr)
        return 2

    print(f"MongoDB 库 = {MONGO_DB}；模式 = {'只检查' if check_only else '创建/校验'}")
    print(f"内容后端 = CONTENT_STORE={CONTENT_STORE}（{'含' if CONTENT_STORE == 'mongo' else '不含'} projects 索引）")
    print(f"队列后端 = QUEUE_STORE={QUEUE_STORE}（{'含' if QUEUE_STORE == 'mongo' else '不含'} queue_tasks 索引）")
    print(f"期望索引 = {len(active_indexes())} 个\n")

    bad = check_indexes(db) if check_only else ensure_indexes(db)

    print()
    if bad:
        what = "缺失" if check_only else "创建失败"
        print(f"✗ {bad} 个索引{what} —— 上线前必须解决（唯一索引是幂等防线）")
        return 1
    if check_only:
        print("✓ 全部索引就位")
    else:
        print(f"✓ 完成。幂等规则：kind ∈ {IDEMPOTENT_KINDS} 的流水按 (user_id, ref, kind) 唯一")
    return 0


if __name__ == "__main__":
    sys.exit(main())
