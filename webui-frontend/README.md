# WebUI 前端 (webui-frontend)

双人播客工作室的前端界面，基于 React + TypeScript + Tailwind CSS。

## 功能

- 双主持人角色配置（音色参考音频上传/选择/试听）
- 可视化对话脚本编辑器（拖拽排序、批量导入、逐行情感控制）
- 4 种情感控制方式（跟随音色/参考音频/8维向量/文本描述）
- 情感预设快捷按钮（平静/轻松/兴奋/严肃/悲伤/惊讶）
- 全局生成参数（静音节奏 + GPT2 采样参数）
- 实时 SSE 进度展示 + 音频试听下载
- 项目保存/加载/删除

## 开发

```bash
# 安装依赖
npm install

# 启动开发服务器（默认 5173 端口，自动代理 /api 到 localhost:3001）
npm run dev

# 构建生产版本
npm run build

# 预览生产构建
npm run preview
```

## 配置

开发模式下，Vite 会自动将 `/api` 请求代理到 `http://localhost:3001`（WebUI 后端）。
如需修改，编辑 `vite.config.ts` 中的 `server.proxy` 配置。

生产部署时，需通过 Nginx 等反向代理将 `/api` 转发到 WebUI 后端。

## 技术栈

- React 18 + TypeScript
- Vite 5（构建工具）
- Tailwind CSS 3（样式）
- Lucide React（图标）
- 原生 EventSource（SSE 进度推送）

## 部署

```bash
npm run build
# 将 dist/ 目录部署到 Web 服务器，配合反向代理
```

### Nginx 配置示例

```nginx
server {
    listen 80;
    server_name podcast.example.com;

    # 前端静态文件
    location / {
        root /path/to/webui-frontend/dist;
        try_files $uri /index.html;
    }

    # API 代理到 WebUI 后端
    location /api/ {
        proxy_pass http://127.0.0.1:3001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # SSE 支持
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```
