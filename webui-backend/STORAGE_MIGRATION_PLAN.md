# 存储改造计划：账本 → MongoDB，会话 → Redis，内容 → MongoDB

> 起因：准备接入真实支付；后续又确认**要上多实例**。现状是纯 JSON 文件 + 进程内 `threading.Lock`。
> 本文回答两个问题：**要不要上数据库**、**怎么上**。
> 撰写依据：`app/membership/{store,service,routes,pay}.py`、`app/{stores,stores_file,stores_mongo,queue_state,voice_store}.py`、
> `app/main.py`、`app/config.py` 的实际代码，以及 `data/` 的真实数据形态（2026-10-12 / 10-13 实测）。

> **实施进度**：Gate 0 ✅ 前置验证 ｜ Gate 1 ✅ 抽存储适配层 `650dd64` ｜
> Gate 2 ✅ 账本切 Mongo `f8abb0d` ｜ **Gate 3 ✅ 会话入 Redis `4a07304`** ｜
> **Gate 4 ◐ 对账工具 `tools/reconcile.py` `3e5ef31` 已落地**，上线清单待部署时执行 ｜
> **Gate 5 ✅ 内容（项目/术语表/收藏）可切 Mongo**（二期 A/B，见 §9）。
> 下文 §5 各 Gate 的清单保留**原始设计**；实际落地与它的差异，见各 Gate 标题下的「实际落地」注。
> **代码已全部落地，尚欠的只有「部署时按清单执行」那一档**（Gate 4 的上线清单）。
> **三个开关互相独立**，可任意组合：`MEMBER_STORE=file|mongo` × `SESSION_STORE=file|redis` × `CONTENT_STORE=file|mongo`。

---

## 0. 结论先行

**是，但不是"全迁"，而是按一致性要求分层。**

| 数据 | 去向 | 理由 |
|---|---|---|
| **订单 / 积分流水 / 用户余额 / 优惠码 / 签到** | **MongoDB（权威账本）** | 真钱不可逆，必须原子、幂等、可对账、抗多实例 |
| **会话 token** | **Redis（带 TTL）** ✅ Gate 3 | 每请求全量读 JSON 是明确的扩展性瓶颈；TTL 自动过期 |
| **项目 / 术语表 / 收藏** | **MongoDB** ✅ Gate 5 | 二期 A/B：**多实例**下文件版「A 实例存的 B 实例读不到」；见 §9 |
| 音色元数据 / 音色预设 | **暂留文件**（未纳批） | 与音频实体、tts-server 广播强绑定，单独一批做（见 §9.2） |
| 音频成品 / 分段缓存 / 模型权重 / 参考音频 | **永远留磁盘** | 大文件资产，塞进库是反模式（详见 §3.4） |

**关键澄清：触发上库的不是"数据太多"。**
当前真实数据量——**2 个用户、21 条会话、77 条积分流水、5 个优惠码、0 笔订单**（`orders.json` 至今不存在）。
这个体量下 JSON 文件**完全够用**，且单进程部署下现有代码**能正常工作**。

**真正的触发点是"要接真实支付"**：钱一旦收进来，故障代价从"页面报错"变成"用户的账对不上"，且不可逆。
这要求三件 JSON 文件给不了的事：**跨读写的原子性、跨进程的互斥、可事后对账的账本**。

> ✅ **前置条件已全部验证通过**（2026-10-12 21:40 实测）：Mongo / Redis 均连通，权限、拓扑、
> 命名空间全部确认 —— 详见 §5.1 实测结果。
> ✅ **两项已拍板**（本期范围、方案路径，见 §8）：**B 分层迁移 + 范围 a**，Gate 1 / Gate 2 已落地。
> ✅ **二期已拍板**（2026-10-13，见 §9）：**A（项目）+ B（术语表 / 收藏）先做；C（队列）单独做**。
> ⚠️ 二期的**动因与一期不同** —— 不再是"接支付"，而是**多实例部署**（见 §9.1）。

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

#### 5.3 已拍板（2026-10-12；原文保留备查）

**① key / db 隔离**：Mongo 用独立库 `podcast` —— 与 `companion` 等现有库天然隔离 ✅；
Redis 用 **db02 + 强制 `podcast:` 前缀**（前缀必须加，否则 `session:*` 这类通用名会与同实例其他项目互踩）✅

**② 本期范围**：**B 分层迁移 + 范围 a** —— 只做"账本上库 + 会话入 Redis"，**不接真实支付通道**
（存储改造与通道接入是两件独立的事，一次只动一处，便于定位问题与回滚）✅

---

### Gate 1 — 抽出存储适配层（不改行为）✅ **已完成**（提交 `650dd64`）

- 把 `app/membership/store.py` 的函数签名**冻结为接口**（现在已经是纯函数式薄封装，天然适合）。
- 新增 `app/db.py`：Mongo / Redis 连接管理（懒连接、超时、失败不阻塞启动）。
- `app/config.py` 加配置项；`.env.example` 同步。
- 此时**行为零变化**，`MEMBER_STORE=file`（默认）走原路径。
- 回归基线：`tests/test_membership.py` 全绿。

### Gate 2 — 账本切 Mongo（核心）✅ **已完成**（提交 `f8abb0d`）

> **⚠️ 实际落地与下方原始设计的差异**（以代码为准，避免口径漂移）：
> - 幂等防线索引实际为 `point_logs.{user_id, ref, kind}` **partial unique** —— 用
>   `partialFilterExpression` 的 `$or` 列举 `kind ∈ {refund, purchase}`，**不是** `sparse`；
>   比原设计多一个 `user_id` 维度（同一 ref 在不同用户下互不干扰）。
> - ⚠️ **`spend` 刻意不纳入幂等索引**：`routes/queue.py:retry_queue_task` 重试时**复用同一
>   `task_id`** 并重新预扣积分，把 `spend` 纳入会让重试的扣费被自己挡住。改稿退差的 ref
>   亦**刻意留空**（非幂等操作，每次改稿都该退）。
> - `users.user_id` / `orders.order_id` / `redeem_codes.code` **不再单独建唯一索引**：
>   它们已经是 `_id`（`users._id=user_id` / `orders._id=order_id` / `redeem_codes._id=code` /
>   `point_logs._id=id` / `checkins._id="{uid}:{date}"`），**主键天然唯一**，额外索引是冗余。
> - 索引总数 = **6 个**（`users.uniq_username`、`users.uniq_email` partial、
>   `point_logs.uniq_idempotent` partial、`point_logs.user_recent`、`point_logs.recent`、
>   `orders.by_out_trade_no`），全部由 `tools/init_indexes.py` **幂等**创建，`--check` 供上线自检。
> - 验证：`TEST_MEMBER_STORE=file|mongo` 双后端下 `test_store_backend.py` 31 项、
>   `test_membership.py` 56 项全绿；并发签到 / 并发兑码**两后端都只成功 1 次**；全量离线回归 41/41。
>   ⚠️ **两模式跑出「绿」意义不同**：`checkins`/`redeem_codes` 的唯一索引在 file 后端**不存在**
>   （JSON 无约束力）⇒「唯一索引消灭锁」**只在 mongo 模式被真正验证**。

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

### Gate 3 — 会话切 Redis ✅ 已完成（2026-10-12）

> **实际落地与原始设计的差异**（4 处）
> 1. 会话**不是**在账本存储里换实现，而是**整体拆到独立门面** `session_store.py`
>    （`SESSION_STORE=file|redis`，与 `MEMBER_STORE` **解耦**）。理由是「会话进 Redis」与
>    「账本进 Mongo」没有依赖关系，分开配置才能**单独切换、单独回滚**。
>    代价：账本门面摘掉了 `load_sessions`/`save_sessions`，`store_mongo` 的 `sessions`
>    集合作废（历史库上若残留可直接 drop）。
> 2. 接口做成**细粒度 KV**（`put_session` / `get_session` / `delete_session` /
>    `delete_user_sessions` / `count_sessions`），不是"读整份 → 改 → 盖回" ——
>    Redis 后端只有单键 O(1) 才谈得上收益，整表读写会把收益吃光。
>    因此 `_purge_expired_sessions()` 直接删掉了（redis 靠 TTL、file 自己惰性清）。
> 3. 「踢某用户全部下线」从「读全表 → 过滤 → 盖回」变成一次 `delete_user_sessions(user_id)`。
>    Redis 没有二级索引，为此维护 `podcast:user_sessions:{user_id}` 反向索引 ——
>    `KEYS podcast:session:*` 会阻塞整个实例，**绝不能用**。
> 4. **降级策略按路径分三种**（原设计只说"让用户重新登录"，太粗）：
>    | 路径 | Redis 挂掉时 | 为什么 |
>    |---|---|---|
>    | 读（`resolve_token`） | 按**未登录**处理（返回 None）+ warning | 合成提交走可选鉴权，会话存储故障不该把公开链路一起打挂 |
>    | 建（注册 / 登录） | `MemberError(503)` | 用户点了登录，必须拿到明确反馈，不能是 500，也不能含混成「密码错误」 |
>    | 删（登出 / 踢下线） | 记 warning 后**忽略** | 故障期间那个 token 本来就通不过鉴权；报错只会让前端卡在「登出中」，会话最终由 TTL 过期 |
>
> 验收：`test_session_store.py` file 24 / redis 25 全绿；`test_membership.py`
> 账本×会话 **2×2 组合**（file|mongo × file|redis）各 56/56 全绿。

- `sessions` 从 JSON 挪到 Redis：`podcast:session:{token}` → JSON，TTL = `MEMBER_TOKEN_TTL_DAYS`。
- `resolve_token()` 从"全量读两个文件"变成"两次 O(1) 查询"（Redis 取会话 + Mongo 取用户）。
- 顺带解决：`_purge_expired_sessions()` 可以删掉（Redis TTL 自动清理）。
- **降级策略**：Redis 不可用时**不**回落到文件（会产生双份状态），而是返回"登录态不可用"，让用户重新登录 —— 语义清晰。

### Gate 4 — 对账工具与安全收尾

**① 对账工具 `tools/reconcile.py` ✅ 已完成**（2026-10-12）

> 实现比原设计多出两条判据（原设计只有 `Σ delta == points`）：
> - **权威判据换成「最新一条流水的 `balance_after` == `points`」** —— 因为 `Σ delta` 在
>   **流水被裁剪**之后必然不等（`MAX_POINT_LOGS_PER_USER=500`，超限丢最旧的），不能当唯一判据。
>   `Σ delta` 降级为**仅在未裁剪时**适用的严格佐证。
> - **新增 `balance_after` 链连续性检查** ⇒ 直接定位**漂移开始的那一条**（比"合计不符"更有用）。
>
> 其余：订单×流水互证（`paid` 无流水 / 有流水非 `paid` / 挂单超时）、重复的幂等流水、
> 孤儿流水（`user_id` 已不存在）；CLI 支持 `--user` / `--json` / `--pending-hours` / `--verbose`，
> 退出码 0 / 1 / 2。测试 `tests/test_reconcile.py`（37 项，含 CLI 真进程冒烟）。
> ⚠️ 它同时是 Gate 2 「`$inc` 成功、写流水前崩溃」那个**残余窗口的唯一兜底**。

**② 上线前检查清单**（部署时执行）
- [ ] `tools/init_indexes.py --check` 对**生产库**跑一遍（唯一索引是幂等防线）
- [ ] `MEMBER_MOCK_PAY=0` —— ⚠️ **范围 a 下暂时不要置 0**：本期不接真实通道，置 0 会让积分商城
      直接不可用。**接真实支付时**再置（届时也要把代码默认值从 `"1"` 改成 `"0"`）
- [ ] `PAY_NOTIFY_SECRET` 已配置（否则回调端点 503 —— 设计如此，不是 bug）
- [ ] `MEMBER_ENFORCE` / `MEMBER_REQUIRE_LOGIN` 取值确认
- [ ] Mongo 连接串**只放 `.env`，不入 git**
- [ ] `mongodump` 定时备份（账本必须可恢复）
- [ ] 后端连 Mongo 的**超时**已设（`DB_TIMEOUT_MS=3000` ✅ Gate 1 已做）

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

| 文件 | 改动 | 估算 | 实际 / 状态 |
|---|---|---|---|
| `app/db.py` | **新增**：Mongo/Redis 连接管理 | ~80 行 | 175 行 ✅ Gate 1 |
| `app/membership/store_contract.py` | **新增**：跨后端共享常量与异常 | （原计划未列） | 87 行 ✅ Gate 2 |
| `app/membership/store_mongo.py` | **新增**：七集合读写实现（原计划写"五个"） | ~180 行 | 456 行 ✅ Gate 2；Gate 3 摘掉 `sessions`（→ 六集合） |
| `app/membership/store.py` | 抽出接口 / 转 dispatch 门面 | ~40 行 | 30 接口 ✅ Gate 1/2；Gate 3 摘掉会话两接口（→ 28） |
| `app/membership/store_file.py` | 原 JSON 实现搬移 + 补原子接口 | （原计划未列） | +241 行 ✅ Gate 1/2；Gate 3 摘掉会话并把文件原语抽到 `json_store.py` |
| `app/membership/service.py` | **删锁**，六个"读改写"→ 原子操作 | ~120 行 | 434 行变更 ✅ Gate 2；Gate 3 会话四函数改走 `session_store` |
| `app/config.py` + `.env.example` | 加 `MONGO_URL` / `REDIS_URL` / `MEMBER_STORE` | ~20 行 | ✅ Gate 1；Gate 3 加 `SESSION_STORE` |
| `app/membership/json_store.py` | **新增**：JSON 文件原语（两个 json 后端共用） | （原计划未列） | 49 行 ✅ Gate 3 |
| `app/membership/session_store.py` | **新增**：会话门面（`SESSION_STORE=file\|redis`） | （原计划未列） | 100 行 ✅ Gate 3 |
| `app/membership/session_store_file.py` | **新增**：会话 JSON 实现（复用 `sessions.json`，格式不变） | （原计划未列） | 107 行 ✅ Gate 3 |
| `app/membership/session_store_redis.py` | **新增**：会话 Redis 实现（TTL + 反向索引） | （原计划未列） | 118 行 ✅ Gate 3 |
| `requirements.txt` | `+ pymongo`（`+ redis`） | 2 行 | ✅ Gate 1 |
| `tools/init_indexes.py` | **新增**：建索引（幂等，可重复跑） | ~50 行 | 150 行 ✅ Gate 2 |
| `tools/reconcile.py` | **新增**：对账（判据见 Gate 4） | ~100 行 | 400 行 ✅ Gate 4 |

**不触碰**：`queue_worker.py`、`mono_runner.py`、`podcast_runner.py`、`seg_cache.py`、`routes/queue.py` 的合成路径、前端。
（`routes/queue.py` 只有扣费**调用点**不变 —— `charge_for_task` 签名保持一致。）

---

## 8. 已确认（2026-10-12 拍板；原文保留备查）

| # | 项 | 选项 | 决定 |
|---|---|---|---|
| 1 | **本期范围** | (a) 只做「账本上库 + 会话入 Redis」<br>(b) 同时接真实支付通道 | ✅ **(a)** —— 两件事解耦，一次只动一处 |
| 2 | **方案路径** | (a) **B 分层迁移**（改动收敛在 `membership/`）<br>(b) **C SQLite + 文件锁**（零新增服务） | ✅ **(a) B** —— 已有实例现成、部署是多机容器 |

**已确认、无需再议**（Gate 0 实测）：网络连通 ✅｜Mongo **6.0.28 单节点** ✅｜Redis **7.4.9 standalone / db2 空** ✅｜
`root@admin` 具备建库建索引权限 ✅｜`podcast` 库全新、与 `companion` 等隔离 ✅。

---

## 9. 二期：内容上库（2026-10-13，Gate 5）

### 9.1 为什么做（动因与一期**不同**）

一期（账本 / 会话）的动因是**接真实支付**；二期的动因是**多实例部署** ——
用户明确：「用上数据库就是考虑到后面需要多实例」。

多实例下，仍落在**本地磁盘 + 进程内锁**的数据会出现这种故障：**用户在 A 实例存的
项目，切到 B 实例就读不到**（每个实例各有一份自己的 `data/`）。一期把账本 / 会话
搬走之后，这一点反而更显眼：**钱一致了，别的还不一致**。

⚠️ 诚实说明：单实例部署下这些文件**完全够用**，现有代码也能正常工作 —— 上库是
**为多实例买保险**，不是修 bug。

### 9.2 范围（已拍板）

| 批次 | 内容 | 状态 |
|---|---|---|
| **A** | 项目（`data/projects/*.json`） | ✅ 本批 |
| **B** | 术语表（全局 + 每用户）、收藏音色 | ✅ 本批 |
| **C** | 任务队列（`data/queue/*.json`） | ⏳ **单独做**（连带 worker 调度模型，改动面大） |
| — | 音色元数据（`data/voices/<uid>/index.json`）、音色预设 | ⏳ 未纳批 —— 与音频实体、tts-server 广播强绑定，单独评估 |
| — | 邮箱验证码（`data/members/email_codes.json`） | ⏳ 未纳批 —— 短 TTL 临时数据，**Redis 比 Mongo 更合适**；且它现在是「mongo 走集合 / file 走文件」的双口径 |
| — | 音频成品 / 分段缓存 / 模型 / 参考音频 | ❌ **永不进库**（§3.4） |

### 9.3 落地形态

新增第四个开关 `CONTENT_STORE=file|mongo`（默认 `file`），与 `MEMBER_STORE` /
`SESSION_STORE` **互相独立、可任意组合**。

模块与一期同构：
```
app/stores.py        门面（dispatch + 术语表纯逻辑：合并 / 中点变体展开 / 按序替换）
app/stores_file.py   文件后端（原 stores.py 的 IO 部分 + 收藏）
app/stores_mongo.py  MongoDB 后端
app/json_store.py    JSON 读 / 原子写原语（从 membership/ 提到顶层，三个 json 后端共用）
```

集合设计（`_id` 即业务主键 ⇒ 天然「一人一份」且 upsert 幂等，**无需唯一索引**）：

| 集合 | `_id` | 文档 |
|---|---|---|
| `projects` | `project_id`（`proj_*`） | 项目全文（含 `owner_id`） |
| `glossary` | `"GLOBAL"` 或 user_id | `{terms: [...]}` |
| `favorites` | `"GLOBAL"` 或 user_id | `{paths: [...]}` |

### 9.4 与原始设计的差异（避免口径漂移）

1. `favorites` **从 `routes/voices.py` 收编进存储层** —— 原先散在路由里、直接读写文件，
   上库时必须先收拢（否则路由会绕过门面）。`stores.py` 从此不只管项目 + 术语表。
2. 项目列表的排序依据：文件版原本按**磁盘 mtime**，现统一为**业务字段 `updated_at`**
   —— mongo 没有 mtime，两个后端必须用同一判据才谈得上「行为等价」。
3. mongo 的项目列表用 aggregation 的 `$size` **服务端算行数**，不把 `lines` 正文拉回来。
4. `app/json_store.py` 从 `app/membership/` **提到 `app/` 顶层** —— 它被内容层复用，
   留在 membership 包里会顺带把整个会员栈初始化一遍。

### 9.5 切换步骤（**必须**带迁移）

⚠️ `data/glossary.json`（全局术语库，超管维护、**对所有用户生效**）当前就有内容，
**切换前不迁移 = 用户看到「词条全没了」**。

```bash
cd webui-backend
# 1) 先 dry-run 看读到了什么
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \
    python tools/migrate_content.py --dry-run
# 2) 真灌（幂等，可重复跑；只加不减）
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \
    python tools/migrate_content.py
# 3) 建索引（CONTENT_STORE=mongo 时才会把 projects 索引纳入）
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy NO_PROXY='*' \
    python tools/init_indexes.py
# 4) 改 .env：CONTENT_STORE=mongo，重启后端
# 5) 用 /api/version 的 content_store_backend 核对是否已是 mongo
```

### 9.6 回滚

改回 `CONTENT_STORE=file` 并重启即可 —— **文件没被删**，`stores_file.py` 就是原来的
实现，逐字节一致。⚠️ 但要注意：切回后**看不到**切到 mongo 之后新写的内容
（那是两份独立的数据）。真要双向，得反向迁一次。

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
