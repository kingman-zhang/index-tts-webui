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
# 升级（数据卷不受影响）
git pull && docker compose up -d --build

# 日志
docker compose logs -f backend    # 后端
docker compose logs -f web        # nginx

# 改 .env 后
docker compose restart backend

# 回滚上一版镜像（数据不动）
docker tag podcast-backend:latest podcast-backend:backup   # 每次 build 前先打备份 tag
```

## 4. 与旧脚本部署的关系

`tools/deploy_g1_autodl.sh`（nohup + dev server 方式）保留可回退，但两者**不能同时跑**：会抢 3001 端口。切 Docker 前先停旧进程（`pkill -f webui-backend/server.py`；dev server 按 Ctrl-C）。数据目录是同一份（`webui-backend/data`），两种方式互通。

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
