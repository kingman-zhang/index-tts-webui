# 支付对接方案：mpay_v2（码支付 V2）→ 积分商城

> 状态：**Gate 1–5 全部落地（2026-10-14）** = 后端 + 前端 + 配置守卫 + **真单联调**。
> 全量离线回归 **48/48**，mpay 通道专项 **100/100**，前端交互取证 **29/29**（真点击 → 真状态机），
> 真单**下单成功**（§12.1，返回字段确认为 `payurl`）。
> 🔴 **唯一阻塞：线上跑的还是 10-10 的旧代码** ⇒ `GET` 回调被 405 拒掉。
> **部署新代码前，请勿付任何真实款项**（付了积分也记不上）。见 §12.4。
> 实现记录见 §9（后端）、§10（前端）、§11（配置守卫）、§12（联调实测）。
> 目标通道：`https://mpay.lizigege.com/`，收款方式 = **微信个人收款监听**（mpay 插件 `wxpay_receipt`）。
> 关联文档：`MEMBERSHIP.md`（会员/积分体系）、`STORAGE_MIGRATION_PLAN.md`（账本上库）。
>
> 与既有铁律的关系：`MEMBERSHIP.md` 里写过「接新支付只改 `app/membership/pay.py`，service / routes 不动」。
> **本次需要修正这条铁律** —— 见 §5.1「铁律需要扩展」。

---

## 0. 结论先行

| 项 | 结论 |
|---|---|
| 对接协议 | ✅ **已定：ePay V1（MD5 签名）**。理由见 §4.1 |
| 支付方式 | ✅ **已定：只开微信**（`type=wxpay`） |
| 实施范围 | ✅ **已定：后端先行 → 前端出码 UI 已补完**（Gate 1–4 全部落地，2026-10-14） |
| 公网回调 | ✅ 你已确认**有公网域名/端口** —— 只需把确切地址给我（§7.1-3） |
| 核心改动 | 后端：通道可插拔（`pay_*.py`）＋ 出码端点 ＋ 单条订单查询 ＋ 回调多载体；前端：出码弹窗 ＋ 付款后轮询 |
| 入账路径 | **完全复用**现有 `settle_order()`（CAS 抢单 + 唯一索引幂等），一行不改 |
| 🔴 待解风险 | **金额校验**：个人收款监听会对金额微调（用于区分订单），已按 **`ge`（多付放行、少付拒绝、按订单快照入账）** 落地；真单仍建议实测一次（§4.2） |
| 必填配置 | mpay 的 `pid` / `KEY` / 我们的公网回调地址 / 站点地址 —— 见 §7 |
| 合规 | 个人收款监听属平台规则外的玩法，官方文档亦提示「自行确认合规边界」，账号风险自担 |

---

## 1. mpay 侧协议速查（ePay V1，本次建议使用）

### 1.1 下单（拿支付二维码）

```
POST https://mpay.lizigege.com/mapi.php
Content-Type: application/x-www-form-urlencoded
```

| 字段 | 必填 | 示例 | 说明 |
|---|---|---|---|
| `pid` | 是 | `1001` | 商户 ID |
| `type` | 是 | `wxpay` | 支付方式：`alipay` / `wxpay` / `qqpay` |
| `out_trade_no` | 是 | `ord_a1b2c3d4e5f6` | 我们的订单号（**直接用现有 `order_id`**） |
| `notify_url` | 是 | `https://站点/api/pay/notify/mpay` | 异步回调地址（**必须公网可达**） |
| `return_url` | 否 | `https://站点/account` | 付款后页面跳转地址 |
| `name` | 是 | `体验包` | 商品名（≤127 字节，超出自动截断） |
| `money` | 是 | `10.00` | **单位：元**，字符串，最多 2 位小数 |
| `clientip` | 是 | `1.2.3.4` | 下单用户 IP |
| `device` | 否 | `pc` | `pc` / `mobile` / `qq` / `wechat` / `alipay` / `jump` |
| `param` | 否 | | 原样回传（可用于携带 `user_id`） |
| `sign` | 是 | `e10adc...` | 见 §1.3 |
| `sign_type` | 是 | `MD5` | |

**响应（JSON）** — ⚠️ V1 的 `code == 1` 才是成功（V2 是 `0`）：

```json
{ "code": 1, "msg": "success", "trade_no": "20160806151343349",
  "payurl": "https://.../pay/wxpay/xxx",     // 跳转链接
  "qrcode": "weixin://wxpay/bizpayurl?pr=xxx", // 二维码内容（前端用它生成二维码）
  "urlscheme": "weixin://dl/business/?ticket=xxx" }
```

> `payurl` / `qrcode` / `urlscheme` **只会返回其中一个**，按 `device` 与支付方式而定。
> 微信个人收款监听场景，实测**大概率是 `qrcode`**（一段内容，前端渲染成二维码给用户扫）。

### 1.2 异步回调（mpay → 我们）

```
GET https://站点/api/pay/notify/mpay?pid=..&trade_no=..&out_trade_no=..&type=..
    &name=..&money=10.00&trade_status=TRADE_SUCCESS&sign=..&sign_type=MD5
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `pid` | 是 | 商户 ID |
| `trade_no` | 是 | mpay 平台订单号 |
| `out_trade_no` | 是 | **我们下单时的订单号**（入账靠它找单） |
| `type` | 是 | 支付方式 |
| `name` | 是 | 商品名 |
| `money` | 是 | **单位：元**，字符串 |
| `trade_status` | 是 | **只有 `TRADE_SUCCESS` 表示成功** |
| `param` | 否 | 原样回传 |
| `sign` | 是 | 见 §1.3 |
| `sign_type` | 是 | `MD5` |

- 请求方式：**GET**；
- 我们收到后**必须返回纯文本 `success`**（不是 JSON），否则 mpay 会按失败重推。

### 1.3 签名规则（MD5）

```
1. 取所有参数（含或不含 sign/sign_type 都行，反正下面要排除）
2. 排除 sign、sign_type，以及空值
3. 按参数名 ASCII 升序排序
4. 拼成 k=v&k2=v2（参数值**不做 URL 编码**）
5. 末尾直接拼接商户密钥 KEY（不是 &key=KEY，是裸拼）
6. 取 md5 十六进制小写

sign = md5("k1=v1&k2=v2" + KEY)
```

- 下单时：用发给 mpay 的参数（除 `sign`/`sign_type`）算；
- 回调时：用 mpay 发来的参数（除 `sign`/`sign_type`）算，与 `sign` 比对。

**示例**（KEY = `testkey`）：
```
参数: {pid:1001, type:wxpay, out_trade_no:T1, money:10.00, ...}
排序拼串: "money=10.00&out_trade_no=T1&pid=1001&type=wxpay..."
明文:     "money=10.00&out_trade_no=T1&pid=1001&type=wxpay...testkey"
sign = md5(明文)
```

### 1.4 订单查询（备用 / 对账用）

```
GET https://mpay.lizigege.com/api.php?act=order&pid={pid}&key={KEY}&out_trade_no={订单号}
→ { code:1, trade_no, out_trade_no, status: 1已支付/0未支付, money, endtime, ... }
```

用途：回调丢失时的**兜底主动查单**（可选，见 §6 Gate 3）。

---

## 2. 本项目现状（改动前的链路）

```
前端 AccountPage.buy(pack)
  ├─ POST /api/points/orders {pack_id}         → 落一条 pending 订单，out_trade_no = order_id
  └─ if mock_pay_enabled: POST .../mock-pay    → service.mock_pay → settle_order → 发积分
     else: 提示「支付通道接入中」（死路）
```

- **回调端点**：`POST /api/pay/notify/{provider}`，**只吃 JSON**（`await request.json()`），
  用 `PAY_NOTIFY_SECRET` 做 **HMAC-SHA256** 验签，归一化后调
  `service.settle_by_out_trade_no(out_trade_no, provider, amount_fen)`，**返回 JSON**。
- **入账**：`settle_order()` —— 校验金额相等 → `CAS pending→paid` → 发 `purchase` 流水。
  **幂等已就绪**（CAS + `(user_id, ref, kind)` 唯一索引），并发/重推安全。
- **商城状态**：`/api/points/packs` 返回 `mock_pay_enabled` / `pay_channel_ready`，前端按此分叉。

**现有契约与 mpay 的三处不匹配**：

| 维度 | 现有 | mpay V1 | 必须改 |
|---|---|---|---|
| 回调载体 | POST + JSON | **GET + query** | ✅ 路由层 |
| 验签算法 | HMAC-SHA256(排序串) | **MD5(排序串 + KEY)** | ✅ pay.py |
| 回调返回 | JSON | **纯文本 `success`** | ✅ 路由层 |
| 归一化字段 | `amount_fen` / `amount` | **`money`（元）** | ✅ pay.py |

---

## 3. 对接后的目标链路

```
①  前端点「购买」
       └─ POST /api/points/orders {pack_id}
             └─ create_order() 落 pending 单（out_trade_no = ord_xxxxxxxxxxxx）   [复用现有]

②  前端拿到订单 → 立即请求出码
       └─ POST /api/points/orders/{order_id}/pay                              [新增]
             └─ pay.create_charge() → POST mpay /mapi.php (type=wxpay)
                   └─ 返回 {pay_type, pay_info}  ── 存进订单 + 返回前端

③  前端按 pay_type 渲染：
       qrcode    → 把 pay_info 渲染成二维码（弹窗）
       payurl    → 新标签页打开 / 或者也转成二维码（个人收款码链接可转二维码）
       urlscheme → 跳转（移动端）

④  用户扫码付款到【个人微信收款码】
       └─ mpay 的监听工具（Go 直连 / Chromium）侦测到收款 → 匹配订单

⑤  mpay 回调我们：
       GET /api/pay/notify/mpay?...&trade_status=TRADE_SUCCESS&sign=...
             ├─ 验签（MD5）                                      [改 pay.py]
             ├─ normalize_notify → {out_trade_no, amount_fen, trade_status}  [改 pay.py]
             ├─ trade_status != TRADE_SUCCESS → 回 "success" 但不入账
             └─ service.settle_by_out_trade_no(...)              [复用现有，一行不改]
       ← 返回纯文本 "success"                                    [改路由层]

⑥  前端轮询订单列表 / 单订单状态 → 显示「已到账 +N 积分」
```

**关键点**：入账这一段（⑤ 的后半）**完全复用**现有幂等路径，对接新通道**不碰账本逻辑**。

---

## 4. 关键决策（D1 / D4 / D5 ✅ 已定；D2 / D3 / D6 待定）

### 4.1 D1 — 协议版本 ✅ 已定：V1（MD5）

| | **V1 · MD5** ⭐建议 | V2 · SHA256WithRSA |
|---|---|---|
| 签名 | `md5(排序串 + KEY)` | 商户私钥签、平台公钥验 |
| 密钥管理 | 1 个字符串 KEY | 需生成密钥对 + 配平台公钥 |
| 现有 `pay.py` 契合度 | **高**（已是「ASCII 排序拼串」，只差最后一步换 MD5） | 低（要引 `cryptography`，验签规则更多） |
| 时间戳防重放 | 无（靠 HTTPS + KEY 保密） | 有 `timestamp` 校验 |
| 下单入口 | `/mapi.php` | `/api/pay/create` |
| 生态资料 | 最多（易支付标准形态） | 较少 |

**→ 已定 V1。** 改动最小、依赖最少、易排错。个人收款小规模场景下，V2 的 `timestamp` 带来的收益不抵复杂度。

### 4.2 D2 — 金额校验策略（🔴 必须先实测）

现有 `settle_order` 对回调金额做**严格相等**校验（不等即 400）。**个人收款监听模式下，mpay 可能为了区分不同订单而对金额做微调**（同一张收款码同时可能有多个待付订单，靠金额后两位区分）。**若确实微调，严格校验必然全部拒收。**

三种处理，建议实测后选：

- **A. 严格相等**（现行为）：mpay 不改金额时才可用。
- **B. 只校验下限**（`回调金额 ≥ 订单金额`）：防少付资损，容忍向上微调。**推荐作为默认**。
- **C. 完全不校验**：只认 `out_trade_no` + 验签。最松，风险最高，不建议。

> 落地形式：加一个开关 `MPAY_AMOUNT_CHECK=strict|ge|off`，默认 `ge`。
> **需要你确认**：你第一笔测试时，mpay 回调/收款页显示的金额，与你下单的金额**是否一字不差**？

### 4.3 D3 — 前端支付呈现

- **A. 弹窗二维码（推荐）**：PC 网页扫码最自然，不打断当前页面。
- **B. 新标签页跳转收银台**：适合 `payurl`，移动端体验好。
- **C. 两者都做**：按 `pay_type` 自动分支（qrcode→弹窗，payurl→跳转）。

建议 **C**（实现成本增加很小，覆盖 PC + 移动）。二维码渲染需前端加一个轻量库（如 `qrcode`），或后端直接出 PNG。

### 4.4 D4 — 支付方式范围 ✅ 已定：只开微信

- **→ 已定 A：只开微信**（`type=wxpay`）—— 与个人收款监听一致，范围最小。
- ~~B. 微信 + 支付宝~~（需 mpay 侧另配支付宝收款通道，本期不做）

### 4.5 D5 — 本次实施范围 ✅ 已定：后端先行

- **→ 已定 A：后端先行** —— `pay.py` + 出码端点 + 回调端点 + 测试；前端先用 `curl` 验证。
  前端出码 UI（§5.6）推到下一期，一次只动一处、可单独回滚。
- ~~B. 后端 + 前端一起~~
- ~~C. 全量含退款/查单兜底~~

### 4.6 D6 — `MEMBER_MOCK_PAY` 处置

接真实支付后**必须置 0**（否则任何登录用户都能给自己免费入账）。建议：
- 生产 `.env` 写 `MEMBER_MOCK_PAY=0`；
- 同时把**代码默认值从 `"1"` 改为 `"0"`**（现在默认开着，忘配就是敞口）；
- 灰度期可用一笔 **0.01 元真实订单**验证，不靠 mock。

---

## 5. 改动清单（文件级）

> 范围 = D5 选 A（后端先行）。前端改动列在 §5.6。

### 5.1 ⚠️ 铁律需要扩展

原铁律「接新支付只改 `pay.py`」假设了**所有通道的回调载体都是 POST+JSON**。mpay 打破了它：
回调是 **GET + query**，且要求**返回纯文本 `success`**。这两件事发生在**路由层**，`pay.py` 管不到。

**处理方式（建议）**：把 provider 的「载体形态」也收进 `pay.py` 的 provider 元数据，
路由层只做**通用适配**：

```python
# pay.py
PROVIDERS = {
    "mpay": {"reply": "success", "methods": ("GET", "POST"), "verify": verify_mpay, ...},
    "__default__": {"reply": "json", "methods": ("POST",), ...},
}
```

这样「通道差异」仍然只写在 `pay.py` 一个文件里，铁律的精神得以保留，只是**描述从"只改签名"升级为"只改 pay.py"**。

### 5.2 `app/membership/pay.py` — 重写

| 函数 | 改动 |
|---|---|
| `_sign_v1(params, key)` | **新增**：ePay V1 MD5 签名（§1.3） |
| `verify_notify(payload, secret)` | **改**：按 provider 分支；`mpay` 走 MD5 验签 |
| `normalize_notify(provider, payload)` | **改**：`mpay` 分支解析 ePay 字段（`money` 元→分、`trade_status`） |
| `create_charge(order, cfg, notify_url, return_url, clientip)` | **新增**：async，POST `/mapi.php`，返回 `{trade_no, pay_type, pay_info}` |
| `PAID_STATUSES` | 已含 `TRADE_SUCCESS` ✅ 无需改 |
| `PROVIDERS` 元数据 | **新增**：载体形态（reply / methods） |

⚠️ `httpx` 会读 `HTTP_PROXY` 环境变量。支付调用要**显式 `trust_env=False`** 或确保 `.env` 的 `NO_PROXY` 覆盖 mpay 域名（见 `MEMORY.md` 沙箱网络铁律）。

### 5.3 `app/membership/service.py` — 小改

| 改动 | 说明 |
|---|---|
| 新增配置常量 | `MPAY_BASE_URL` / `MPAY_PID` / `MPAY_KEY` / `MPAY_TYPE` / `MPAY_AMOUNT_CHECK` / `PAY_NOTIFY_BASE`(我们的公网根) |
| `create_charge_for_order(user, order_id)` | **新增**：校验归属 → 调 `pay.create_charge` → 把 `pay_type/pay_info/pay_created_at` 存进订单 → 返回 |
| `list_packs()` | `pay_channel_ready` 判据从 `PAY_NOTIFY_SECRET` 改为 `MPAY_KEY`；`mock_pay_enabled` 保持读 `MOCK_PAY` |
| `settle_order()` | **不动**（幂等路径）—— **除非** D2 选了非严格校验，则在金额校验那一处加分支 |

### 5.4 `app/membership/routes.py` — 回调端点改造

```python
# 改：接受 GET + POST，参数来自 query / form / JSON 三种载体
@router.api_route("/api/pay/notify/{provider}", methods=["GET", "POST"])
async def pay_notify(provider: str, request: Request):
    ...
    # 返回：mpay → PlainTextResponse("success")
    #       其他 → JSON（保持旧契约，测试不破）
```

**新增出码端点**：
```python
@router.post("/api/points/orders/{order_id}/pay")
def order_pay(order_id: str, request: Request, user = Depends(get_current_user)):
    """为 pending 订单向 mpay 下单并返回支付信息（可重复调用=重新出码）。"""
```

⚠️ 出码需要用户 IP（`clientip`），反代下要读 `X-Forwarded-For`。

### 5.5 配置 / 文档

- `app/config.py` / `.env.example`：新增 `MPAY_*` 段（照现有注释风格，写清"改了要重启"）
- `MEMBERSHIP.md`：补一节「真实支付通道 = mpay」
- `命令大全.md`：补「手动验证回调」的 curl 命令（用 KEY 现场算 sign）

### 5.6 前端（D5 选 B 时）

- `api/members.ts`：新增 `orderPay(orderId)`；`PointsOrder` 加 `pay_type/pay_info`
- `AccountPage.tsx`：
  - `buy()` 的 else 分支改为「createOrder → orderPay → 弹二维码」
  - 订单列表 pending 项的「模拟支付」按钮 → 改为「去支付 / 重新出码」
  - 付款后轮询订单状态（复用现有 `loadOrders`，加个 3~5s 的短轮询直到 paid）
- 依赖：二维码渲染（`qrcode` / `qrcode.react`），或在后端出一个 PNG data-URI

### 5.7 测试

- `tests/test_pay_mpay.py`（新增）：
  - 签名/验签向量（含排序、空值排除、大小写）
  - `normalize_notify` 的元→分换算、缺字段返回 None
  - `create_charge` 用 `httpx.MockTransport` 打桩（**不打真实 mpay**）
  - 回调端到端：GET query + 正确 sign → 入账；错 sign → 401；重复推 → 幂等
  - 非 `TRADE_SUCCESS` → 不入账但回 `success`
- `tests/test_points_purchase.py`：现有 JSON 回调用例**必须继续通过**（验证向后兼容）

---

## 6. 分阶段实施（Gate）

| Gate | 内容 | 产出 | 依赖 |
|---|---|---|---|
| **G0** | 你提供配置 + 拍板 D1~D6 + **实测金额是否微调** | 本节所有决策落定 | 你的输入 |
| **G1** | `pay.py` 重写 + 纯函数单测（签名/验签/归一化） | 不依赖网络，全绿 | G0 |
| **G2** | 出码端点 + `create_charge`（MockTransport 打桩测试） | 本地可验 | G1 |
| **G3** | 回调端点改造 + 端到端联调（**一笔 0.01 元真实订单**） | 真实入账取证 | G2 + 公网回调可达 |
| **G4** | 前端出码 UI + 轮询 | 可点击走通 | G3 |
| **G5** | 上线：`.env` 配 `MPAY_*`、`MEMBER_MOCK_PAY=0`、部署验证 `/api/version` | 生产可用 | G4 |

每个 Gate 独立可回滚；G1~G3 都在后端，前端不影响线上。

---

## 7. 需要你提供的配置 / 信息

### 7.1 必填（阻塞项）—— **当前只差这 4 项**

| # | 项 | 说明 / 从哪拿 |
|---|---|---|
| 1 | `MPAY_PID` | mpay 商户后台 → API 信息 → 商户 ID |
| 2 | `MPAY_KEY` | mpay 商户后台 → API 信息 → 商户密钥（MD5 用） |
| 3 | **公网回调地址** | 我们 backend 的公网根地址（`https://域名` 或 `http://公网IP:端口`），mpay 必须能访问到。路径固定 `/api/pay/notify/mpay` |
| 4 | **站点地址** | 前端访问地址（`return_url` 用，如 `https://xxx/account`） |

（协议 V1、支付方式只微信、实施范围后端先行 —— ✅ 已定，无需再答。）

### 7.2 需要实测确认（重要）

| # | 问题 |
|---|---|
| 7 | 你第一笔测试时，**mpay 回调里的 `money` 与你下单金额是否完全一致**？（决定 D2） |
| 8 | 你第一笔测试时，`/mapi.php` 返回的是 `qrcode` / `payurl` / `urlscheme` 哪个？（决定前端形态） |
| 9 | mpay 是否有 **IP 白名单**？（要把我们服务器出口 IP 加白） |
| 10 | 我们的 backend 目前**是否已有公网域名/端口**能直接被 mpay 访问？ |

### 7.3 可选

| # | 问题 |
|---|---|
| 11 | 套餐是否要调整（现有最小 ¥10；个人收款下是否加 ¥1 / ¥5 小额包？） |
| 12 | 是否需要**退款**（V1 退款需 mpay 后台开开关） |
| 13 | pending 订单**超时**多久自动作废（建议 30 分钟）？ |

---

## 8. 边界与风险

1. **回调必须公网可达** —— 这是头号阻塞。若无，需要反代 / 隧道（frp、Cloudflare Tunnel 等），或把回调落到已有公网入口。
2. **金额校验**（§4.2）—— 处理不当要么全部拒收，要么资损。
3. **个人收款监听的合规边界** —— 属平台规则外玩法，账号存在被限制风险，官方文档亦明确提示自行评估。请自行判断。
4. **回调无鉴权、只靠 MD5** —— `MPAY_KEY` 一旦泄露即可伪造入账。`KEY` 只写服务器 `.env`，不入库、不进日志。
5. **订单超时未处理** —— 现有 pending 订单会一直挂着；建议加超时作废（§7.3-13）。
6. **`out_trade_no` 可重复下单** —— 同一订单重复调 `/mapi.php` 的行为需实测（可能复用同一支付单，也可能报错）；方案里对 30 分钟内已出码的订单**直接复用** `pay_info`，避免重复下单。
7. **对账** —— `tools/reconcile.py` 已能报出「paid 无流水 / 有流水非 paid」；接支付后建议按周跑一次，并与 mpay 后台账单核对。

---

## 9. 实现记录（2026-10-14，后端先行）

### 9.1 最终形态：通道可插拔

按你「mpay 不是标准方案，将来接标准支付**不能与它耦合**」的要求，支付通道被拆成
**门面 + 通道模块**，与项目既有 `store.py`（门面）+ `store_file/mongo`（后端）同一套路：

| 文件 | 角色 |
|---|---|
| `app/membership/pay_contract.py` | **新增**。通道层的错误契约与共享常量（`PayChannelError` / `PAID_STATUSES` / `AMOUNT_POLICIES`），避免「门面 ↔ 通道」循环 import（对齐 `store_contract.py`） |
| `app/membership/pay.py` | **重写为门面**：注册表 + 按 provider 分发 + 契约校验（`_REQUIRED_API`，**import 期 fail fast**）。不含任何通道业务逻辑 |
| `app/membership/pay_mpay.py` | **新增**。mpay（ePay V1 / MD5）通道 —— 签名、验签、归一化、下单全在这里 |
| `app/membership/pay_generic.py` | **新增**（旧 `pay.py` 的实现迁来）。通用 HMAC-SHA256 通道，`PROVIDER_ID="*"` 通配兜底，保住历史契约与 `test_points_purchase.py` |

**通道契约**（每个通道模块必须实现）：`PROVIDER_ID` / `TITLE` / `NOTIFY_METHODS` /
`NOTIFY_REPLY` / `NOTIFY_REPLY_FAIL` / `AMOUNT_POLICY` / `configured()` / `config_problem()` /
`verify()` / `normalize()` / `create_charge()`。

⇒ **将来接标准支付**：新开 `pay_wechat_official.py` 实现这 11 项，把 `.env` 的
`PAY_PROVIDER` 指过去即可。`service.py` / `routes.py` **一行不用改**（这正是解耦的落点）。

### 9.2 其余改动

| 文件 | 改动 |
|---|---|
| `service.py` | 新增 `PAY_PROVIDER` / `PAY_PUBLIC_BASE_URL` / `PAY_RETURN_URL` / `PAY_AMOUNT_CHECK` / `PAY_CHARGE_REUSE_SECONDS` / `MPAY_*`；`pay_channel_ready()`；`create_charge_for_order()`（async）；`settle_order` / `settle_by_out_trade_no` 加 `amount_policy`（默认 `strict`，**向后兼容**）；`_check_amount()` 策略化 |
| `routes.py` | 回调端点 `POST` → `api_route(GET, POST)`；新增 `_collect_payload()`（query / form / JSON 三载体）与 `_client_ip()`；应答按通道声明走纯文本或 JSON；新增 `POST /api/points/orders/{order_id}/pay` |
| `store.py` / `store_file.py` / `store_mongo.py` | 新增 `update_order_fields()`（落出码信息；**明令不得用它改 status**） |
| `.env.example` | 新增「真实支付通道」段（4 个通用项 + mpay 4 项） |
| `build_info.py` | `/api/version` 的 membership 段新增 `pay`（`enabled` / `ready` / `problem` / `known`） |
| `tests/test_pay_mpay.py` | **新增，80 项断言** |

### 9.3 关键实现决策

1. **配置的权威源仍是 `service.py`**：通道模块**延迟 import** `service` 读配置（`_conf()`），
   所以「配置改了不生效」只需查一个地方，测试也能直接改常量即时生效（与既有测试风格一致）。
2. **回调金额策略由通道声明**（mpay = `ge`），`PAY_AMOUNT_CHECK` 只是运维覆盖口子。
   `settle_order` 的默认仍是 `strict` ⇒ 现有测试与调用点行为**不变**。
3. **出码信息落库 + 30 分钟复用窗口**：既避免「同一 out_trade_no 重复下单」（各通道行为不一），
   又满足「用户关掉二维码要重开」。
4. **应答形态由通道声明**：`NOTIFY_REPLY="json"` 是通用通道（老契约），`="success"` 是 mpay。
   路由层只读这个声明来构造响应，不写 `if provider == "mpay"`。
5. **通配通道保历史**：`get_provider()` 精确优先、否则落 `*` ⇒ `/api/pay/notify/任意名`
   仍走通用通道，`test_points_purchase.py` **未改动即通过**。
6. **下单 HTTP 客户端可注入**（`create_charge(order, ctx, client=None)`）—— 测试用
   `httpx.MockTransport` 打桩，与 `test_engine_302ai.py` 的做法一致；真实调用时
   `trust_env=False` 显式绕开环境代理。

### 9.4 验证

- `tests/test_pay_mpay.py` **80/80**：签名硬编码向量（含中文商品名）、验签（含大写 / 篡改）、
  归一化（元→分 / 缺字段）、下单打桩（qrcode/jump/urlscheme、code≠1、HTTP 500）、
  出码端到端（落库 / 复用 / 越权 404 / 未登录 401）、回调（GET + 纯文本 `success` / 幂等 /
  验签失败 401 `fail`）、**金额 ge（多付放行、少付拒绝、按快照入账）**、非成功状态、
  通配兜底、未配置 503、缺 `PAY_PUBLIC_BASE_URL` 503。
- `tests/test_points_purchase.py` **未改动即通过**（历史 JSON 回调契约向后兼容）。
- **全量离线回归 48/48**（改造前基线 47 + 新增 1 个测试文件）。

### 9.5 尚未做（等你的输入）

- **联调**：`.env` 已填（2026-10-14），但 `MPAY_PID` 仍是商户编号 —— 改成纯数字后跑一笔真单。
  三个配置错的守卫与实测过程见 **§11**。
- **待实测回填**：`/mapi.php` 实际返回的是 `qrcode` / `payurl` / `urlscheme` 哪一个
  —— 代码三种都已支持，只影响前端渲染方式（§10 已把三种都做了）。

---

## 10. 实现记录（2026-10-14，前端 · Gate 4）

### 10.1 先回答一个概念问题：付款后是「回调」还是「轮询」？

**两条路都要，各管各的** —— 这是理解前端怎么写的前提：

```
用户扫码付款
   │
   ├─① 支付平台 → 我们的后端   （服务端到服务端，**权威入账**）
   │     mpay 监听工具确认收款 → GET /api/pay/notify/mpay → 验签 → settle_order() → 订单变 paid
   │
   └─② 前端 → 我们的后端        （**只是问「好了没」**）
         每 2s GET /api/points/orders/{id} → status 变 paid 就收手
```

- **回调**是真正把钱记成积分的唯一动作，跑在服务端；浏览器**看不到也接收不到**。
- **轮询**是给浏览器的：没有它，用户付完钱必须手动刷新页面才看到积分到账。
- 轮询**只读**、**不具备入账能力** —— 它造不出假账（假账只可能来自回调验签被绕过）。

一句话：**回调是记账，轮询是报信。**

### 10.2 改了什么

| 文件 | 改动 |
|---|---|
| `app/membership/routes.py` | 新增 `GET /api/points/orders/{order_id}` —— 轮询用的单条查询（只读、无副作用） |
| `src/api/members.ts` | `PointsOrder` 补出码字段；`PointsPackList` 补 `pay_provider`；新增 `order()` / `payOrder()` |
| `src/components/PayDialog.tsx` | **新增**：出码弹窗，按 `pay_type` 三种形态渲染 + 付款后轮询 + 成功态自动关闭 |
| `src/pages/AccountPage.tsx` | 商城接线：购买改走真实通道出码；待支付行加「去支付」；双开告警 |
| `package.json` | 新增依赖 `qrcode.react`（渲染二维码，无传递依赖） |

新增/更新的验证与探针：`outputs/verify-pay-dialog.js` + `src/__probe_pay.tsx`。

### 10.3 关键实现决策

1. **为什么新增「单条订单查询」而不是复用列表接口**：列表要先扫全量 + 排序 + 分页
   （单文件存储下 O(全部订单)），并发下单时还得靠 `limit=1` 猜哪条是刚才那单。
   单条查询语义明确、代价恒定。
2. **轮询节奏**：前 2 分钟每 **2s**（回调通常几秒内到，做到「付款即见」），之后降到 5s。
   单次查询失败（网络抖动 / 后端重启）**不打断轮询**，下一拍继续。
3. **成功只认一次**：轮询与「我已完成支付」手动查询可能同时命中，用 `paidRef` 兜住，
   保证入账回调只触发一次（不会重复弹提示 / 重复刷新）。
4. **回调放进 ref**：父组件每次重渲染都产生新的函数身份，若直接进 `useEffect` 依赖，
   轮询计时器会被反复重建（表现为「一直在等却从不查」）—— 所以 `onPaid` / `onClose`
   一律走 ref，让 `markPaid` 保持稳定。
5. **弹窗 `key={order_id}`**：换单必须重置内部状态，否则会沿用上一单的支付码。
6. **不做自动重新出码**：后端复用窗口（`PAY_CHARGE_REUSE_SECONDS`，默认 30 分钟）内重新
   出码只会拿到**同一张**，无意义；超时只给一句「若支付码已失效请关闭后重试」。
7. **兜底 `pay_type=""`**：后端在无可支付内容时会直接报错，正常不会出现；仍渲染原文，
   避免用户面对一个空白弹窗。
8. **双开告警**：`pay_channel_ready && mock_pay_enabled` 同时为真时给**红色**提示
   （`MEMBER_MOCK_PAY=1` 意味着任何登录用户都能免费给自己入账）。这是暴露配置错误，
   不是替用户做决定 —— 真实通道就绪时前端**优先走真实通道**。

### 10.4 验证（`outputs/verify-pay-dialog.js`，**29/29**）

真组件探针（`src/__probe_pay.tsx` 挂 `ShopCard` / `PayDialog`）+ `window.fetch` 全量 mock，
每条断言都是「真点击 → 真状态机 → 真 DOM」：

- 点「购买」只调出码端点 1 次、不走模拟支付；弹窗金额/积分/引导文案正确
- **二维码编的就是通道返回的 `pay_info`**（与一份隐藏的参照二维码逐字节比对，168×168）
- 付款前一直轮询且不误判成功；mock 把订单翻成 `paid` 后 → 成功态 → 自动关闭
- 成功提示**总计仅 1 次**（证明轮询未双发）；全程只出码 1 次
- 待支付行「去支付」可重开弹窗，复用窗口内显示复用提示
- `jump` / `urlscheme` / 未知形态 三种渲染各自正确；Esc 关闭且不触发入账
- 双开告警出现；无通道时回落演示模式（点购买直接模拟入账、不出码）
- 全程原生 dialog **0 次**

后端：全量离线回归 **48/48**；新端点手工验证 `200`（本人）/ `404`（不存在或越权）/ `401`（无鉴权），
且 `GET /api/points/orders` 列表未被新子路由截胡。

---

## 11. 实现记录（2026-10-14，配置守卫 · 联调实测后）

背景：用户按 §7 填完 `.env` 后实测，出码这一半被**三个配置错**卡住（全是 .env 侧、代码没错）。
用户选了「都加」—— 把这三个错做成**开箱即拦**（属**报错质量**，不动业务逻辑）。

### 11.1 实测发现的三个配置错

| # | 现象 | 根因 |
|---|---|---|
| 1 | 出码被 mpay 拒：`商户ID 必须是整数。` | `MPAY_PID` 填了**商户编号** `M2026...`，而 ePay V1 的 `pid` 契约是 **Int** |
| 2 | 二维码能出、**回调永不来**（静默失败） | `PAY_PUBLIC_BASE_URL` 填了**完整回调 URL** ⇒ 拼成 `.../notify/mpay/api/pay/notify/mpay`。⚠️ **mpay 不校验 notify_url、照样返回成功**，所以没有任何报错 |
| 3 | 任何登录用户可白拿积分 | `MEMBER_MOCK_PAY` 未写，而**代码默认值是 `"1"`** ⇒ 实测未付款直接调 `mock-pay` 就 +1000 积分 |

### 11.2 三个守卫（落点 / 行为）

| 守卫 | 落点 | 行为 | 不动业务逻辑 |
|---|---|---|---|
| `MPAY_PID` 必须**纯数字** | `pay_mpay.charge_problem()`（新增通道契约项） | 出码 503 + 点名 pid（认得出「你填的是商户编号」）；`/api/version` 的 `pay.problem` 同步点名；**且不向 mpay 发请求**（省一次白跑的往返） | ✅ 只加前置检查 |
| `PAY_PUBLIC_BASE_URL` 不能含 `/api/pay/notify` | `service.pay_public_base_problem()`（新增） | 出码 503 + 说清重复在哪一段。**不自动去重** —— 宽容化会让这个坑变成"有时对有时错"，更难查 | ✅ 只加前置检查 |
| `MEMBER_MOCK_PAY` 忘写 | `service.MOCK_PAY` 默认值 `"1"` → **`"0"`** | 默认值取**安全的那一侧**：写漏时应当是「关」 | ✅ 仅默认值 |

顺带把「拼回调地址」收成**唯一一处**：新增 `service.notify_url_for(provider_id)`，
出码与自检共用（原先 `create_charge_for_order` 里直接拼，自检没得看）。

### 11.3 一条**刻意**的分工（别顺手改掉）

新增的通道契约项 `charge_problem()` 与原有 `configured()` **分工不同**，这是本次的关键设计：

| | 回答什么 | 谁在用 |
|---|---|---|
| `configured()` | 配置**在不在**（`base_url` / `pid` / `key` 三项非空） | 回调端点 503 判定、`pay_channel_ready()` 的一半 |
| `charge_problem()` | 配置在不在 **+ 形态对不对**（如 pid 纯数字） | **只**用于出码与自检 |

⚠️ 为什么 `MPAY_PID` 形态错**只拦出码、不拦回调**：回调验签**只依赖 `MPAY_KEY`**。
若把回调也拦掉，「pid 配错期间到账的钱」就记不上账了 —— 那是比"少一次往返"严重得多的问题。
`tests/test_pay_mpay.py` 的 `[17]` 把这条钉住了（pid 错时出码 503 **且未发请求**，但同一订单的回调仍 200 `success` 并入账）。

### 11.4 本次同时改掉的 `.env`（用户已授权直接改）

| 项 | 原值 | 改为 |
|---|---|---|
| `PAY_PUBLIC_BASE_URL` | `https://media.lizigege.com/api/pay/notify/mpay` | `https://media.lizigege.com` |
| `MEMBER_MOCK_PAY` | （键不存在 ⇒ 吃代码默认 `1`） | `0`（**追加在文件末尾**） |
| `PAY_PROVIDER` | `mpay` | 不变 |

后续（同日晚，用户改完 `pid` 并授权后）：

| 项 | 新值 | 说明 |
|---|---|---|
| `MPAY_PID` | `M2026…` → **纯数字**（`1002`） | 用户自己从 mpay 后台改；守卫的点名随之解除（`ready=true`、`problem=""`） |
| `PAY_AMOUNT_CHECK` | `ge` → **留空** | 采纳建议，让各通道自己声明。实测验：留空后 `resolve_amount_policy()` 仍返回 mpay 的 `ge` |

### 11.5 验证

- `tests/test_pay_mpay.py` **100/100**（原 80 + 新增 `[17]`/`[18]`/`[19]` 共 20 条：
  pid 形态分层报错 / 未发请求 / **不拦回调** / 地址重复识别 / 未发请求 / 自检暴露 /
  `MEMBER_MOCK_PAY` 代码默认值用**干净子进程**验）。
- **全量离线回归 48/48**（与守卫前同基线，无回归）。
- 真实 `.env` 下 `/api/version` 实测：
  `mock_pay_enabled=False`、`notify_url=https://media.lizigege.com/api/pay/notify/mpay`（单段，正确）、
  `problem` 精确点名 `MPAY_PID`。
- 后端仍差：`MPAY_PID` 改成纯数字后跑一笔真单。→ **已完成，见 §12**。

---

## 12. 联调实测（2026-10-14 晚 · 真单）

配置补齐（`pid=1002` + key + 根地址 + 策略留空）后跑真实下单。**六条结论**：

### 12.1 ✅ 出码链路已通

```
POST https://mpay.lizigege.com/mapi.php
→ {"code":1,"msg":"提交成功",
   "trade_no":"PAY20261014203946817724",
   "payurl":"https://mpay.lizigege.com/payment/PAY20261014203946817724"}
```

- pid / key / **签名全部正确**（错误阶梯已走到最后 `验签` 之后）。
- **出码字段 = `payurl`**（不是 `qrcode` / `urlscheme`）⇒ 前端走 `pay_type="jump"`，
  渲染成跳转按钮 → 打开 mpay 收银台（SPA，二维码在那边）。
  **三种形态的代码没白写，但本次只用到 `jump`。**
- 走本项目模块 `pay_mpay.create_charge()` 的解析结果与裸打接口**逐字一致**。

### 12.2 🔴 mpay 通道是**按固定金额逐档配的** —— 套餐金额与通道必须一一对应

金额 `0.01` 被拒：

```
未找到可用支付通道：金额不在通道范围内（最小 10.00，最大 10.00）；
                  金额不在通道范围内（最小 30.00，最大 30.00）；
                  金额不在通道范围内（最小 50.00，最大 50.00）；
                  金额不在通道范围内（最小 100.00，最大 100.00）
```

⇒ 本环境有 4 个**固定金额**通道：`10 / 30 / 50 / 100` 元。

| 套餐 | 金额 | 结果 |
|---|---|---|
| `p1000` 体验包 | 10.00 | ✅ 命中 |
| `p3200` 标准包 | 30.00 | ✅ 命中 |
| `p5750` 超值包 | 50.00 | ✅ 命中 |
| `p12500` 尊享包 | 100.00 | ✅ 命中 |

⚠️ **新增运维约束**：**改套餐价必须同步在 mpay 后台加/改通道**，否则该套餐一点「购买」就报
「未找到可用支付通道」。好消息是失败发生在**下单时**（用户能看见的报错），不是静默失败。
**刻意不加代码守卫**：mpay 的报错已足够明确，而通道金额是**平台侧配置、随时可变** ——
把一份范围硬编码进代码，只会多出一处会漂移的真相。

### 12.3 ✅ 公网回调地址确实路由到我们的 backend

`https://media.lizigege.com/api/version` → `200` 且 `data_dir=/app/data`（Docker 容器内）⇒ 域名没写错。

### 12.4 🔴 **唯一阻塞：线上跑的是 10-10 的旧代码**

| 判据 | 线上实测值 | 说明 |
|---|---|---|
| `process_started_at` | `2026-10-10T01:41:11` | 进程 **10-10** 启动，而本次改动是 **10-14** |
| `membership` 键 | 含 `pay_notify_enabled`（旧字段）、**无 `pay`** | 是守卫之前的老版本 |
| `mock_pay_enabled` | `True` | 旧默认值 |
| `GET /api/pay/notify/mpay` | **405 Method Not Allowed** | 旧版回调端点只挂 `POST`，而 **mpay 回调走 GET** |

⇒ **不部署就永远收不到回调**（GET 被 405 拒，mpay 会重推、最终放弃）。
⇒ ⚠️ **部署完成前不要付真实款项** —— 付了积分也记不上（回调进不来），只能人工对账补发。

部署步骤：`feat-pay` 提交 → **ff 合 main** → 推 → 服务器 `bash tools/deploy_g1_autodl.sh`。
⚠️ 本次**前端也变了**（`PayDialog.tsx` 等）⇒ 不能 `--skip-frontend`，要重建 `podcast-web`。

### 12.5 关于「金额会被微调」—— 留待真付款定论

本环境通道 `min == max`（**固定金额**）⇒ 理论上回调 `money` 与订单金额应当**相等**，「微调」未必发生。
但也可能 mpay 在收银台侧对**同金额并发订单**做后两位微调 —— 只有真付一笔才能看到回调的实际 `money`。
**`ge` 对两种情形都安全**（相等放行、多付放行、少付拒收），故**保持不动**，不倒向 `strict`。

### 12.6 联调在 mpay 侧留下的痕迹

真单冒烟生成了数笔**待付订单**（`0.01` 元失败 1 笔 + `10.00` 元成功 2 笔，`out_trade_no` 前缀
`smoke*` / `mod*`），均**未付款、不扣款**，会在复用窗口外自然过期。要清理可在 mpay 后台按该前缀查。
