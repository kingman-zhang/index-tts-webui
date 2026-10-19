# 域名迁移：media → voice

> 状态：**代码侧已完成并验证**（2026-10-19）。DNS / 反向代理 / 平台后台由你在服务器侧执行。
> 目标域名：`voice.lizigege.com`（原 `media.lizigege.com`）

## 一、为什么换（先说清代价，再决定要不要换）

换子域不是零成本，代价有三个，都已可控但不该被忽略：

1. **搜索权重归零重来**。百度对"子域更换"不友好，旧域名已积累的收录不会自动转移，
   只能靠 301 慢慢传递，通常要 1–3 个月才有明显效果。
2. **旧链接全部失效**。`/docs`、已发出去的支付链接、别人收藏的地址，都指向旧域名。
   ⇒ 旧域名**必须保留 301 至少 6 个月**，不能直接停解析。
3. **支付回调依赖域名**。`PAY_PUBLIC_BASE_URL` 是拼回调地址的根，改域名时如果
   旧域名同时停解析，那**改之前已经生成、用户还没付的订单，付完钱回调进不来**，
   表现为「钱扣了、积分不到账」。

如果换域名的理由是"media 听起来像媒体站、voice 更贴产品"，那值得做；
如果只是觉得好看，建议**上线推广前一次性换完**——推广开始后再换，损失会被放大。

## 二、代码侧改了什么（已完成，你不用动）

| 文件 | 改了什么 | 为什么会漏 |
|---|---|---|
| `webui-frontend/src/lib/site.ts` | `SITE_URL` 字面量 | 唯一真源，canonical / og:url / JSON-LD / 预渲染都读它 |
| `webui-frontend/index.html` | og:image + 两段 JSON-LD 的 `url`（3 处） | 它**不 import** `SITE_URL`，是手写的 |
| `webui-frontend/public/robots.txt` | `Sitemap:` 行 | 纯静态文件，构建期不参与替换 |
| `webui-frontend/public/sitemap.xml` | 两个 `<loc>` + `lastmod` | 同上 |
| `outputs/verify-landing-page.js` | 改为**从 site.ts 读期望值** | 否则验收脚本自己变成第二个硬编码源，换域名就假失败 |
| 文档 4 份 | `命令大全` / `上线待办清单` / `PAY_MPAY_PLAN` / 落地页说明 | 不改正则照抄会配错 |

**新增护栏：`webui-frontend/scripts/check-site-url.mjs`**

已挂进 `npm run build`：扫描 `src/`、`public/`、`scripts/`、`index.html`，
把每一处 `*.lizigege.com` 与 `SITE_URL` 比对，不一致就**构建失败**并打印文件:行号。

```
✗ 域名不一致：1 处不指向 https://voice.lizigege.com
  public/robots.txt:25
    实际    https://media.lizigege.com
    这一行  Sitemap: https://media.lizigege.com/sitemap.xml
```

刻意**不跟着改**的域名（脚本里白名单登记，新增子域必须登记）：

- `mpay.lizigege.com` —— 支付网关自己的站点。顺手全局替换会**直接打不出二维码**。
- `stats.lizigege.com` —— 自建 umami 统计，与产品站分开部署。

## 三、你要做的四件事

### 1. DNS

```
voice.lizigege.com   A/AAAA → 与 media 同一台机器
media.lizigege.com   **保留不动**（301 要用它）
```

不要删 media 的解析。它现在是"旧地址入口"，作用是把旧链接和旧回调接住。

### 2. 证书

`voice.lizigege.com` 需要单独签发。如果你用的是泛域名证书（`*.lizigege.com`）则无需操作；
如果是单域名证书，用 acme.sh / certbot 补一张。

### 3. 反向代理：新增 voice，旧 media 做 301

```nginx
# ── 新站：正常服务 ───────────────────────────────
server {
    listen 443 ssl http2;
    server_name voice.lizigege.com;
    ssl_certificate     /path/to/voice.fullchain.pem;
    ssl_certificate_key /path/to/voice.key;

    location / {
        proxy_pass http://127.0.0.1:8080;   # ← 你现有的前端容器端口
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# ── 旧站：永久重定向（保留 ≥6 个月）───────────────
server {
    listen 443 ssl http2;
    server_name media.lizigege.com;
    ssl_certificate     /path/to/media.fullchain.pem;
    ssl_certificate_key /path/to/media.key;

    # ⚠️ 例外：支付回调不能 301。
    #    部分支付网关不跟随重定向，会把 301 当成失败 ⇒ 钱到了、记账没到。
    location /api/pay/notify/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        return 301 https://voice.lizigege.com$request_uri;
    }
}
```

> **为什么 `/api/pay/notify/` 要单独放行**：这是本次迁移唯一"改错了会丢钱"的地方。
> 301 对浏览器是透明的，对服务端 HTTP 客户端不一定是——默认行为取决于实现。
> 保留旧域名上的回调直通，等于给改配置期间的 pending 订单留了一条活路。

### 4. 后端 `.env` 与支付后台

```bash
# webui-backend/.env
PAY_PUBLIC_BASE_URL=https://voice.lizigege.com   # ⚠️ 只填根地址，不要带 /api/pay/notify/...
```

改完**重启 backend**（`.env` 是 import 期读的，热改无效）。

⚠️ 这里有个已知的静默失败：`PAY_PUBLIC_BASE_URL` 如果填成完整回调 URL，
会拼成 `.../notify/mpay/api/pay/notify/mpay`，而 **mpay 不校验 notify_url、照样返回成功**
⇒ 二维码正常出、用户付了钱、积分永远不到。代码现在会拦住（出码 503 并说清重复在哪段）。

同时检查支付网关后台：如果里面配过"站点地址/异步通知地址"，一并改成 voice。

### 5. 百度 / Google 搜索资源平台

1. **新增站点** `voice.lizigege.com`，完成验证（HTML 标签或文件验证）。
2. 提交 `https://voice.lizigege.com/sitemap.xml`。
3. 旧站点 `media.lizigege.com` 保持验证状态，观察 301 是否被识别。
4. 百度若提供"站点改版/域名更换"工具，按它的流程提交（能显著加快权重转移）；
   没有的话就靠 301 + sitemap 双管。

## 四、验证清单（部署后逐条过）

```bash
# 1. 新域名能开、且是静态预渲染（不执行 JS 就有正文）
curl -s https://voice.lizigege.com/ | grep -c 最佳实践        # > 0

# 2. 域名在四处都一致
curl -s https://voice.lizigege.com/ | grep -o 'rel="canonical" href="[^"]*"'
curl -s https://voice.lizigege.com/robots.txt | grep Sitemap
curl -s https://voice.lizigege.com/sitemap.xml | grep -o '<loc>[^<]*</loc>'

# 3. 旧域名 301 到新域名（-I 只看头）
curl -sI https://media.lizigege.com/docs | head -3                # 期望 301 + Location: voice...

# 4. 支付回调仍通（旧域名路径不被 301）
curl -sI https://media.lizigege.com/api/pay/notify/mpay | head -3  # 期望不是 301

# 5. 后端跑的是新配置
curl -s https://voice.lizigege.com/api/version | head -20          # 看 process_started_at 是否刚重启
```

## 五、回滚

代码侧回滚一次 commit 即可（`SITE_URL` + 三个静态文件）。
运维侧回滚 = 把 nginx 的 `server_name` 改回 media、`.env` 的 `PAY_PUBLIC_BASE_URL` 改回 media、重启 backend。

**建议**：DNS 先只加 voice（media 不动），nginx 两套并存跑一周；
确认支付、统计、收录都正常后再考虑 media 是否继续保留 301。
