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

鉴权头：`Authorization: Bearer <token>` 或 `X-Auth-Token: <token>`；token 有效期默认 30 天。

## 二、配置（环境变量或 .env，全部可缺省）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMBER_REG_BONUS` | 100 | 注册赠送积分（0=关闭） |
| `MEMBER_CHECKIN_BONUS` | 5 | 每日签到积分（0=关闭） |
| `MEMBER_POINTS_PER_1000_CHARS` | 10 | 合成扣费：每千字扣分（0=关闭按量扣费） |
| `MEMBER_TOKEN_TTL_DAYS` | 30 | 会话有效期 |
| `MEMBER_ENFORCE` | 0 | **1=收费模式**：登录才可提交合成任务、预扣积分、失败自动退款；0=不扣费 |
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

## 六、扣费钩子（MEMBER_ENFORCE=1 时生效）

- **提交** `/api/queue/submit`（双人播客与单人配音共用入口）：按 `ceil(字数/1000) × 单价` 预扣；余额不足返回 402。
- **编辑** 未执行任务改稿 → 按新字数多退少补。
- **失败/取消/中断** → 自动全额退款（`queue_worker` 收尾处统一处理，幂等）。
- **重试** → 重新预扣。
- 流水中 `spend`（扣费）/`refund`（退款）与 task_id 关联，可对账。

## 七、前端入口

- `/account` 页面：登录/注册、资料编辑、改密码、积分余额、每日签到、优惠码兑换、流水列表。
- 顶栏右侧用户菜单：未登录显示「登录」，登录后显示昵称+积分余额，下拉可进个人中心/退出。
- token 存 localStorage（`wb-auth-token`），跨 `/podcast`、`/dubbing` 生效。

## 八、回滚方式

- 删除 `app/membership/` 目录、`tools/member_admin.py`；
- `app/routes/__init__.py` 去掉 membership 引用；
- `app/queue_worker.py` 去掉 `refund_task_points` 及两处 finally 调用；
- `app/routes/queue.py` 还原 submit/update/retry/cancel 四处（改动均有 `member_svc.ENFORCE` 开关包裹，不开启时逻辑与旧版一致）；
- 前端删除 `pages/AccountPage.tsx`、`components/UserMenu.tsx`、`api/members.ts`、`lib/auth.ts`，App.tsx 去掉 `/account` 路由，Header.tsx 去掉 UserMenu。
