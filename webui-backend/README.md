# WebUI 后端 (webui-backend)

前端的 API 网关，部署在 WebUI 服务器上（无需 GPU）。

## 功能

- 转发合成请求到 TTS 服务端
- SSE 推送合成进度给前端
- 代理音频文件下载（隐藏 TTS 服务器地址）
- 保存/加载播客项目（JSON 文件存储）
- 代理参考音频上传/列表

## 部署

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动（--tts-url 指向 TTS 服务端地址）
python server.py --tts-url http://gpu-server:8000 --host 0.0.0.0 --port 3001
```

或用环境变量：

```bash
export TTS_URL=http://gpu-server:8000
export HOST=0.0.0.0
export PORT=3001
python server.py
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config` | 后端配置与 TTS 服务在线状态 |
| GET | `/api/tts/health` | 代理 TTS 健康检查 |
| GET | `/api/voices` | 列出参考音频 |
| POST | `/api/voices/upload` | 上传参考音频 |
| POST | `/api/synthesize` | 单段合成（快速试听） |
| POST | `/api/podcast/generate` | 提交双人播客合成任务 |
| GET | `/api/podcast/status/{task_id}` | SSE 推送合成进度 |
| GET | `/api/podcast/audio/{task_id}` | 代理下载音频 |
| GET | `/api/audio/{filename}` | 按文件名代理下载音频 |
| GET | `/api/tasks` | 列出最近任务 |
| GET | `/api/projects` | 列出保存的项目 |
| POST | `/api/projects` | 保存项目 |
| GET | `/api/projects/{id}` | 加载项目 |
| PUT | `/api/projects/{id}` | 更新项目 |
| DELETE | `/api/projects/{id}` | 删除项目 |
| POST | `/api/projects/import` | 从纯文本导入对话脚本 |

## 项目数据

项目保存在 `data/projects/` 目录下，每个项目一个 JSON 文件，包含角色配置、对话脚本、静音设置、生成参数。
