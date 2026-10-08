# 会员系统（G2）使用说明

> 2026-09-19 落地。存储为纯 JSON 文件（`data/members/`），零新增依赖，可整体回滚。

## 一、功能总览

| 功能 | 端点 | 说明 |
|---|---|---|
| 注册 | `POST /api/auth/email-code` + `POST /api/auth/register-email` | **唯一注册方式（邮箱验证码）**，见「邮箱注册」节；需配置 SMTP_*；注册赠送积分 |
| 登录 | `POST /api/auth/login` | `{username 或 email, password}` → `{token, user}`；账号禁用返回 403 |
| 登出 | `POST /api/auth/logout` | 吊销当前 token |
| 当前用户 | `GET /api/auth/me` | `Authorization: Bearer <token>` |
| 编辑资料 | `PATCH /api/users/me` | `{nickname?, bio?}` |
| 修改密码 | `POST /api/users/me/password` | `{old_password, new_password}`；改完全端登出 |
| 积分余额 | `GET /api/points/balance` | |
| 积分流水 | `GET /api/points/logs?offset&limit` | 每笔变动均有记录（earn/spend/redeem/checkin/grant/refund） |
| 优惠码兑换 | `POST /api/points/redeem` | `{code}`；一码多用/每人一次/过期/停用均有校验 |
| 每日签到 | `GET/POST /api/points/checkin` | 每日一次，送固定积分 |
| 积分商城 | `GET /api/points/packs` | 套餐清单（含按当前单价折算的可合成字数）；**无需登录** |
| 下单 | `POST /api/points/orders` | `{pack_id}` → pending 订单；**此时不发积分**，见「积分购买」节 |
| 我的订单 | `GET /api/points/orders?offset&limit` | 自己的订单（别人的订单返回 404，不用 403 以免探测） |
| 模拟支付 | `POST /api/points/orders/{order_id}/mock-pay` | 演示用；与真实回调**同一条入账路径**，同样幂等。开关 `MEMBER_MOCK_PAY` |
| 支付回调 | `POST /api/pay/notify/{provider}` | **无 Bearer**，靠 HMAC 验签；`PAY_NOTIFY_SECRET` 未配则 503 |

鉴权头：`Authorization: Bearer <token>` 或 `X-Auth-Token: <token>`；token 有效期默认 30 天。

## 二、配置（环境变量或 .env，全部可缺省）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMBER_REG_BONUS` | **500** | 注册赠送积分（0=关闭）。2026-10-05 由 100 调为 500（= ¥5 ≈ 1 万字）。**Compose 环境变量优先于 backend `.env`** |
| `MEMBER_CHECKIN_BONUS` | 20 | 每日签到积分（0=关闭）；Compose 环境变量优先于 backend `.env` |
| `MEMBER_POINTS_PER_1000_CHARS` | 10 | 合成扣费**单价**：每千字扣分（0=关闭按量扣费）。线上 `.env` 实际配 50 ⇒ 50 积分/千字 = ¥5/万字 |
| `MEMBER_MIN_CHARGE` | 5 | 单次合成最低收费（积分）；0=不设地板。见「计费口径」 |
| `MEMBER_TOKEN_TTL_DAYS` | 30 | 会话有效期 |
| `MEMBER_ENFORCE` | 0 | **1=收费模式**：登录才可提交合成任务、预扣积分、失败自动退款；0=不扣费 |
| `MEMBER_MOCK_PAY` | **1** | 1=开放模拟支付端点。⚠️ **接入真实支付后必须置 0**，否则任何登录用户都能给自己的订单免费入账 |
| `PAY_NOTIFY_SECRET` | 空 | 真实支付回调验签密钥（HMAC-SHA256）；空=回调端点一律 503 |
| `MEMBER_ADMIN_TOKEN` | 空 | 管理接口令牌；未设置则管理接口整体禁用 |

**关键设计：`MEMBER_ENFORCE=0`（默认）时，合成链路零行为变化**——老用户无感；带 token 提交也不会扣钱。开启收费只需在 .env 加一行 `MEMBER_ENFORCE=1`。

## 二·五、邮箱注册（2026-09-19 新增；2026-09-20 起为唯一注册方式）

> 原 `POST /api/auth/register`（用户名+密码直注册）已移除；`service.register()` 保留仅供管理 CLI / 测试使用。

流程：用户填邮箱 → `POST /api/auth/email-code` 收 6 位验证码 → `POST /api/auth/register-email` `{email, code, password, nickname?}` 建号并自动登录。

- 用户名从邮箱前缀自动派生（非法字符清洗、冲突加随机后缀），登录时邮箱和派生用户名均可作为账号。
- 防刷：验证码 10 分钟有效；同邮箱 60 秒 1 条、每日 10 条；验证错 5 次作废；发送失败自动作废不误报成功；注册赠送积分在验证通过后才发。

**SMTP 配置（QQ 邮箱为例）**：QQ 邮箱网页版 → 设置 → 账号 → 开启 SMTP 服务 → 生成授权码，然后 .env 加：

```ini
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=你的QQ号@qq.com
SMTP_PASS=刚生成的授权码
SMTP_FROM_NAME=播客工坊
```

未配置时 `/api/auth/email-code` 返回 503，用户名注册不受影响。163 邮箱同理（smtp.163.com）。`SMTP_TLS` 可选 `ssl`（465 默认）/`starttls`（587 默认）/`none`（本地调试明文）。用户量起来后可平滑切阿里云邮件推送 DirectMail（只需替换 `app/membership/mailer.py` 的 `send_email`）。

## 三、管理接口（请求头 `X-Admin-Token: <MEMBER_ADMIN_TOKEN>`）

- `POST /api/admin/members/codes` `{count, points, max_uses?, expires_days?, note?}` → 批量生成优惠码
- `GET  /api/admin/members/codes` → 优惠码列表（含使用情况）
- `POST /api/admin/members/points/grant` `{user, delta, reason}` → 发放/扣减积分（delta 可负）
- `GET  /api/admin/members/users?query=` → 用户列表
- `POST /api/admin/members/disable` `{user, disabled}` → 禁用/启用（禁用即踢下线）

## 四、管理 CLI（无需后端运行，直接改 JSON）

```bash
cd webui-backend
python tools/member_admin.py add-codes --points 100 --count 10 --note "内测活动"
python tools/member_admin.py list-codes
python tools/member_admin.py grant --user 张三 --delta 500 --reason "客服补偿"
python tools/member_admin.py users
python tools/member_admin.py disable --user 某人
```

## 五、数据文件（`data/members/`）

- `users.json` 用户表（pbkdf2 口令哈希 + 独立盐，不存明文）
- `sessions.json` 会话表（token → 用户，带过期时间）
- `point_logs.json` 积分流水（新记录在前；每用户保留最近 500 条）
- `redeem_codes.json` 优惠码
- `checkins.json` 签到记录
- `orders.json` 积分购买订单（`{order_id: {...}}`；含套餐快照与入账流水 id，便于对账）

### 并发安全（2026-09-29 修）

`store` 的锁只保证**单次**读/写原子，保护不了跨读写的业务判断。签到与优惠码兑换
都是「读文件 → 判断 → 写回 → 发积分」，并发请求会各读到旧状态再各自写回，导致**重复发放积分**
（实测：8 线程并发签到，修前 **5 次成功、多发 80 分**；修后恒为 1 次）。

- `service.user_lock(user_id)`：按用户互斥（可重入），`checkin()` 整段持有，
  `_apply_delta()` 也持有 ⇒ 同一用户的积分变动不会互相覆盖。
- 优惠码是**跨用户共享**资源，另加进程级 `_redeem_lock` 串行化「读码→占坑→写回」，
  防单次码被多人同时兑走。
- ⚠️ 以上均为**单进程**保证。将来若把 backend 起成多 worker（多进程），
  需换成文件锁（`fcntl`）并在写入前复查当日签到记录 —— 现在不做是因为当前部署就是单进程。
- 回归：`tests/test_membership.py` 的「并发原子性」段（Barrier 对齐 8 线程签到 + 4 线程抢单次码）。

## 六、扣费钩子（MEMBER_ENFORCE=1 时生效）

- **提交** `/api/queue/submit`（双人播客与单人配音共用入口）：预扣积分，余额不足返回 402。
- **编辑** 未执行任务改稿 → 按新字数多退少补。
- **失败/取消/中断** → 自动全额退款（`queue_worker` 收尾处统一处理，幂等）。
- **重试** → 重新预扣。
- 流水中 `spend`（扣费）/`refund`（退款）与 task_id 关联，可对账。

### 计费口径（2026-09-28 改，见 `membership/service.py: estimate_task_cost`）

```
cost = max(MEMBER_MIN_CHARGE, ceil(字数 × MEMBER_POINTS_PER_1000_CHARS / 1000))
字数 = 各行 text 剥离行内 [pause:N] 后的字符数之和
```

**为什么改**：旧式是 `ceil(字数/1000) × 单价` —— 整千向上取整，不足 1000 字也
按一整千收费（500 字与 1000 字同价），且 1000→1001 字直接翻倍；同时把「签到
送 5 积分」这类小额赠送变成了摆设（签到 10 天才够一次）。改为线性后
**1000 字及以上的价格与旧口径完全一致**（收入中性），只有短任务变便宜。

以 `.env` 实际配置（单价 50、地板 5）为例：

| 正文字数 | 旧口径 | 新口径 |
|---|---|---|
| 50 | 50 | 5 |
| 300 | 50 | 15 |
| 1000 | 50 | 50 |
| 1001 | 100 | 51 |
| 5000 | 250 | 250 |

**为什么保留最低收费**：成本主要由**合成段数**（引擎调用次数）决定，而不随字数
线性 —— 一段 20 字要 1 次调用，20 字拆成 4 段要 4 次，第三方引擎每次还有上传/
导入固定开销。地板用来兜住短而碎的任务；设 0 即取消地板。

**改动需同步三处**（否则前端预估与实际扣费不一致）：后端本文件所述函数、前端
`types/index.ts` 的 `estimatePoints()` / `billableChars()`、以及 `MonoEditor` 内
显示的字数（同口径）。回归基线：`tests/test_task_cost.py`。

## 六·五、积分购买（2026-10-05；**真实支付通道尚未接入**）

链路（与「谁收钱」无关）：

```
选套餐 → create_order()  落一条 pending 订单（不碰积分）
        ↓
支付成功（真实回调 / 模拟）→ settle_order()  幂等入账
```

- 定价锚点：**1 积分 = ¥0.01**；合成单价 50 积分/千字 ⇒ **¥5/万字** ⇒ 1000 积分 ≈ 2 万字。
- 套餐规格见 `app/membership/packs.py`（方案 B 阶梯赠送，**改价只改这一张表**）：

  | 套餐 | 价格 | 到账积分 | 赠送 | 约可合成 |
  |---|---|---|---|---|
  | 体验包 | ¥10 | 1000 | — | 2 万字 |
  | 标准包 | ¥30 | 3200 | +200（6.7%） | 6.4 万字 |
  | 超值包 | ¥50 | 5750 | +750（15%） | 11.5 万字 |
  | 尊享包 | ¥100 | 12500 | +2500（25%） | 25 万字 |

  「约可合成」由后端按**当前** `MEMBER_POINTS_PER_1000_CHARS` 现算（`est_chars`），不写死。
- **入账只认订单里的 `points` 快照**：套餐改名/调价后，历史订单仍看得懂、金额不受影响。
- **幂等**：同一订单重复通知只发一次积分，判据是积分流水 `ref == "order:<order_id>"` + `kind=purchase`。支付平台重试、用户连点都安全。
- 回调**不做 Bearer 鉴权**（支付平台不会带我们的 token），安全性完全靠验签 ⇒ `PAY_NOTIFY_SECRET` 未配置时回调端点一律 **503**，不提供「无密钥放行」的降级。
- 通道差异全部收敛在 `app/membership/pay.py`：`normalize_notify()` 做报文归一化、`verify_notify()` 做验签。**接新通道只改这个文件**，service / routes 不动。
- 当前 `MEMBER_MOCK_PAY=1` 提供「模拟支付成功」端点用于商城演示；**接入真实支付后必须置 0**。

### 个人收款的现实约束（选型参考）

个人主体拿不到微信/支付宝的官方商户回调。可选路径：

1. **第三方聚合支付**（个人可开通，有异步回调，费率约 2–3%）：对接成本最低，`pay.py` 加一个 provider 分支即可；主要风险是平台跑路/合规。
2. **平台代收 + 兑换码**（零风险、零费率）：在知识星球/小报童/微店等卖「激活码」，用户在本站 `POST /api/points/redeem` 兑换。**现有能力已支持**，不依赖任何支付接口。
3. **个人收款码 + 人工确认**：用户转账后提交订单号，管理员用 `tools/member_admin.py grant` 或管理 API 加分。零成本，但纯人工。

（当前落地的是「下单 + 幂等入账 + 回调契约」，1/2/3 任选其一接上即可，业务侧无需改动。）

## 七、前端入口

- `/account` 页面：登录/注册、资料编辑、改密码、积分余额、**积分商城（购买套餐）**、每日签到、优惠码兑换、流水列表。
- 顶栏右侧用户菜单：未登录显示「登录」，登录后显示昵称+积分余额，下拉可进个人中心/退出。
- token 存 localStorage（`wb-auth-token`），跨 `/podcast`、`/dubbing` 生效。

## 八、回滚方式

- 删除 `app/membership/` 目录、`tools/member_admin.py`；
- `app/routes/__init__.py` 去掉 membership 引用；
- `app/queue_worker.py` 去掉 `refund_task_points` 及两处 finally 调用；
- `app/routes/queue.py` 还原 submit/update/retry/cancel 四处（改动均有 `member_svc.ENFORCE` 开关包裹，不开启时逻辑与旧版一致）；
- 前端删除 `pages/AccountPage.tsx`、`components/UserMenu.tsx`、`api/members.ts`、`lib/auth.ts`，App.tsx 去掉 `/account` 路由，Header.tsx 去掉 UserMenu。
