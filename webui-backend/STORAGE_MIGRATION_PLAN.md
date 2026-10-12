# 存储改造计划：把「账本」搬进 MongoDB，会话搬进 Redis

> 起因：准备接入真实支付。现状是纯 JSON 文件 + 进程内 `threading.Lock`。
> 本文回答两个问题：**要不要上数据库**、**怎么上**。
> 撰写依据：`app/membership/{store,service,routes,pay}.py`、`app/{stores,queue_state,voice_store}.py`、
> `app/main.py`、`app/config.py` 的实际代码，以及 `data/members/` 的真实数据形态（2026-10-12 实测）。

---

## 0. 结论先行

**是，但不是"全迁"，而是按一致性要求分层。**

| 数据 | 去向 | 理由 |
|---|---|---|
| **订单 / 积分流水 / 用户余额 / 优惠码 / 签到** | **MongoDB（权威账本）** | 真钱不可逆，必须原子、幂等、可对账、抗多实例 |
| **会话 token** | **Redis（带 TTL）** | 每请求全量读 JSON 是明确的扩展性瓶颈；TTL 自动过期 |
| 项目 / 术语表 / 音色元数据 / 收藏 | **暂留文件**（二期再议） | 与钱无关，迁移收益低、风险不为零 |
| 音频成品 / 分段缓存 / 模型权重 / 参考音频 | **永远留磁盘** | 大文件资产，塞进库是反模式（详见 §3.4） |

**关键澄清：触发上库的不是"数据太多"。**
当前真实数据量——**2 个用户、21 条会话、77 条积分流水、5 个优惠码、0 笔订单**（`orders.json` 至今不存在）。
这个体量下 JSON 文件**完全够用**，且单进程部署下现有代码**能正常工作**。

**真正的触发点是"要接真实支付"**：钱一旦收进来，故障代价从"页面报错"变成"用户的账对不上"，且不可逆。
这要求三件 JSON 文件给不了的事：**跨读写的原子性、跨进程的互斥、可事后对账的账本**。

> ✅ **前置条件已全部验证通过**（2026-10-12 21:40 实测）：Mongo / Redis 均连通，权限、拓扑、
> 命名空间全部确认 —— 详见 §5.1 实测结果。
> **需要你拍板的只剩两项**：本期范围、方案路径（§8）。

---

## 1. 现状盘点（事实，非推断）

### 1.1 存储对象清单

| 数据对象 | 当前形态 | 读写模式 | 并发保护 | 风险 |
|---|---|---|---|---|
| 用户表 | `data/members/users.json` | **整份读 → 改 → 整份写** | `threading.RLock`（store）+ `user_lock`（业务） | 🔴 高 |
| 积分流水 | `data/members/point_logs.json` | **整份读 → `insert(0)` → 整份写** | 同上 | 🔴 高 |
| 订单 | `data/members/orders.json` | 整份读改写 | `_order_lock`（进程级） | 🔴 高 |
| 会话 | `data/members/sessions.json` | **每请求整份读** | 仅 store 级单次原子 | 🟠 中高 |
| 优惠码 | `data/members/redeem_codes.json` | 整份读改写 | `_redeem_lock` + `user_lock` | 🟠 中 |
| 签到 | `data/members/checkins.json` | 整份读改写 | `user_lock` | 🟡 中 |
| 邮箱验证码 | `data/members/email_codes.json` | 整份读改写 | store 级 | 🟢 低 |
| 队列任务 | `data/queue/{task_id}.json` | 一任务一文件，**节流写盘** | `asyncio.Lock` | 🟢 低 |
| 项目 / 术语表 / 收藏 | `data/projects/*.json` 等 | 单文件读写 | 无 | 🟢 低 |

写入策略已经是 `tmp + os.replace`（原子替换），这一点做得对 —— **问题不在"单次写是否原子"，而在"跨多次写是否原子"**。

### 1.2 部署形态

- 启动方式：`server.py` → `app/main.py:run()` → **`uvicorn.run(app, host, port)`，未传 `workers`** ⇒ **当前是单进程单 worker**。
- 这意味着：**现有 `threading.Lock` 在今天的部署下是有效的**。
- 代码里也已如实标注这个边界（`service.py:347`、`MEMBERSHIP.md` §五）：
  > "只覆盖**单进程**；将来若上多 worker，需换成文件锁（fcntl）并复查当日记录。"

---

## 2. 问题诊断（按"接支付前是否必须解决"分级）

### 🔴 P0 — 接真实支付前必须解决

**P0-1｜跨文件写入非原子，崩在中间就账实不符**

`settle_order()`（`service.py:612`）一次入账要写**三处**：
```
① users.json        余额 +points
② point_logs.json   追加 purchase 流水
③ orders.json       状态 pending → paid
```
三步之间没有任何原子性保证。若进程在 ① 与 ② 之间被杀（容器 OOM、重启、部署），结果是**余额涨了但没有流水**——
而现有代码明确把流水当作幂等判据（`_has_order_credit()`，`service.py:603`），于是：
> 用户付了钱 → 余额加了 → 流水没写 → 平台重推回调 → `_has_order_credit()` 返回 False → **再发一次积分**。

代码作者显然意识到这点，写了补偿分支（`service.py:636`「上次崩在已入账、未改状态之间」），但补偿只能覆盖"先写流水后崩"，覆盖不了"先改余额后崩"。

**P0-2｜锁的作用域绑死在单进程上**

`_user_locks` / `_order_lock` / `_redeem_lock` 全是 `threading.RLock`。
- 今天：单 worker，够用。
- 明天：只要为了扛并发把 `workers` 改成 2（或起第二个容器做滚动发布），**同一订单会被两个进程同时入账**——因为两个进程各有一把自己的锁，互不可见。
- 支付场景下这是**直接的资金损失**，且是"低概率但必然发生"（支付平台的回调重试天然会并发）。

**P0-3｜`MEMBER_MOCK_PAY=1` 是当前默认值**

`service.py:53`：`MOCK_PAY = os.environ.get("MEMBER_MOCK_PAY", "1") == "1"`。
任何登录用户都能 `POST /api/points/orders/{id}/mock-pay` 给自己的订单白拿积分。
**上线前必须置 0**，且建议把默认值本身改成 `"0"`（默认安全，而非默认便利）。

### 🟠 P1 — 用户上量后立刻暴露

**P1-1｜每个 API 请求全量反序列化两个 JSON**

`resolve_token()`（`service.py:106`）：
```python
sessions = store.load_sessions()      # 整份读 + json.loads
user     = store.load_users().get(...)  # 又整份读 + json.loads
```
所有带鉴权的请求都要走这一遍。复杂度 O(会话数 + 用户数) **每请求**。
现在 21 条会话 → 无感；到 1 万用户时，每次接口调用都要解析几百 KB JSON。**这是纯浪费，且随规模线性恶化。**

**P1-2｜流水是"头插 + 整份重写"**

`append_point_log()`：`logs.insert(0, entry)` 然后整份写回。
每加一条流水 = **O(n) 全量重写**，且 `_trim_logs()` 还要再整份读一次找该用户的记录。
77 条无感；10 万条时每次积分变动要重写数 MB 文件。

**P1-3｜"先查后写"的幂等判据本身不幂等**

`_has_refund()`、`_has_order_credit()` 都是「全表扫描 → 判断 → 再写」，**中间没有持锁**：
```python
if _has_refund(user_id, refund_ref):   # ← 查
    return
_apply_delta(...)                      # ← 写（此处才拿锁）
```
并发两个退款请求可同时通过检查。现有防护是 `task["points_charged"] = 0` 置零，仍是"读改写下一次"。

**P1-4｜余额预检在锁外（TOCTOU）**

`charge_for_task()`（`service.py:751-759`）：先 `load_users()` 检查余额，**再**调 `_apply_delta()`（后者内部才拿锁、才重新读）。
`rebalance_task_charge()`（`service.py:794-800`）同样。
单进程下 `_apply_delta` 里的 `new_balance < 0` 检查兜住了"不会扣成负数"，但**余额判断与实际扣减之间存在窗口**，并发时会给出错误的"积分不足"。

### 🟡 P2 — 已知、可接受、有空再说

- `_trim_logs()` 为了截断直接访问 `store._lock` / `store._read()` / `store._write()`（私有 API）——**抽象层已经泄漏**，说明这套文件存储的接口设计已到尽头。
- `settle_by_out_trade_no()` 按 `out_trade_no` **扫全表**找订单（`service.py:670-677`）。
- 分页全靠内存切片（`list_orders` / `list_point_logs`），没有索引概念。
- 密码哈希用 pbkdf2（`security.py`）——**这个没问题，迁移时原样保留**。

---

## 3. 目标架构

### 3.1 分层图

```
                    ┌───────────────────────────────┐
   HTTP 请求 ──────▶ │  FastAPI (uvicorn, 可多 worker) │
                    └───────┬───────────────┬───────┘
                            │               │
              ┌─────────────▼─────┐   ┌─────▼──────────────┐
              │   MongoDB         │   │   Redis            │
              │   「权威账本」      │   │   「会话 + 协调层」  │
              │                   │   │                    │
              │ users (含余额)     │   │ session:{token}    │
              │ orders            │   │ rate:email:{addr}  │
              │ point_logs        │   │ lock:*（如需）      │
              │ redeem_codes      │   │                    │
              │ checkins          │   │ 丢数据=用户登出     │
              │                   │   │ （可接受）          │
              │ 丢数据=账目丢失     │   │                    │
              │ （不可接受）        │   └────────────────────┘
              └───────────────────┘
                            │
              ┌─────────────▼──────────────────────┐
              │  本地磁盘（不变）                    │
              │  音频 / 分段缓存 / 模型 / 参考音频    │
              │  项目 json / 术语表 / 队列任务 json   │
              └────────────────────────────────────┘
```

### 3.2 核心设计原则：**用数据库的原子能力消灭锁**

这是本方案与"把 `threading.Lock` 换成 Redis 锁"的分水岭。

> **Gate 0 已确认本实例是单节点 MongoDB（无副本集）⇒ 多文档事务不可用。**
> 所以下面的设计不是"更优选择"，而是**这里唯一可行的正确做法**。

**不要**把锁搬到 Redis（Redis 锁有续期、脑裂、时钟漂移问题，运维成本高）。
**要**把每一个"读-判断-写"重构成**单条原子写**，从根上不需要锁：

| 现在（读改写 + 锁） | 改造后（单条原子操作） |
|---|---|
| `load_users()` → 查余额 → 扣减 → `save_users()` | `find_one_and_update({_id: uid, points: {"$gte": cost}}, {"$inc": {"points": -cost}})`<br>→ **返回 None 即余额不足**，一次往返、天然并发安全 |
| `_has_order_credit()` 扫流水 → 判断 → 发分 | `orders` 集合按 `order_id` 建**唯一索引**；用 CAS 抢状态：<br>`find_one_and_update({order_id, status: "pending"}, {"$set": {"status": "paid"}})`<br>→ **抢到才发分，抢不到直接返回 already_paid** |
| `checkin()` 查当天记录 → 发分 | `checkins` 集合建 `{user_id, date}` **唯一索引**，插入成功才发分 |
| `redeem()` 查码 → 占坑 → 写回 | 用 `$inc: {used: 1}` 配合条件 `{"used": {"$lt": max_uses}}`<br>+ 独立集合记 `{code, user_id}` 唯一索引防同一人重复兑 |
| `_has_refund()` 查 → 退 | `point_logs` 集合对 `ref + kind` 建**唯一索引**，插入冲突即已退过 |
| `_trim_logs()` 手工截断 | 不需要了 —— 流水是 append-only 集合，按 `created_at` 索引分页 |

**收益**：P0-2（多进程）自动消失、P1-3 / P1-4（TOCTOU）自动消失、P1-2（O(n) 重写）自动消失。
**代价**：`service.py` 里的锁代码（约 30 行）全部删除，改为原子操作。

> **唯一索引不是"顺便加的优化"，它是资金安全的第一道防线。**
> 「先查后写」无论加什么锁都有窗口；数据库唯一约束没有窗口。

### 3.3 为什么 Redis 是**可选**的（诚实评估）

你已提供 Redis，但我要说清楚它到底带来什么，避免为了用而用：

| Redis 用途 | 若不用 Redis，Mongo 能否替代 | 建议 |
|---|---|---|
| 会话存储 | ✅ 能：Mongo 加 **TTL 索引**（`expireAfterSeconds`）自动过期 | 一期可用 Mongo，二期再挪 |
| 支付回调幂等去重 | ✅ 能：唯一索引已解决（更硬） | **不需要 Redis** |
| 任务扣费互斥 | ✅ 能：`$inc` 原子操作已解决 | **不需要 Redis** |
| 邮箱验证码限流（60s/次、10 次/天） | ⚠️ 能但笨重（要维护计数文档） | **Redis 更顺手** |
| 多实例部署时的通用互斥 | ✅ 大部分场景已被唯一索引覆盖 | 一期不必 |

**结论**：Redis 一期的**刚需只有"会话 + 限流"两处**，且两处都有 Mongo 替代方案。
所以本方案把 Redis 列为 **Gate 3（可选/二期）**，不与"账本上库"捆绑 —— 降低一次性改动面与回滚难度。

> 但既然实例现成、成本为零，我倾向**一期就把会话挪过去**（改动小、收益直接），二期再补限流。

### 3.4 什么**绝对不能**进数据库

- **音频成品 / 分段缓存（`seg_cache`）**：G4 刚把单人合成改成"流式落盘、峰值内存=单段"。音频走文件系统是**性能设计**，不是技术债。塞进 GridFS 会前功尽弃。
- **模型权重、参考音频**：同上。
- **队列任务文件**：一任务一文件 + 节流写盘，已适配"进度频繁更新"的场景，比数据库行更合适。

---

## 4. 方案对比（A / B / C）

| | **A. 全部迁 Mongo** | **B. 分层迁移（推荐）** | **C. 只换 SQLite + 文件锁** |
|---|---|---|---|
| 做法 | 所有 JSON 全进 Mongo，含项目/术语表/队列 | 账本→Mongo；会话→Redis；其余留文件 | 启动时把 `store.py` 换成 SQLite；锁换 `fcntl` |
| 解决 P0-1（跨写原子） | ✅ | ✅ | ✅（单机事务） |
| 解决 P0-2（多进程） | ✅ | ✅ | ⚠️ 仅单机多进程；多容器共享 SQLite 文件不稳定 |
| 解决 P1-1（全量读） | ✅ | ✅ | ✅ |
| 改动面 | 大（10+ 模块、含队列热路径） | **中（membership 模块内闭环）** | 中 |
| 引入新组件 | Mongo | Mongo（+Redis） | 无（stdlib sqlite3） |
| 回滚难度 | 高（数据形态全变） | **低（`MEMBER_STORE=file` 一行切回）** | 中 |
| 与现有多机内网 | ✅ 天然支持多实例 | ✅ | ❌ 不适合多实例 |
| 风险 | 队列热路径改动可能引入新 bug | **低** | 单点文件 + 多实例不支持 |

**推荐 B。** 理由：
1. 改动**收敛在 `app/membership/` 内**，不碰刚做完 G4 的合成/队列热路径 —— 符合"改动收敛、可回滚"。
2. 已有多机内网 + 现成的 Mongo/Redis 实例，不引入新运维对象。
3. 用开关灰度（`MEMBER_STORE=file|mongo`），出问题一行回退。

**什么时候该选 C**：如果后端永远只跑**单机单实例**、且你不想多维护一个数据库 —— 那 SQLite 是更省的选择（同样的 ACID，零新增服务）。
但你的部署是容器化 + 内网多机，迟早要多实例，所以 B 更前瞻。

---

## 5. 实施路线（Gate 制，逐阶段确认）

### Gate 0 — 前置验证 ✅ **已完成**（2026-10-12 21:40）

#### 5.1 实测结果

| 项 | 实测值 | 结论 |
|---|---|---|
| Mongo 连通 | ✅ `admin.command("hello")` 成功 | 可用 |
| Mongo 版本 | **6.0.28**（`maxWireVersion=17`） | `$inc` / `findOneAndUpdate` / 唯一索引 / TTL 索引全部支持 |
| **Mongo 拓扑** | `setName = None`、`isWritablePrimary = True` | ⚠️ **单节点，非副本集 ⇒ 无多文档事务、无 oplog** |
| Mongo 账号权限 | `root@admin`，角色 `root@admin` | 超级权限，可建库/建索引 ✅ |
| `podcast` 库 | **不存在**（现有库：`admin`/`companion`/`config`/`local`） | 干净起步，与 `companion`（其他项目）天然隔离 ✅ |
| Redis 连通 | ✅ `PING → True` | 可用 |
| Redis 版本/模式 | **7.4.9**，`redis_mode = standalone` | ✅ **单体，db 编号可用**（非 Cluster） |
| Redis 持久化 | `appendonly = yes`（AOF 已开）、`maxmemory = 0`（不限） | 会话数据不会因重启全丢 |
| Redis db2 | `dbsize = 0`，实例 `keyspace = {}` | 干净，无冲突 ✅ |
| 网络延迟 | 11 ms（同网段 `192.168.31.0/24`） | 局域网速度，无需担心连接开销 |

**对方案的两个直接影响：**

1. **单节点 Mongo ⇒ 事务路线被物理排除。** 这**印证了 §3.2 的设计必要性** ——
   用「单文档原子操作 + 唯一索引」不是"更优选择"，而是**这里唯一可行的正确做法**。
   方案无需修改，但 §3.2 从"推荐"升级为"必须"。
2. **单节点无 oplog ⇒ 无时间点恢复（PITR）。** 备份只能靠 `mongodump` 定时全量。
   账本必须可恢复，**备份是上线前置项，不是可选项**（已列入 Gate 4 清单）。

#### 5.2 网络排查结论（**这不是目标主机的问题**）

**事实（可复现）：**

| 阶段 | `:30001` / `:30002` 结果 |
|---|---|
| 首次探测（沙箱内） | ❌ `ConnectionRefusedError`（RST） |
| 执行过一次「沙箱绕过」授权后 | ✅ **OPEN**，连续 3 轮复测稳定，`PyMongo` / `redis-py` 实连成功 |
| 对照：`:80` `:6379` `:27017` | ❌ 始终 RST（**这两个端口确实没服务**，符合预期） |

**定位到的机制：**

- 命令并非直接在宿主 shell 执行，而是经 unix socket 交给沙箱 broker：环境变量 `TOYBOX_SANDBOX_SOCK` /
  `CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS`，PATH 前置了 `shim/brokered-bin/`（`cat`/`ls`/`grep` 等都是 shim）。
- 对应的沙箱策略文件 `cli/vendor/toybox-macos/toybox.sb` 是 `(deny default)` + **仅**
  `(allow network-outbound (local unix))` —— 即**唯一被放行的出站通道是到 broker 的 unix socket**。
- 宿主系统代理是 Clash `127.0.0.1:7890`，其 PAC 例外列表**包含 `192.168.*`** ⇒ 你本机的 Studio 3T 对
  `192.168.31.238` 是**直连**，因此你那边一直正常。此外环境里另有沙箱注入的
  `HTTP_PROXY=127.0.0.1:65091`（**与连库无关**，但见下方注意事项）。

**结论（推断部分已标注）：**
- ✅ **确定**：不通**不是**目标主机/端口映射的问题 —— 服务一直是好的（这也与你 Studio 3T 能连通一致）。
- 🔎 **推断**：沙箱的网络放行是**按会话/按授权**生效的状态。首次遇到未授权目标时沙箱返回 RST；
  在完成一次沙箱绕过授权后，本会话放行。**未知**：全新会话是否重现 RST（未能验证）。

**工程注意事项（重要）：**

```bash
# 连 DB 的命令一律显式去代理 —— 沙箱注入的 HTTP_PROXY 会干扰基于 httpx 的代码
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
    NO_PROXY='localhost,127.0.0.1,192.168.31.238' <你的命令>
```
`pymongo` / `redis-py` 走原生 socket、**不读** `HTTP_PROXY`，所以 DB 连接本身不受影响；
但本项目**既有的启动约定**就是去代理（`start.sh` 的上游用法），
**`.env` 的 `NO_PROXY` 应补上 `192.168.31.238`**，避免将来用 httpx 访问同网段服务时踩坑。

#### 5.3 仍需你拍板（2 项）

**① key / db 隔离**（已定方案，仅告备）：
- Mongo 用独立 database `podcast` —— 与 `companion` 等现有库天然隔离 ✅
- Redis 用 **db02 + 统一 key 前缀 `podcast:`** —— 前缀**必须加**，否则 `session:*` 这类通用名会与同实例其他项目互踩

**② 本期范围**：只做"账本上库 + 会话入 Redis"，**还是**同时接积分商城的真实支付通道？
（**建议拆开** —— 存储改造与通道接入是两件独立的事，一次只动一处，便于定位问题与回滚。）

---

### Gate 1 — 抽出存储适配层（不改行为）

- 把 `app/membership/store.py` 的函数签名**冻结为接口**（现在已经是纯函数式薄封装，天然适合）。
- 新增 `app/db.py`：Mongo / Redis 连接管理（懒连接、超时、失败不阻塞启动）。
- `app/config.py` 加配置项；`.env.example` 同步。
- 此时**行为零变化**，`MEMBER_STORE=file`（默认）走原路径。
- 回归基线：`tests/test_membership.py` 全绿。

### Gate 2 — 账本切 Mongo（核心）

- 实现 `store_mongo.py`：`users` / `orders` / `point_logs` / `redeem_codes` / `checkins` 五个集合。
- **建索引**（这是资金安全的关键）：
  ```javascript
  db.users.createIndex({user_id: 1}, {unique: true})
  db.orders.createIndex({order_id: 1}, {unique: true})
  db.orders.createIndex({out_trade_no: 1})
  db.point_logs.createIndex({ref: 1, kind: 1}, {unique: true, sparse: true})  // 幂等防线
  db.point_logs.createIndex({user_id: 1, created_at: -1})
  db.checkins.createIndex({user_id: 1, date: 1}, {unique: true})               // 防重复签到
  db.redeem_codes.createIndex({code: 1}, {unique: true})
  ```
- 改造 `service.py`：删除 `_user_locks` / `_order_lock` / `_redeem_lock`（约 30 行），把 §3.2 表格里的六个"读改写"换成原子操作。
- `MEMBER_STORE=mongo` 切换，跑并发回归（现有 `tests/test_membership.py` 里的 Barrier 并发段直接复用）。

### Gate 3 — 会话切 Redis（可选，建议同批做）

- `sessions` 从 JSON 挪到 Redis：`podcast:session:{token}` → JSON，TTL = `MEMBER_TOKEN_TTL_DAYS`。
- `resolve_token()` 从"全量读两个文件"变成"两次 O(1) 查询"（Redis 取会话 + Mongo 取用户）。
- 顺带解决：`_purge_expired_sessions()` 可以删掉（Redis TTL 自动清理）。
- **降级策略**：Redis 不可用时**不**回落到文件（会产生双份状态），而是返回"登录态不可用"，让用户重新登录 —— 语义清晰。

### Gate 4 — 对账工具与安全收尾

- 新增 `tools/reconcile.py`：
  - 校验 `users.points == sum(point_logs.delta)`（**有账本之后才做得成的事**）
  - 找出 `status=paid` 但无对应 `purchase` 流水的订单（应为 0）
  - 找出 `status=pending` 超过 N 小时的订单（人工核对）
- **上线前检查清单**：
  - [ ] `MEMBER_MOCK_PAY=0`（并把代码默认值也改成 `0`）
  - [ ] `PAY_NOTIFY_SECRET` 已配置（否则回调端点 503 —— 这是设计如此，不是 bug）
  - [ ] `MEMBER_ENFORCE` / `MEMBER_REQUIRE_LOGIN` 取值确认
  - [ ] Mongo 连接串**只放 `.env`，不入 git**
  - [ ] `mongodump` 定时备份（账本必须可恢复）
  - [ ] 后端连接 Mongo 的**超时**已设（默认 30s 会把接口拖死）

---

## 6. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 连接不上 DB → 全站不可用 | 连接池超时设小（`serverSelectionTimeoutMS=3000`）；启动时探活失败**不阻塞启动**，接口层返回明确 503 |
| 一期改动引入资损 | 开关灰度（`MEMBER_STORE`）；先跑并发回归；上线初期保留文件写入做影子对比（可选） |
| **Mongo 单点故障（已实测确认为单节点）** | 无副本集 ⇒ **无自动故障转移、无 oplog、无 PITR**。上线前**必须**：①配 `mongodump` 定时全量备份 + 至少一次恢复演练；②连接池超时设小（`serverSelectionTimeoutMS=3000`），DB 挂了接口快速失败而不是挂死；③接受"DB 挂 = 注册/扣费/入账全停"这一事实（合成任务本身依赖本地文件，可继续） |
| Redis 丢数据 | 会话丢失=用户重登，**可接受**；因此**绝不把余额放 Redis** |
| 密码泄漏 | 连接串只进 `.env`；`.env` 已在 `.gitignore`（需确认） |

**回滚**：`MEMBER_STORE=file` 一行切回（Gate 1/2 期间）。切 Mongo 后产生的**新订单**不会自动回流到文件 —— 但由于"生产无用户、无订单"，回滚窗口内没有真实资金，风险可控。

---

## 7. 改动清单与工作量

| 文件 | 改动 | 规模 |
|---|---|---|
| `app/db.py` | **新增**：Mongo/Redis 连接管理 | ~80 行 |
| `app/membership/store_mongo.py` | **新增**：五个集合的读写实现 | ~180 行 |
| `app/membership/store.py` | 抽出接口 / 转 dispatch | ~40 行调整 |
| `app/membership/service.py` | **删锁**，六个"读改写"→ 原子操作 | ~120 行调整 |
| `app/config.py` + `.env.example` | 加 `MONGO_URL` / `REDIS_URL` / `MEMBER_STORE` | ~20 行 |
| `requirements.txt` | `+ pymongo`（`+ redis`） | 2 行 |
| `tools/reconcile.py` | **新增**：对账 | ~100 行 |
| `tools/init_indexes.py` | **新增**：建索引（幂等，可重复跑） | ~50 行 |

**不触碰**：`queue_worker.py`、`mono_runner.py`、`podcast_runner.py`、`seg_cache.py`、`routes/queue.py` 的合成路径、前端。
（`routes/queue.py` 只有扣费**调用点**不变 —— `charge_for_task` 签名保持一致。）

---

## 8. 待你确认（2 项）

| # | 项 | 选项 | 我的建议 |
|---|---|---|---|
| 1 | **本期范围** | (a) 只做「账本上库 + 会话入 Redis」<br>(b) 同时接真实支付通道 | **(a)** —— 两件事解耦，一次只动一处 |
| 2 | **方案路径** | (a) **B 分层迁移**（改动收敛在 `membership/`）<br>(b) **C SQLite + 文件锁**（零新增服务） | **(a) B** —— 已有实例现成、部署是多机容器；<br>仅当"永远单机单实例"才选 C |

**已确认、无需再议**（Gate 0 实测）：网络连通 ✅｜Mongo **6.0.28 单节点** ✅｜Redis **7.4.9 standalone / db2 空** ✅｜
`root@admin` 具备建库建索引权限 ✅｜`podcast` 库全新、与 `companion` 等隔离 ✅。

---

## 附录 A：已验证的连通性检查脚本

> 2026-10-12 实测通过，可直接复用。**注意前置的去代理**（见附录 B）。

> ⚠️ **凭据不入库**：本文件会被 git 跟踪，因此**不写明文口令**。真实值放 `.env`
>（已被 `.gitignore` 忽略：`.gitignore:30`）。

```bash
export MONGO_URL='mongodb://root:<password>@192.168.31.238:30001/?authSource=admin'
export REDIS_PASSWORD='<password>'

env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \
/Users/zhangjianwen/.workbuddy/binaries/python/envs/default/bin/python - <<'PY'
import os
from pymongo import MongoClient
import redis

c = MongoClient(os.environ["MONGO_URL"],
                serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
h = c.admin.command("hello")
print("Mongo:", c.server_info()["version"],
      "| setName =", h.get("setName"), "| 库 =", c.list_database_names())

r = redis.Redis(host="192.168.31.238", port=30002,
                password=os.environ["REDIS_PASSWORD"], db=2,
                socket_timeout=5, socket_connect_timeout=5)
si = r.info("server")
print("Redis:", si["redis_version"], "| mode =", si["redis_mode"], "| db2 keys =", r.dbsize())
PY
```

## 附录 B：环境陷阱（本次排查产出）

| 陷阱 | 说明与应对 |
|---|---|
| **沙箱网络按会话放行** | 若某次会话连 DB 首次报 `ConnectionRefused`，**先怀疑沙箱、不要去查目标主机**。本次实测：首次 RST，沙箱绕过授权后连续 3 轮 OPEN 且实连成功。 |
| **`HTTP_PROXY` 污染** | 环境中有沙箱注入的 `HTTP_PROXY=http://127.0.0.1:65091`。`pymongo`/`redis-py` 不读它，但 **httpx 会读**。连内网服务的命令一律 `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY=...`。 |
| **Mongo 单节点** | 无多文档事务、无 oplog ⇒ **不支持 PITR**，备份只能 `mongodump` 且**必须做**。 |
| **Redis 共用实例** | 同实例还有别的项目 ⇒ 所有 key **必须**带 `podcast:` 前缀。 |
| **`podcast` 库尚未创建** | 首次写入时由 Mongo 自动建库；索引需显式创建（Gate 2 的 `tools/init_indexes.py`，幂等可重复跑）。 |
