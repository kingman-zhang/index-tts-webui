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

# 启动
python server.py --host 0.0.0.0 --port 3001
```

## TTS 资源池配置（三类服务一种写法）

合成走「资源池」，池里每条资源都是同一件事：自建 tts-server（`provider=local`）、
302.ai、SiliconFlow、autodl.art。**加/减一台服务器 = 加/删一条资源**。

推荐用 YAML 文件（改完不用重启，热加载；**能写注释** ⇒ 想停用一台就整条 `#` 掉）：

```bash
cp tts-resources.example.yaml data/config/tts-resources.yaml   # 按需增删条目
```

```bash
# .env 里只加这一行（相对路径按 backend 根解析，容器里 /app/data 就是挂载卷）
TTS_RESOURCES_FILE=data/config/tts-resources.yaml
```

密钥不写进配置，配置里只写 `api_key_env`（环境变量名），值仍在 `.env`。
**扩展名必须是 `.yaml`/`.yml`** —— 从旧的 `.json` 迁过来 `mv` 一下即可（内容不用动）。
字段说明见 `.env.example` 顶部与 `ENGINES.md`。

也支持内联（改完要重启）：`TTS_RESOURCES=[{id: gpu-a, provider: local, base_url: "http://host-a:8000"}]`。

**`TTS_URL` 一般不用填**：未配置时自动取池里第一个 `local` 的地址。它只服务
**音色管理面**（预设音色上传、音色库增删改/试听、`/api/tts/health` 探针）与旧播客端点，
**合成不读它**。多台时其余各台缺的音色由 `voice_sync` 在合成前按需补传。

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
