# podcast-webui 前端镜像：vite 生产构建 + nginx 静态伺服（替代开发环境跑 dev server）
# 构建上下文 = webui-frontend/
FROM node:20-alpine AS build

WORKDIR /build

# 依赖层缓存
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ─── 运行层 ───
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80
