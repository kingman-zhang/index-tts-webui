# 任务队列上库方案（B1：数据上库；调度仍单实例）

> 与 `STORAGE_MIGRATION_PLAN.md` **平级**，专管队列这一块（那里的 §9「二期」把队列列为 C，
> 并明确「后面单独设计」）。本文档写设计；命令照 `命令大全.md` 抄；上线照 `上线待办清单.md` 勾。

**一句话**：把任务队列的**持久化**从 `data/queue/*.json` 挪到门面 `app/queue_store.py`
后面（`QUEUE_STORE=file|mongo`），**调度逻辑一行不改**、仍是单实例；跨实例的抢占（租约）留给 B2。

---

## 0. 状态

| Gate | 内容 | 状态 | 提交 |
|---|---|---|---|
| **B1-1** | 存储门面 + 双后端（`queue_store.py` / `_file` / `_mongo`） | ✅ | 本次 |
| **B1-2** | 顺序模型：`_queue_order.json` → 每任务 `seq` 字段 | ✅ | 本次 |
| **B1-3** | 索引（`queue_tasks`）+ 迁移工具 `tools/migrate_queue.py` | ✅ | 本次 |
| **B1-4** | `QUEUE_WORKER_ENABLED` 执行器开关（防多实例互踩） | ✅ | 本次 |
| **B2** | 跨实例租约抢占（CAS + 心跳续租 + 过期回收 + 取消读库） | ⏳ 未做 | — |

---

## 1. 为什么做（动机）

**唯一动因 = 多实例部署。** 队列的权威状态原先全在进程内：

| 变量 | 含义 | 关键性 |
|---|---|---|
| `queue_tasks` | `task_id -> 完整任务对象`（**含 `lines` 正文**） | 列表/取消/重试的唯一数据源 |
| `queue_order` | 排队顺序（**全局**，含所有用户） | 调度顺序、`queue_position` |
| `running_ids` | **「有活执行器」的唯一权威标志** | 用它区分「真在跑」与「僵尸」 |
| `current_task_id` | 兼容展示（最早启动的运行中任务） | 前端 `current` |

磁盘只有两样：一任务一文件 + `_queue_order.json`（**纯 id 数组**）。后果：

- A 实例提交的任务，**B 实例看不见**（列表 404）。
- 取消 / 重试 / 拖拽打到 B **全部无效**。
- 但**不会重复合成** —— 内存天然隔离，各实例只调度自己内存里的任务。
  ⇒ 现状是「任务绑定到提交它的那台机器」。

**⚠️ 关键反直觉**：一旦把任务搬进库而调度不改，两个实例会同时抢同一个 `queued` 任务
⇒ **反而引入重复合成**。所以「上库」与「抢占」的先后顺序不能错：
**B1 只上库、并且用开关把多实例的队列明确关到只剩一台**；B2 才上抢占。

---

## 2. 分层：B1 做什么、不做什么

### B1 做

1. **持久化走门面**：`queue_state` 的 `persist_task` / `load_persisted_tasks` /
   `delete_persisted_task` / `persist_queue_order` **函数名与调用点全部不变**，内部
   转发到 `queue_store`（按 `QUEUE_STORE` 分派 file / mongo）。零业务语义改动。
2. **顺序模型换成 `seq`**（见 §3.2）——`_queue_order.json` 退役。
3. **索引与迁移工具**就位。
4. **`QUEUE_WORKER_ENABLED` 开关**：一台实例「接管队列」，其余实例不接管（见 §5）。

### B1 不做（＝B2 的范围）

- ❌ 跨实例抢占 / 心跳续租 / 租约过期回收 → **B2**
- ❌ **列表读路径改为查库**：`GET /api/queue` 仍读本实例内存。
  因此 **B1 之后仍是单实例部署**（对外承诺不变），B1 拿到的是：
  ① 任务不再依赖本地磁盘；② 重启恢复读库；③ 数据模型 + 索引 + 门面就位。
- ❌ 队列进 Redis：任务历史要长期可查 + 要按 kind/用户/状态多维过滤，
  Redis 做主存不合适（与「会话在 Redis」也不是一回事）。

---

## 3. 数据模型

### 3.1 集合

| 集合 | `_id` | 文档 |
|---|---|---|
| `queue_tasks` | `task_id`（`q_*` / `recovered_*`） | 任务全文，字段与今天的任务 JSON **逐字段一致** |

`_id` 就是业务主键（沿用一期/二期的约定），因此 upsert 天然幂等、`load_task` 一次主键查。
文档里同时保留业务字段 `id`（前端与后端到处读 `task["id"]`），两个后端都如此 —— 行为等价。

**不迁的东西**：`data/queue/bak_001~006/`（历史快照目录）留在磁盘，不迁、不删。

### 3.2 顺序：`_queue_order.json` → `seq`

原实现把顺序放在**一个**文件里（纯 id 数组）。上库后改成**每个任务自带 `seq`**：

| | 原实现 | B1 |
|---|---|---|
| 存储位置 | `data/queue/_queue_order.json`（一个全局数组） | 各任务文档的 `seq` 字段 |
| 顺序语义 | 数组下标 = 全局队列位次 | `seq` 升序 = 全局队列位次 |
| 插入队尾 | `list.append` | `seq = max(全部任务 seq) + 1` |
| 拖拽排序 | 重写整个数组 | 把**这些任务当前占用的 seq 槽位**按新顺序回填 |

**为什么不用「一个全局顺序文档」**：那只是把同一个可变共享点从文件搬到库里 ——
B2 里它既无法让两台实例原子地取队首，又变成写热点。**每任务 `seq` 才是队列该有的模型。**

**槽位回填为什么正确**：`reorder` 是「把 N 个任务重新填回它们原先占的那 N 个槽位」
（带 `kind` 时正是逐槽填回）。把这 N 个任务的 `seq` 值排序后按新顺序重发，等价于
同一组槽位换了主人 —— 其他用户任务的相对位置**一点不动**。见
`queue_state.persist_queue_order()`。

**`seq` 只保证相对顺序**：任务被取走后剩下的任务的 `seq` 不会重编号（今天也没有），
所以会有空洞；重启后按 `seq` 排序得到的相对顺序不变 ⇒ 正确。

**兼容老数据**：老任务的 `seq` 不存在时，`queue_store.load_legacy_order()` 读一次
`_queue_order.json` 当排序提示（file 后端才有；mongo 后端返回 `[]`，由
`tools/migrate_queue.py --backfill-seq` 在迁移时补上）。缺 `seq` 的任务排在有 `seq` 的后面，
内部按 `created_at` —— 与旧 `load_queue_order()` 的「先持久化顺序、再按 created_at 补队尾」等价。

### 3.3 索引（`CONTENT_STORE` 那套写法，仅 `QUEUE_STORE=mongo` 时要求存在）

```python
QUEUE_INDEXES = (
    ("queue_tasks", [("status", 1), ("seq", 1)],   {"name": "by_status_seq"}),
    ("queue_tasks", [("member_id", 1), ("created_at", -1)], {"name": "by_member_recent"}),
)
```

- `by_status_seq` —— **B2 的调度索引**：`find_one_and_update({status:"queued"}, sort=[("seq",1)])`
  一次原子取队首。B1 用不到（`load_all_tasks` 是全表扫，启动一次、量级小），
  但**索引属于 schema**，随数据模型一起声明比事后偷偷补好。
- `by_member_recent` —— 列表查询形状（某用户 + 时间倒序）。

---

## 4. 门面接口（`app/queue_store.py`）

刻意**极小**——只有「一个任务一份文档」这一种形态，保持与 file 后端可逐项等价：

```python
save_task(doc: dict) -> None          # 整份覆盖（upsert by _id = doc["id"]）
load_task(task_id) -> dict | None
load_all_tasks() -> list[dict]        # 启动恢复用
delete_task(task_id) -> bool
load_legacy_order() -> list[str]      # 老 _queue_order.json 的排序提示（mongo 恒 []）
```

`_REQUIRED_API` 契约在 import 期校验（与 `stores.py` / `store.py` 同一套写法）：
**非法取值启动即报错，不静默回退** —— 两后端存储形态不同，静默切换会让用户看到
「任务全没了」。

### 顺带修掉的一个既有缺陷

原 `persist_task` / `persist_queue_order` 是 `path.write_text(...)` **直写、非原子**：
崩在写一半就是 JSON 截断 → 启动 `json.loads` 失败只打 warning → **该任务从内存消失、
文件永久留盘、用户看不到也删不掉**。

新的 file 后端复用 `app/json_store.py` 的 `write_json`（tmp + `os.replace`），
**内容一字不差、写盘变原子**。这是 B1 捆绑的修复，不是新语义。

---

## 5. 开关

```ini
# 队列存储后端：file（默认，与历史完全一致）| mongo
QUEUE_STORE=file
# 本实例是否接管任务队列：1（默认）= 加载 / 恢复 / 调度；0 = 完全不碰
QUEUE_WORKER_ENABLED=1
```

### `QUEUE_WORKER_ENABLED=0` 的语义（**B1 的防互踩保险**）

一旦存储共享，第二个实例启动时会**破坏**第一个实例的运行中任务：它把库里
`status=running` 的任务读进内存，因为「没有 `tts_task_id`」而**改判 `INTERRUPTED`**，
之后用户点删除走进「终态分支」，就会**把另一台正在写的任务从库里删掉**。

B1 还没有租约，无法判断「那台是不是活着」，所以**必须显式声明**：

| | `=1`（默认） | `=0` |
|---|---|---|
| 启动加载队列 | 是 | **否**（`queue_tasks` 恒空） |
| 恢复轮询 / 从 TTS 补任务 | 是 | 否 |
| `seg_cache.prune`（孤儿清理） | 是 | **否**（不加载 ⇒ 会把自己的缓存全判成孤儿） |
| 自动调度 | 是 | 否 |
| `GET /api/queue` | 正常 | 空列表 |
| `POST /api/queue/submit` | 正常 | **503「本实例未启用任务队列」** |

**部署铁律（B2 之前）**：多实例时**只能在一台**上 `QUEUE_WORKER_ENABLED=1`。
`/api/version` 的 `queue_worker_enabled` 报的就是它 —— 不靠猜 `.env`。

---

## 6. 语义变更清单（review 时只看这一张表）

| # | 变更 | 影响面 | 判据 |
|---|---|---|---|
| 1 | `_queue_order.json` 退役 → 任务 `seq` 字段 | 内部，无对外接口变化 | `queue_position` 数值不变（仍是内存 `queue_order` 下标 +1） |
| 2 | file 后端写盘改原子（tmp + rename） | 只会变好 | 崩在写一半不再丢任务 |
| 3 | `queue_state.queue_file()` 删除 | 仅测试引用（已改为 `queue_store_file.task_path`） | `grep queue_file` 无残留 |
| 4 | 新增 `QUEUE_STORE` / `QUEUE_WORKER_ENABLED` | 默认值 = 历史行为 | file + 开 = 与改造前逐项一致 |
| 5 | 老数据缺 `seq` 时首次启动会补写一次（顺序按 `_queue_order.json` → `created_at`） | 一次性 | 日志 `缺 seq，已…补写` |

---

## 7. 上线与回滚

**上线**（详见 `上线待办清单.md` D 段）：

```bash
# 1) 先补基础镜像的 pymongo（与账本/内容同一个前置坑）
# 2) 迁数据（幂等、只加不减、可先 --dry-run）
python tools/migrate_queue.py --dry-run
python tools/migrate_queue.py
# 3) 建索引
python tools/init_indexes.py --check     # 缺失会退出码 1
python tools/init_indexes.py
# 4) 改 .env：QUEUE_STORE=mongo → 重启 → 核对取值域
curl -s localhost:3001/api/version | jq '.queue_store_backend, .queue_worker_enabled'
```

**回滚**：`QUEUE_STORE=file` + 重启，**无需反向迁移** —— file 后端的
`data/queue/*.json` 一直在原地（B1 不会删它），只是不再是真源。回滚后新产生的任务
会写回文件（老任务仍是切库前那份，可能落后一轮，人工确认即可）。

---

## 8. 已知边界（B1 之后仍然成立）

1. **仍是单实例部署**：列表/取消/重试都只作用于本实例内存 ⇒ 多实例时
   `QUEUE_WORKER_ENABLED` 只能在一台开。
2. **没有租约**：进程被 kill -9 后，库里那条 `running` 要靠「下次启动的
   `INTERRUPTED` 改判」才恢复（与今天一致）；跨实例无人回收。
3. **`persist_task` 是同步写**（file 是本地 IO、mongo 是阻塞 IO），都在事件循环里跑。
   与今天同形；因为进度写盘早有节流（2s / 进度步长 / 收尾），频率可控。
   真要挪线程池，属 B2/后续优化。
4. **音频资产与音色元数据仍留本地磁盘**（`data/outputs/`、`data/voices/`）——
   这部分不在数据面，见 `STORAGE_MIGRATION_PLAN.md` §9 的「未纳批」项。
