# 统计接入：自建 umami

> 状态：前端接入代码**已完成并验证**（2026-10-19）。部署 umami 实例由你在服务器侧执行。
> 目标地址：`https://stats.lizigege.com`（独立子域，不要挂主站路径下）

## 一、umami 是什么，为什么选它

一句话：**一个开源的、不用 Cookie 的网站访问统计工具**，自己跑在自己的服务器上，数据不出门。

它统计的东西和百度统计/GA 属于同一类——访问量、来源渠道、页面路径、停留、设备、
自定义事件——但实现取向完全不同：

| | umami | 百度统计 / GA4 |
|---|---|---|
| 数据归属 | 你自己的 PostgreSQL | 第三方服务器 |
| Cookie | **不用**（靠 IP+UA 每日哈希做匿名去重） | 依赖 Cookie / 客户端 ID |
| 脚本体积 | ~2 KB | 几十 KB 起 |
| 界面 | 极简，一屏看完 | 层级深，需要学 |
| 被拦截器屏蔽 | 有，但比 GA 少得多 | GA 常被 uBlock 直接拦 |
| 成本 | 一台小机器 + 一个 Postgres | 免费（用数据换） |
| 合规 | 无跨站追踪，基本不需要 Cookie 同意条 | 需要 |

**为什么不选 GA**：你会把访客行为数据交给 Google，而且国内访问 GA 不稳定，
统计本身就失真。**为什么不选百度统计**：它的"来源"在国内渠道（知乎、小红书）上
识别很差，且同样存在数据外流和偶发加载失败。

**它的代价要说清楚**：umami 需要**一个 PostgreSQL**。这比 GA 那种"贴一行代码"重，
但比 Plausible（也要 PG）没差别；如果你连 Postgres 都不想养，更轻的替代是
GoatCounter（单二进制 + SQLite），代价是功能更素、中文生态少。
按你的场景（要区分知乎/小红书/公众号来源、要看转化漏斗）**umami 更合适**。

## 二、资源占用（先确认机器吃得下）

| 容器 | 常驻内存 | 说明 |
|---|---|---|
| umami | 约 120–200 MB | Node 应用，低流量下基本闲置 |
| postgres:16-alpine | 约 50–100 MB | 只有统计表，几个月也就几十 MB |

两台加起来 **200–300 MB**。你的主力是 GPU 推理，这点开销通常可以忽略，
但如果 AutoDL 实例内存吃紧（比如 16G 已经跑满），优先考虑 GoatCounter。

## 三、部署

### 1. 建目录，别混进项目仓库

统计服务是**独立基础设施**，建议单独放：

```bash
mkdir -p /data/umami && cd /data/umami
openssl rand -hex 32        # 生成 APP_SECRET，复制下来
openssl rand -base64 24     # 生成数据库密码，复制下来
```

### 2. `docker-compose.yml`

```yaml
services:
  umami-db:
    image: postgres:16-alpine
    container_name: umami-db
    restart: unless-stopped
    environment:
      POSTGRES_DB: umami
      POSTGRES_USER: umami
      POSTGRES_PASSWORD: 把上面生成的密码填这里
      # umami 官方建议数据库用 UTC：它自己按 UTC 存，展示时再按站点时区换算。
      # 这里跟着设 UTC，避免和容器时区叠加出偏移。
      TZ: UTC
    volumes:
      - umami-db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U umami -d umami"]
      interval: 10s
      timeout: 5s
      retries: 5

  umami:
    image: docker.umami.is/umami-software/umami:postgresql-latest
    container_name: umami
    restart: unless-stopped
    depends_on:
      umami-db:
        condition: service_healthy
    environment:
      DATABASE_TYPE: postgresql
      DATABASE_URL: postgresql://umami:把上面生成的密码填这里@umami-db:5432/umami
      APP_SECRET: 把上面生成的 APP_SECRET 填这里
      TZ: Asia/Shanghai
    # ⚠️ 只绑 127.0.0.1：对外由宿主 nginx 反代并挂证书。
    #    直接写 "3030:3000" 会把它暴露在公网，而 umami 后台是有登录页的。
    ports:
      - "127.0.0.1:3030:3000"

volumes:
  umami-db:
```

> 镜像 tag：`postgresql-latest` 跟的是"配 Postgres 的最新版"。生产上更稳的做法是
> 钉一个具体版本号（去 umami 的 GitHub Releases 查当前版本，写 `:vX.Y.Z`），
> 避免某天 `latest` 跳大版本把数据库 schema 升上去。**首次部署建议先钉版本。**

```bash
docker compose up -d
docker compose logs -f umami      # 等到 "Ready on http://localhost:3000"
```

### 3. nginx 反代（独立子域）

```nginx
server {
    listen 443 ssl http2;
    server_name stats.lizigege.com;
    ssl_certificate     /path/to/stats.fullchain.pem;
    ssl_certificate_key /path/to/stats.key;

    # 统计后台不建议对公网完全开放：加一个 IP 白名单，或者改用 basic auth
    # allow 你的公网IP; deny all;

    location / {
        proxy_pass http://127.0.0.1:3030;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**为什么必须是独立子域、不能是 `voice.lizigege.com/stats`**：
前端 nginx 的 `try_files ... /index.html` 会把任何未知路径回落成 SPA 的 HTML。
`/stats/script.js` 会返回一个 HTML 而不是 JS，tracker 直接失效——而且不报错。

### 4. 初始化

1. 打开 `https://stats.lizigege.com`，默认账号 **admin / umami**。
2. **第一件事改密码**（右上角 → 设置 → 账户）。
3. 进 **设置 → 网站 → 添加网站**：
   - 名称：`播客工坊`
   - 域名：`voice.lizigege.com`（只写主机名，不带 `https://`）
4. 保存后拿到 **Website ID**（UUID 格式），下面要用。
5. 设置里把**时区**改成 `Asia/Shanghai`，否则你看到的"今天"会按 UTC 切。

### 5. 前端配置

配置文件是 `webui-frontend/.env.production.local`。

⚠️ **这个文件在 `.gitignore` 里，`git pull` 不会带下来**，所以：

- 本地构建：自己 `cp .env.example .env.production.local` 再改；
- **服务器部署：必须手动在服务器上创建一次**（否则镜像里读不到，统计静默不生效）。

```bash
# 服务器上（路径按你的仓库位置）
cd /data/website/index-tts-webui/webui-frontend
cp .env.example .env.production.local   # 首次创建
vi .env.production.local                # 填 ID
```

```dotenv
VITE_UMAMI_SRC=https://stats.lizigege.com/script.js
VITE_UMAMI_ID=你在后台拿到的 UUID
VITE_UMAMI_DOMAINS=voice.lizigege.com
```

> **`.dockerignore` 已为此放行**：它原本有 `*.local`，会把这份文件挡在构建上下文外。
> 已加 `!.env.production.local` 例外 —— 否则 `docker build` 里 `npm run build`
> 读不到配置，统计静默不注入，而构建**照样成功**，属于最难查的一类。

然后**重新构建**（这些是构建期变量，重启容器没用）：

```bash
# 本地
cd webui-frontend && npm run build

# 服务器（前端有改动，不要加 --skip-frontend）
bash tools/deploy_g1_autodl.sh
```

`vite.config.ts` 的 `umamiPlugin` 会把它注入 `index.html`，
`scripts/prerender.mjs` 再按 `umami:start/end` 标记**复制到 `dist/landing.html`**。

> 为什么要在两处都注入：生产的 `/` 是 `dist/landing.html`，它的 head 是**手工拼**的，
> 不复用 `index.html`。只在 index.html 注入，落地页就没有统计——而落地页恰恰是流量入口。
> 这个坑在本项目已经出现过一次（og:image 只改了 index.html）。

### 6. 验证：怎么确认「已经关联上了」

分三层，**只有第三层能证明真的通了**。前两层只证明「代码里配了」。

#### 第一层：构建产物里有标签（本地，10 秒）

```bash
cd webui-frontend
grep -o 'data-website-id="[^"]*"' dist/index.html dist/landing.html
```

**两行都必须有输出。** 只有一行 ⇒ `prerender.mjs` 没复制到 `landing.html`，
落地页就没统计（而它是流量入口）。两行都没有 ⇒ 构建时没读到 env，见下表。

#### 第二层：线上 HTML 里有标签（部署后）

```bash
curl -s https://voice.lizigege.com/ | grep -o 'data-website-id="[^"]*"'
curl -s https://voice.lizigege.com/dubbing | grep -o 'data-website-id="[^"]*"'
curl -s https://stats.lizigege.com/script.js | head -c 80    # 必须是 JS，不能是 <!doctype html>
```

#### 第三层：浏览器实测（唯一可信的判据）

打开 `https://voice.lizigege.com`，按顺序看四个地方：

| 看哪里 | 期望 | 说明 |
|---|---|---|
| DevTools → Network → 过滤 `script.js` | 200，Type = script | 拿不到 tracker 就没下文 |
| DevTools → Network → 过滤 `api/send` | POST，200/204 | **这一条出现才叫"上报成功"** |
| Console 输入 `umami` 回车 | 返回对象（含 `track`） | 说明 tracker 已初始化 |
| umami 后台 →「实时」 | 5–10 秒内出现你这条访问 | 带路径与来源 |

#### 失败对照表（按现象查，别猜）

| 现象 | 最可能的原因 | 怎么处理 |
|---|---|---|
| 产物里没有 script 标签 | 构建时没读到 env | 文件名必须是 `webui-frontend/.env.production.local`（**不是** `.env`、不是仓库根目录）；改完要 `npm run build`，重启容器无效 |
| `script.js` 返回 `<!doctype html>` | 反代把请求回落成 SPA 了 | 统计必须用**独立子域**。挂在 `voice.lizigege.com/stats` 会被 `try_files ... /index.html` 吃掉 |
| script 200，但**没有** `api/send` | `data-domains` 与当前访问域名不匹配 | 这是最容易误判的一条，见下 |
| `api/send` 404 | 反代没转发 POST / 路径被改写 | 检查 `proxy_pass`，确认 `script.js` 与上报接口同源 |
| 什么都没发生，Network 干净 | 广告拦截器 | 用无痕窗口、关扩展再试 |

**`data-domains` 必读**：我们配的是 `VITE_UMAMI_DOMAINS=voice.lizigege.com`，
tracker 会拿它比对 `window.location.hostname`，**不匹配就静默不上报**。

所以：如果你 DNS 还没生效、先用 `media.lizigege.com` 或服务器 IP 打开页面测试，
会看到「标签在、script 也加载了、就是没有数据」—— 这不是坏了，
是域名没对上。验证时要么等 voice 生效，要么临时把测试域名补进去：

```dotenv
VITE_UMAMI_DOMAINS=voice.lizigege.com,media.lizigege.com
```

#### 验证来源归因（顺手做，1 分钟）

访问带 UTM 的地址，然后去 umami 后台看「来源」是否显示成你填的值：

```
https://voice.lizigege.com/?utm_source=test&utm_medium=manual&utm_campaign=verify
```

显示 `test / manual / verify` ⇒ 归因链路通了。
这一步现在不做，等推广开始后就没法回溯每篇内容的实际效果。


## 四、埋点规划（比装统计更重要）

装好 umami 只解决"有多少人来"，不解决"为什么没转化"。要回答后者，得按漏斗埋事件。

### 前端事件（umami，看行为）

用 `data-umami-event` 属性最省事，不需要写 JS：

```tsx
<button data-umami-event="cta_signup" data-umami-event-pos="hero">免费注册</button>
```

建议只埋这 6 个，多了会淹没在噪声里：

| 事件名 | 触发点 | 想回答的问题 |
|---|---|---|
| `cta_signup` | 落地页「免费注册 / 进入工作台」 | 哪个位置 CTA 有效 |
| `voice_preview` | 落地页试听音色 | 试听是不是决策点 |
| `case_open` | 展开「照着做」案例 | 教程区块有没有人看 |
| `gen_click` | 工作台「生成配音」 | 卡在编辑还是卡在信心 |
| `buy_click` | 积分商城「购买」 | 看到价格后走不走 |
| `docs_view` | 进入 `/docs` | 文档是助力还是避难所 |

⚠️ **umami 会被广告拦截器屏蔽**，所以下面这三个数**不能以它为准**。

### 服务端事件（自己记，看转化真相）

付费这种事必须由后端记账，否则永远不知道真实转化率：

| 指标 | 数据来源 | 为什么不能靠前端 |
|---|---|---|
| 注册成功 | `members` 表已有 `created_ts` | 前端上报会被拦、会被伪造 |
| 首次合成成功 | `queue` 任务表（status=done，且用户首次） | 同上 |
| 付费到账 | `orders` 表（已支付） | **直接关系到钱，只能以后端为准** |

漏斗就三条：**访问 → 注册 → 首次合成 → 付费**。
每一级的流失率决定下一步该改什么：

- 访问→注册 低 ⇒ 落地页文案/信任问题
- 注册→首次合成 低 ⇒ 上手门槛（这是本产品最可能断的一环）
- 首次合成→付费 低 ⇒ 免费额度给太多，或者没找到"下次还要用"的理由

## 五、运维

```bash
cd /data/umami
docker compose logs -f umami              # 看日志
docker compose exec umami-db pg_dump -U umami umami > backup-$(date +%F).sql   # 备份
docker compose pull && docker compose up -d   # 升级（升级前先备份）
```

- **备份频率**：统计丢了不致命，但历史渠道数据丢了就没法做同比。建议每周一次，和项目数据一起备份。
- **隐私**：umami 记录 IP（用于匿名去重）和 UA。如果将来对外正式运营，
  在页脚或隐私说明里提一句"使用自建统计、不使用 Cookie 追踪"就够了。
- **不要**把 umami 后台暴露在公网无鉴权状态：它记录了你所有渠道的数据。
