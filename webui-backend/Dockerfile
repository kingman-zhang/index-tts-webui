# podcast-webui 后端镜像：FastAPI + ffmpeg（音频变速/响度归一/转码）
# 构建上下文 = webui-backend/（data/、.env、logs/ 已被 .dockerignore 排除，全部走挂载卷）
# podcast-base:with-deps 是基于 python:3.11-slim作为基础镜像做出来的，只是把一些依赖安装好，避免下次部署时，又拉依赖，但如果依赖变了，需要重新构建这个镜像
FROM podcast-base:with-deps

WORKDIR /app

COPY app ./app
COPY server.py .

# .env 挂载到 /app/.env（config.py 按 Path(__file__).parents[1]/.env 寻址）
# data 卷挂载到 /app/data（DATA_DIR 默认 ./data，相对 WORKDIR）
EXPOSE 3001

CMD ["python", "server.py"]
