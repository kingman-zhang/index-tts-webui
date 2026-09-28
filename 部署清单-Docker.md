# Docker Compose 部署清单（生产）

> 架构：`web`（nginx，静态前端 + /api 反代）→ `backend`（FastAPI + ffmpeg，仅内网）
> 数据零进镜像：`data/`、`.env`、`logs/` 全部挂载卷；升级 = 重建镜像，数据不动。

## 0. 前置（服务器上一次性）

- Docker + docker compose v2（`docker compose version` 能跑通）
- 仓库克隆到 `~/index-tts-webui`（与旧脚本路径一致）：
  ```bash
  git clone https://github.com/kingman-zhang/index-tts-webui.git ~/index-tts-webui
  cd ~/index-tts-webui && git checkout main
  ```

## 1. 数据与配置落位（仅首次 / breezeblue 更新时）

```bash
# ① 音色库数据（165MB，gitignore 拿不到，必须 rsync）
rsync -avz --progress webui-backend/data/ <server>:~/index-tts-webui/webui-backend/data/
# ② 配置
cp webui-backend/.env.example webui-backend/.env
vim webui-backend/.env
```

`.env` 必查项（与本地开发环境的差异）：

| 项 | 要求 |
|---|---|
| `MEMBER_ADMIN_TOKEN` | **必须换强随机**（本地是 local-admin-token，绝不能上生产） |
| `TTS_ENGINE_PREFERRED` | `indextts_302ai`（云端引擎，无需本地 tts-server/GPU） |
| `TTS_STATUS_POLL` | `0`（服务器无本地 TTS，探测无意义） |
| `MEMBER_ENFORCE` / `MEMBER_REQUIRE_LOGIN` | 生产按商业化开关决定 |
| `TTS_CONCURRENCY` / `USER_CONCURRENCY` | 默认即可（302.ai 账号级串行，调大无收益） |
| `PODCAST_WEB_PORT` | 对外端口，默认 8088（可加进 .env 覆盖） |

## 2. 启动

```bash
docker compose up -d --build
docker compose ps                 # 两个容器 healthy/running
curl -s http://127.0.0.1:8088/api/health        # 后端自身探针（容器 healthcheck 用的就是这个）
curl -s http://127.0.0.1:8088/api/breezeblue/voices?page_size=1 | head -c 200  # 音色库数据在
```

浏览器验证：`http://<server-ip>:8088`（有域名/反代的话，宿主 nginx 把 80/443 转到 127.0.0.1:8088 即可，`/api` 无需特殊处理——容器内 nginx 已统一反代）。

## 3. 日常运维

```bash
# 升级（数据卷不受影响）—— 推荐用脚本：它会重建镜像、等 healthy、再做部署后自检
bash tools/deploy_g1_autodl.sh

# 等价的手工方式（脚本的 Docker 路径内部就是这两步）
git pull && docker compose up -d --build

# ⚠️ Docker 部署下不要随手加 --skip-frontend：前端也是镜像（podcast-web）。
#    只有确定前端无改动时才用，否则页面还是旧构建、后端却已更新。
```

> 判定「这次要不要重前端」：`git log --name-only <旧HEAD>..<新HEAD> | grep webui-frontend`。
> 有输出 ⇒ 必须 `docker compose up -d --build`（两个服务），不能只重建 backend。

```bash
# 日志
docker compose logs -f backend    # 后端
docker compose logs -f web        # nginx

# 改 .env 后
docker compose restart backend

# 回滚上一版镜像（数据不动）
docker tag podcast-backend:latest podcast-backend:backup   # 每次 build 前先打备份 tag
```

## 4. 与裸进程部署的关系

`tools/deploy_g1_autodl.sh` **会自动识别部署形态**：检测到容器 `podcast-backend` 在跑就走
Docker 路径（`git pull` → `docker compose up -d --build` → 自检），否则走裸进程路径
（nohup 重启 `server.py`）。两条路的「生效」条件不同，走错一条就是「脚本跑完了、代码一行没变」。

两种形态**不能同时跑**（会抢 3001 端口）。

切 Docker 前先停旧进程——**按端口找，别按命令行字符串匹配**：

```bash
lsof -ti tcp:3001 | xargs -r kill        # 正确：启动方式不同，cmdline 不一样
# pkill -f webui-backend/server.py       # 错误：脚本是 `cd webui-backend && python3 server.py`，
#                                        #       cmdline 里没有 "webui-backend/" 前缀，匹配不到 ⇒ 漏杀
```

数据目录是同一份（`webui-backend/data`），两种方式互通。

**升级后必须核对「进程跑的是哪份代码」**（`git pull` 只改磁盘，不重启等于没改）：

```bash
# 裸进程：直接比 HEAD
curl -s localhost:3001/api/version | python3 -c 'import json,sys;print(json.load(sys.stdin)["git_head"])'
git rev-parse --short HEAD                  # 两个必须一致

# 容器：镜像里没有 .git，git_head 恒为 null，不能据此判断「没部署成功」。
# 改比「镜像构建时间 vs HEAD 提交时间」——容器一定在镜像之后启动，比 StartedAt 没用：
# 只重建容器不重建镜像时 StartedAt 也会变新，会假通过。
python3 - <<'PY'
import subprocess, re, datetime as d
img = subprocess.run(["docker","inspect","-f","{{.Created}}","podcast-backend:latest"],
                     capture_output=True, text=True).stdout.strip()
head = subprocess.run(["git","log","-1","--format=%cI"], capture_output=True, text=True).stdout.strip()
f = lambda s: int(d.datetime.fromisoformat(re.sub(r"\.\d+", "", s).replace("Z","+00:00")).timestamp())
print(f"镜像构建 {img}  vs  HEAD 提交 {head}")
print("✓ 镜像是新的" if f(img) >= f(head) else "✗ 镜像比 HEAD 旧 —— 重建没生效，仍是旧代码")
PY
```

`docker exec podcast-backend python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:3001/api/version').read().decode())"`
还能拿到容器内的 `/api/version`（backend 不对外暴露，只能从容器内探）。判据三条：
① 镜像构建时间 ≥ HEAD 提交时间；② 该端点返回 200 且含 `text_pipeline`（旧镜像是 404，端点本身
就是新代码才有的符号）；③ `glossary_terms` > 0 且 `glossary_exists` 为真。

`/api/version` 还会回 `stale_sources`：裸进程部署下非空表示这些源文件在进程启动之后
才被改动，即**进程里仍是旧代码**，必须重启。另回 `data_dir` / `glossary_exists` /
`name_punct_enabled` 等开关，可用来一次性排除「数据目录指错」和「.env 开关没生效」。

## 5. 基础镜像工作流（podcast-base:with-deps）

backend 的 Dockerfile 已改为 `FROM podcast-base:with-deps`（自建基础镜像，装好 ffmpeg + 全部 pip 依赖，避免每次部署重拉）。**基础镜像必须满足**：

```bash
# 构建/更新基础镜像（依赖变了才需要重做）
docker build -t podcast-base:with-deps - <<'EOF'
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
      "fastapi>=0.110.0" "uvicorn[standard]>=0.29.0" "httpx>=0.27.0" \
      "pydantic>=2.6.0" "python-multipart>=0.0.9" "python-docx>=1.1.0" "pypdf>=4.0.0"
EOF

# 自检三项（缺一 backend 起不来/判 unhealthy）
docker run --rm podcast-base:with-deps sh -c \
  "which curl; ffmpeg -version | head -1; python -c 'import fastapi, uvicorn, httpx; print(\"deps OK\")'"
```

| 要求 | 原因 |
|---|---|
| `fastapi/uvicorn/httpx/pydantic/python-multipart/python-docx/pypdf` 齐全 | 业务 Dockerfile 不再装依赖，缺哪个启动即 ModuleNotFoundError |
| `ffmpeg` | 播客段级变速、响度归一、art 通道 mp3 转码 |
| `curl`（或用新版 compose 的 python healthcheck） | healthcheck 探活；新 compose 已改为 python urllib，不依赖 curl |

## 6. 构建说明

- backend 镜像：`python:3.11-slim` + ffmpeg + requirements.txt，依赖层缓存（改代码不重装依赖）；pip 主源失败自动落清华镜像
- web 镜像：node:20 `npm ci && vite build`（产物 ~340KB，gzip 99KB）→ nginx:alpine
- 前端从 dev server 变为**生产构建 + nginx 静态伺服**，SPA 路由（/dubbing、/account）已配回退，上传体积上限 200MB
