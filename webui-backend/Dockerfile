# podcast-webui 后端镜像：FastAPI + ffmpeg（音频变速/响度归一/转码）
# 构建上下文 = webui-backend/（data/、.env、logs/ 已被 .dockerignore 排除，全部走挂载卷）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg：podcast_runner 变速 + 响度归一（-16 LUFS）+ wav 头修复都依赖它
# curl：healthcheck 用
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层单独缓存（代码变更不触发重装）
COPY requirements.txt .
RUN pip install -r requirements.txt \
    || pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app ./app
COPY server.py .

# .env 挂载到 /app/.env（config.py 按 Path(__file__).parents[1]/.env 寻址）
# data 卷挂载到 /app/data（DATA_DIR 默认 ./data，相对 WORKDIR）
EXPOSE 3001

CMD ["python", "server.py"]
