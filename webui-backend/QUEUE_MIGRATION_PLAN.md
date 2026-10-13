# 任务队列上库方案（B1：数据上库；调度仍单实例）

> 与 `STORAGE_MIGRATION_PLAN.md` **平级**，专管队列这一块（那里的 §9「二期」把队列列为 C）。
> 本文档写设计；命令照 `命令大全.md` 抄；上线照 `上线待办清单.md` 勾。

**一句话**：把任务队列的**持久化**从 `data/queue/*.json` 挪到门面 `app/queue_store.py`
后面（`QUEUE_STORE=file|mongo`），**调度逻辑一行不改**、仍是单实例。

**🔴 决策（2026-10-13）：跨实例抢占（B2）不做，改为挂起。**
当前没有用户量、请求也不多，多实例要付的代价（租约 / 心跳 / 过期回收 / 读路径改造 /
**共享音频资产**）换不来任何收益。**等单实例真的撑不住时再启动。**
本次把目标收敛成两件事：**任务数据上库** + **可统计**（在跑多少 / 近一周任务量）。
详见 §1.1 与 §9。

---

## 0. 状态

| Gate | 内容 | 状态 | 提交 |
|---|---|---|---|
| **B1-1** | 存储门面 + 双后端（`queue_store.py` / `_file` / `_mongo`） | ✅ | `893d636` |
| **B1-2** | 顺序模型：`_queue_order.json` → 每任务 `seq` 字段 | ✅ | `893d636` |
| **B1-3** | 索引（`queue_tasks`）+ 迁移工具 `tools/migrate_queue.py` | ✅ | `893d636` |
| **B1-4** | `QUEUE_WORKER_ENABLED` 执行器开关（防多实例互踩） | ✅ | `893d636` |
| **B1-5** | **软删除** + **字段级 `$set`** + **`created_ts`（统计用时间字段）** | ✅ | `e976f51` |
| **B2** | 跨实例租约抢占（CAS + 心跳续租 + 过期回收 + 取消读库） | ⏸ **挂起**（用户量上来再说） | — |

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

### 1.1 为什么 B2 被挂起（2026-10-13 决策）

多实例要付的代价，分三层 —— 第 3 层是最容易漏的那个：

| 层 | 要解决什么 | 状态 |
|---|---|---|
| **数据面** | 任务状态放哪、谁是真源 | ✅ B1 已做（写走库；读仍读内存） |
| **调度面** | 一个任务只能被一个实例跑 | ⏸ 未做（＝B2：CAS 抢占 / 心跳续租 / 过期回收 / 读路径改查库） |
| **资产面** | 跑出来的东西放哪、别的实例能不能拿到 | ❌ **未做，且是「能多实例」的真正门槛** |

**资产面为什么是门槛**：mono / podcast 是在 backend **进程内**合成的（不像老的
`resume_polling` 有 `tts_task_id` 可以跨进程接管）⇒ ① 任务一旦被抢走，**必须由那台跑完**；
② 音频落**本机** `data/outputs/`、段缓存落本机 ⇒ 不共享就会出现「A 实例跑完的任务，
用户在 B 实例下载不到」、断点续传换一台即失效。

⇒ 也就是说：**调度面做完也只是「调度上多实例」，不共享资产就不是真的能扩容。**
三层的成本加起来，在「没有用户量」的当下是纯支出 —— 所以挂起，等单实例真的撑不住再启动。

**挂起不等于白做**：B1 的数据面、`seq` 顺序模型、门面抽象，都是 B2 的前置（§9.4）。

---

## 2. 分层：B1 做什么、不做什么

### B1 做

1. **持久化走门面**：`queue_state` 的 `persist_task` / `load_persisted_tasks` /
   `delete_persisted_task` / `persist_queue_order` **函数名与调用点全部不变**，内部
   转发到 `queue_store`（按 `QUEUE_STORE` 分派 file / mongo）。零业务语义改动。
2. **顺序模型换成 `seq`**（见 §3.2）——`_queue_order.json` 退役。
3. **索引与迁移工具**就位。
4. **`QUEUE_WORKER_ENABLED` 开关**：一台实例「接管队列」，其余实例不接管（见 §5）。
5. **上库后的三处收口**（B1-5，见 §9）—— 都是「上库之后才暴露出来的问题」：
   - **软删除**：`delete_task` 只打 `deleted_at`，不再从库里真删。否则用户点一次
     「清空已完成」，「近一周任务量」的统计历史就跟着没了。
   - **字段级 `$set`**：`lines` 占任务体积 81%，而进度更新只改几个小字段 ——
     整份覆盖等于让 80%+ 的写流量服务于 5% 的内容。
   - **`created_ts`（BSON Date）**：`created_at` 是**无时区字符串**，范围查能用、
     按天/周聚合不能用；另存一个真正的时间字段供统计。

### B1 不做（＝B2 的范围，**已挂起** —— 见 §1.1）

- ⏸ 跨实例抢占 / 心跳续租 / 租约过期回收 → **B2**
- ⏸ **列表读路径改为查库**：`GET /api/queue` 仍读本实例内存。
  因此 **当前仍是单实例部署**（对外承诺不变），现在拿到的是：
  ① 任务不再依赖本地磁盘；② 重启恢复读库；③ 数据模型 + 索引 + 门面就位；④ 可统计。
- ⏸ **共享音频资产**（`data/outputs/`、段缓存）—— 真正的扩容门槛，见 §1.1。
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

**两个「后端自治字段」**（mongo 后端独有，**不返回给上层、不参与上层写入**）：

| 字段 | 类型 | 写入时机 | 干什么 |
|---|---|---|---|
| `created_ts` | BSON Date（UTC） | `$setOnInsert`（**只在首次插入**） | 统计：按天/周聚合、时间范围查（`created_at` 是无时区字符串，聚合用不了） |
| `deleted_at` | BSON Date（UTC） | `delete_task` 软删除时 | 标记删除；读接口一律 `{deleted_at: {$exists: false}}` 过滤 |

⇒ 因此 `queue_tasks` 的**可见集合 = 没有 `deleted_at` 的文档**，其余一律视为已删除。
`_clean()` 在返回前把这两个字段（连同 `_id`）剥掉，所以内存里的任务对象与
`file` 后端读出来的**逐字段一致**，上游代码零感知。

**为什么 `created_ts` 要单独存在**：`created_at` 是 `datetime.now().isoformat()` 的产物
—— **本地时间、不带时区**。它的字典序等于时间序，所以 `{$gte: "..."}` 这类范围查能用；
但 `$dateToString` / `$dateTrunc` 只接受 Date 类型，**按天/周分组会直接报错**。
迁移工具按「naive 值 = 本地时间」解析后转 UTC 显式写入（见 §9.3）。

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
    ("queue_tasks", [("status", 1), ("seq", 1)],            {"name": "by_status_seq"}),
    ("queue_tasks", [("status", 1), ("deleted_at", 1)],     {"name": "by_status_deleted"}),
    ("queue_tasks", [("member_id", 1), ("created_at", -1)], {"name": "by_member_recent"}),
    ("queue_tasks", [("created_ts", -1)],                   {"name": "by_created_ts"}),
)
```

- `by_status_seq` —— 调度取队首用（`find_one_and_update({status:"queued"}, sort=[("seq",1)])`
  是 B2 的形态）。现在用不到（`load_all_tasks` 全表扫，启动一次、量级小），
  但**索引属于 schema**，随数据模型一起声明比事后偷偷补好。
- `by_status_deleted` —— 统计「在跑多少」＝ `count({status:"running", deleted_at:{$exists:false}})`。
- `by_member_recent` —— 列表查询形状（某用户 + 时间倒序）。
- `by_created_ts` —— 「近一周任务量」这类**时间范围 / 分天聚合**的统计。

---

## 4. 门面接口（`app/queue_store.py`）

刻意**极小**——只有「一个任务一份文档」这一种形态，保证 file / mongo **对外语义**一致：

```python
save_task(doc: dict) -> None          # 写入 / 覆盖（upsert by _id = doc["id"]）
load_task(task_id) -> dict | None     # 不存在**或已删除** ⇒ None
load_all_tasks() -> list[dict]        # 全部**未删除**任务（启动恢复用）
delete_task(task_id) -> bool          # 从系统中移除；返回「本次是否生效」
load_legacy_order() -> list[str]      # 老 _queue_order.json 的排序提示（mongo 恒 []）
```

**「逐项等价」指语义，不指实现**：

| | file 后端 | mongo 后端 |
|---|---|---|
| `save_task` | 整份写文件 | `$set` 小字段 + 大字段变了才写（指纹） |
| `delete_task` | `unlink` 真删文件 | 打 `deleted_at` **软删**（读接口过滤） |
| 额外字段 | 无 | `created_ts` / `deleted_at`（**上层看不到**） |

上层只依赖「删掉的任务不再可见」这一条，不依赖实现方式。

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
| 6 | **mongo 的 `delete_task` 变软删**（打 `deleted_at`，不再真删文档） | **统计口径**：库里会累积已删除记录；读接口行为不变 | 删除后 `load_task` → None，但 `db.queue_tasks.find_one({_id})` 仍在且带 `deleted_at` |
| 7 | **mongo 的 `save_task` 变字段级 `$set`**（大字段变了才写） | 无（写入量下降） | 进度更新不动 `lines`；改稿/重试仍写全文 |
| 8 | **新增 `created_ts`（BSON Date）** | 仅 mongo；上层看不到 | `db.queue_tasks` 每条有 `created_ts`，且 `$type: "date"` |
| 9 | 新增队列索引 `by_status_deleted` / `by_created_ts` | 仅 mongo | `init_indexes.py --check` 期望 4 个 `queue_tasks` 索引 |

⚠️ 第 6 条的**连带影响要在使用侧记住**：`DELETE /api/queue/{id}` 对**排队中**的任务是
「取消」（状态转 `cancelled`、记录保留），对**终态**任务才是移除；而 `DELETE /api/queue`
（前端「清空已完成」）走的是批量移除。这三处现在都只会软删，所以**历史永远查得到**。

---

## 7. 上线与回滚

**上线**（详见 `上线待办清单.md` D 段）：

```bash
# 1) 先补基础镜像的 pymongo（与账本/内容同一个前置坑）
# 2) 迁数据（幂等、只加不减、可先 --dry-run）
python tools/migrate_queue.py --dry-run    # 会报「created_ts 可解析 N/M」
python tools/migrate_queue.py              # 真灌；顺带补 seq 与 created_ts
# 3) 建索引
python tools/init_indexes.py --check     # 缺失会退出码 1
python tools/init_indexes.py
# 4) 改 .env：QUEUE_STORE=mongo → 重启 → 核对取值域
curl -s localhost:3001/api/version | jq '.queue.backend, .queue.worker_enabled'
```

⚠️ **迁移时不要漏了 `created_ts`**：它是「近一周任务量」的唯一可用字段。迁移工具会从
`created_at` 按「本地时间」解析后转 UTC 写入；**解析失败的任务会被记进输出**（那几个
只能退回插入时刻，时间会不准）。

**回滚**：`QUEUE_STORE=file` + 重启，**无需反向迁移** —— file 后端的
`data/queue/*.json` 一直在原地（不会删它），只是不再是真源。回滚后新产生的任务
会写回文件（老任务仍是切库前那份，可能落后一轮，人工确认即可）。
⚠️ 反向也要留意：**切到 mongo 后软删的任务，切回 file 时会「复活」** ——
file 后端读的是它自己的文件，跟 mongo 的 `deleted_at` 没关系。

---

## 8. 已知边界（仍然成立）

1. **仍是单实例部署**：列表/取消/重试都只作用于本实例内存。多实例部署（B2）
   **已决定挂起**，见 §1.1；在此之前多实例时 `QUEUE_WORKER_ENABLED` 只能一台开。
2. **没有租约**：进程被 kill -9 后，库里那条 `running` 要靠「下次启动的
   `INTERRUPTED` 改判」才恢复（与今天一致）；跨实例无人回收。
3. **`persist_task` 是同步写**（file 是本地 IO、mongo 是阻塞 IO），都在事件循环里跑。
   与今天同形；因为进度写盘早有节流（2s / 进度步长 / 收尾），频率可控。
   真要挪线程池，属后续优化。
4. **音频资产与音色元数据仍留本地磁盘**（`data/outputs/`、`data/voices/`）——
   这部分不在数据面，见 `STORAGE_MIGRATION_PLAN.md` §9 的「未纳批」项，
   以及本文档 §1.1 的「资产面」。
5. **软删除会让库持续增长**：任务不会被真正删掉。实测单任务均 7.7 KB、最大 38 KB，
   1000 条约 8 MB —— 这是「统计历史不丢」的代价，**可接受，不做归档**。
   若将来要清理，先决定「统计要不要保留那段时间」，再定策略（见 §9.2）。

---

## 9. 上库后的三处收口（B1-5，2026-10-13）

这三件事都不是「优化」，而是**上库之后才暴露出来的正确性问题**。

### 9.1 字段级 `$set`（写放大）

原 `save_task` 是 `replace_one`（整份覆盖）。而：

| 事实 | 数值 |
|---|---|
| `lines` 占任务文档体积 | **81%**（实测 15 条任务：116 KB 里 94 KB 是 lines） |
| 单任务平均 / 最大 | 7.7 KB / 37.8 KB |
| 进度更新频率（`mono_runner` 节流后） | ≤ 1 次 / 2s |

⇒ 一个 10 分钟的任务最多写 ~300 次，每次都在重写那 81%。**80%+ 的写流量服务于 5% 的内容。**

**做法**：把「大且低频」的字段挑出来（`queue_store_mongo._PAYLOAD_FIELDS` =
`lines` / `voices` / `silence` / `params`），**只在内容真变了时才写**（进程内记 SHA1 指纹
`_payload_sig`）；其余字段（都很小）照常每次都写。

**为什么只挑大字段做指纹**：指纹判错会让「某个字段静默写不进去」。把范围压到 4 个大字段，
就把它压缩成「这 4 个字段要么整批写、要么整批跳过」，风险面最小。

**⚠️ 绝不能退回 `replace_one`**：整份替换会把 `deleted_at` 抹掉 —— 已删除的任务复活。
`$set` 天然安全（没列出的字段不动）。测试里有这条回归（`test_queue_store.py`
「对已删除任务再 save 不会抹掉 deleted_at」）。

### 9.2 软删除（统计历史）

`DELETE /api/queue`（前端「清空已完成」）原先是真删库 ⇒ **用户点一次，前几周的任务量
就统计不到了**。而「近一周任务量」正是本次要做的事。

**做法**：mongo 后端的 `delete_task` 只打 `deleted_at`；`load_task` / `load_all_tasks`
统一 `{deleted_at: {$exists: false}}` 过滤。所以**上层语义完全不变**（任务「不存在了」），
但库里留档。

**file 后端保持真删**（`unlink`）：本地文件没有「标记删除」的载体，而软删除要解决的是
「统计历史不能丢」—— 那是 mongo 后端的诉求（统计靠查库）。两后端的**对外语义一致**。

⚠️ **代价**：库只增不减。按实测体量（8 MB / 1000 条）**可接受，不做归档**。
若将来要清理，有三条路，按代价递增：
1. 接受现状（最简单）；
2. 定期把老记录搬到 `queue_tasks_archive`；
3. 用 TTL 索引按 `deleted_at` 自动过期**已删除**的记录（`expireAfterSeconds`）——
   ⚠️ TTL 索引会把文档**真删**，等于放弃那部分历史，是「限定期限的保留策略」。

### 9.3 `created_ts`（能聚合的时间字段）

`created_at` 是 `datetime.now().isoformat()`：**本地时间、无时区**。
- 范围查 ✅ 能用（ISO 字典序 == 时间序）
- 按天/周聚合 ❌ `$dateToString` / `$dateTrunc` 只接受 Date 类型
- 时区 ❌ 没有任何时区信息，换服务器时区就整体错位

**做法**：mongo 后端用 `$setOnInsert` 写入 `created_ts`（BSON Date，UTC）；
迁移工具把老任务的 `created_at` **按本地时区解析后转 UTC** 显式写入
（naive datetime 的 `astimezone()` 正是「视为本地时间」的语义）。

⚠️ **BSON Date 是毫秒精度**，而 Python `datetime` 是微秒 —— 核对时先截断到毫秒再比
（实测 `.138481` → `.138`），否则会误判成「不一致」。

实测（真实 dev 数据 15 条迁移进 `podcast_test`）：

```
核对 15 条：毫秒精度下与 created_at 解析值不一致的有 0 条
时区换算抽样：17:06 本地 → 09:06 UTC（-8h）
按天聚合（这条 pipeline 就是「近一周任务量」）：
  2026-09-20  6 个任务 / 2026-09-21  2 个 / 2026-09-30  4 个 / 2026-10-10  3 个
```

### 9.4 这些给 B2 留了什么

挂起 B2 不代表白做：数据面（`queue_tasks` + `_id = task_id`）、顺序模型（`seq`）、
门面抽象（换后端不改上层）、`by_status_seq` 索引，全是 B2 的前置。
真要启动 B2 时，**只剩「加租约字段 + CAS 取队首 + 读路径改查库」**，不用再动数据模型。

### 9.5 统计怎么查（用户自行查库）

不做统计工具/接口 —— 直接查库即可。常用两条（详见 `命令大全.md`）：

```js
// 现在有多少任务在跑（含排队）
db.queue_tasks.countDocuments({ status: { $in: ["running", "queued"] },
                                deleted_at: { $exists: false } })

// 近 7 天每天提交了多少任务
db.queue_tasks.aggregate([
  { $match: { created_ts: { $gte: new Date(Date.now() - 7*864e5) } } },
  { $group: { _id: { $dateToString: { format: "%Y-%m-%d", date: "$created_ts",
                                      timezone: "+08:00" } },
              n: { $sum: 1 } } },
  { $sort: { _id: 1 } },
])
```

⚠️ **两个口径要分清**：
- 「**任务量**」＝ 提交了多少 → 用 `created_ts`，**不要**过滤 `deleted_at`
  （软删的任务也是发生过的任务）；
- 「**当前列表里有多少**」→ 要加 `deleted_at: {$exists: false}`。

`timezone: "+08:00"` 是给「按天分组」用的（`created_ts` 存的是 UTC，不写时区会按 UTC 切天）。
