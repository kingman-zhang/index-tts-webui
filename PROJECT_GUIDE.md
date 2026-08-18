# 双人播客 WebUI 项目说明与排障指南

> 本文是本项目的长期介绍文档，记录项目目标、架构、功能演进、部署方式、数据流、关键设计决策和常见问题。代码实现以当前源码为准，本文用于帮助后续开发和排障，不替代接口文档。

## 1. 项目目标

本项目基于 IndexTTS2，开发一个独立部署、面向商业化使用的双人播客生成 WebUI。

用户不再需要登录 GPU 服务器、手工修改 JSONL、切换参考音频或调整 IndexTTS 参数，而是直接在浏览器中完成：

- 女主持人与男嘉宾等双角色配置。
- 参考音色选择、上传、录音、试听、重命名和删除。
- 角色默认情感设置。
- 每行对话的独立情感设置。
- 8 维情感向量和情感预设。
- 全局语速与角色独立语速。
- 静音节奏和 GPT2 采样参数。
- JSONL / 文本脚本导入与编辑。
- 可视化脚本与 JSONL 代码双视图，双方共享同一份脚本数据，可互相切换并同步修改。
- 术语替换。
- 异步任务队列、进度、试听和下载。
- 项目保存、加载和角色预设管理。

## 2. 三层架构

```text
┌─────────────────┐
│ 浏览器 React SPA │
│ 端口 5173        │
└────────┬────────┘
         │ /api
┌────────▼────────┐
│ WebUI Backend    │
│ FastAPI，端口3001 │
│ 项目/队列/代理    │
└────────┬────────┘
         │ HTTP，可经 SSH 隧道
┌────────▼────────┐
│ TTS Server       │
│ FastAPI，端口8000 │
│ GPU + IndexTTS2   │
└────────┬────────┘
         │
┌────────▼────────┐
│ IndexTTS2 模型    │
└─────────────────┘
```

### 部署边界

- `tts-server/` 与 IndexTTS 源码和模型在 GPU 服务器上运行。
- `webui-backend/` 可以在另一台普通服务器运行。
- `webui-frontend/` 可独立静态部署。
- 浏览器只访问 WebUI 后端，不直接访问 TTS 服务端。
- WebUI 后端代理音频，隐藏 GPU 服务器地址。

## 3. 目录说明

```text
podcast-webui/
├── AGENTS.md             # 新会话和开发代理的工作约定
├── PROJECT_GUIDE.md      # 本项目介绍、演进和排障指南
├── README.md             # 快速开始和基础功能说明
├── tts-server/
│   ├── server.py         # TTS HTTP API、任务管理、模型调用
│   ├── podcast_engine.py # 逐行生成、语速处理、WAV 拼接
│   └── README.md         # TTS 部署和 API
├── webui-backend/
│   ├── server.py         # API 网关、项目、队列、音频代理
│   └── README.md         # 后端部署和 API
└── webui-frontend/
    ├── src/App.tsx       # 应用状态与页面布局
    ├── src/api/client.ts # 前端 API 客户端
    ├── src/types/        # 前端数据类型与默认项目
    └── src/components/   # 角色、脚本、情感、参数、队列等组件
```

上游源码位于项目外层：

```text
../index-tts-main/
```

不要把 WebUI 业务功能直接写入上游源码目录。

## 4. 核心生成流程

```text
用户配置角色和脚本
        ↓
前端 submitToQueue
        ↓
WebUI 后端创建 queued 任务
        ↓
WebUI 后端提交 /api/podcast
        ↓
TTS 服务端创建 pending 任务
        ↓
后台线程获得 GPU 锁，进入 running
        ↓
逐行调用 IndexTTS2.infer()
        ↓
每行生成临时 WAV
        ↓
按角色速度调用 ffmpeg atempo
        ↓
根据行间关系插入静音
        ↓
原子生成最终 WAV
        ↓
TTS 任务 completed
        ↓
WebUI 队列转换为 success
        ↓
前端显示播放和下载
```

### 为什么不是直接调用 `indextts2 batch --concat`

最初的实验通过 JSONL 和 CLI 完成。WebUI 版本没有在服务器上拼接命令字符串，而是直接调用 IndexTTS2 Python API：

```python
tts.infer(**infer_kwargs)
```

这样可以逐行注入角色、情感、语速和采样参数，并且能实时报告进度。最终效果与批量拼接逻辑一致，但更适合服务化。

## 5. 任务状态模型

这是项目中非常重要的跨层约定。

### TTS 服务端

```text
pending → running → completed
                    ↘ failed
```

### WebUI 队列

```text
queued → running → success
                  ↘ failed
                  ↘ cancelled
```

TTS 服务端的完成状态不是 `success`，而是 `completed`。WebUI 后端轮询必须将：

```python
status in ("success", "completed")
```

转换为：

```python
task["status"] = "success"
```

如果再次出现“文件已生成但界面一直合成中”，优先检查这里。

### 进度约定

- TTS：整数百分比 `0-100`。
- WebUI 队列：小数 `0-1`。
- 前端显示时乘以 100。

## 6. 音频与 WAV 设计

### 分段策略

每行脚本单独生成一段音频：

```text
line 1 → 0001.wav
line 2 → 0002.wav
...
line N → 00NN.wav
```

每段生成完成后，根据说话人选择语速：

```python
speed = speaker_speeds.get(line.speaker, request.params.speed)
```

语速不是 IndexTTS2 的原生 infer 参数，而是通过 ffmpeg：

```bash
ffmpeg -i input.wav -filter:a atempo=0.85 -ar 24000 output.wav
```

### 拼接策略

- 同一说话人连续行：使用 `between_lines` 静音。
- 说话人切换：使用 `speaker_switch` 静音。
- 最后一行不添加尾部静音。
- 拼接前检查所有 WAV 的采样率、通道数、位深。
- 使用 `.tmp` 临时文件写出，成功后 `os.replace()` 原子替换。

### 无法播放时的检查

```bash
ffprobe podcast_xxx.wav
```

或者：

```python
import wave
with wave.open("podcast_xxx.wav", "rb") as f:
    print("sample rate:", f.getframerate())
    print("channels:", f.getnchannels())
    print("sample width:", f.getsampwidth())
    print("frames:", f.getnframes())
```

还要检查：

1. TTS `/api/task/{id}/audio` 是否 200。
2. WebUI `/api/podcast/audio/{id}` 是否 200。
3. Content-Type 是否为 `audio/wav`。
4. Content-Length 是否等于实际响应字节数。
5. 前端是否真的渲染了 `<audio controls>`，而非只改变 React 状态。

## 7. 角色、音色和预设

### 音色来源

- TTS 服务器的自定义音色目录：`VOICES_DIR`。
- WebUI 本地音色目录：`webui-backend/data/voices/`，用于 fallback。
- WebUI 预设音色目录：`webui-backend/data/preset-voices/`。
- 预设音色通过 `manifest.json` 分为女声、男声、情感参考。

预设音色：

- 可以选择和试听。
- 不允许直接重命名。
- 不允许删除。

自定义音色：

- 可以上传。
- 可以使用浏览器麦克风录制。
- 可以自定义名称。
- 可以自动选中。
- 可以试听、重命名和删除。

录音通常为 `.webm`，上传、服务端白名单和 MIME 映射必须支持 `.webm`。

### 角色预设与音频资源的区别

这是两个独立概念：

- 音频资源：实际参考音频文件，支持 CRUD。
- 角色预设：保存角色名称、所选音色、角色语速和默认情感，支持保存、加载、重命名和删除。

角色预设保存语速时使用 `role_speed`，加载时兼容旧数据：

```typescript
p.role_speed ?? p.speed ?? 1.0
```

## 8. 情感控制

每行可以有自己的情感配置，角色默认情感用于新增或导入行的初始值。

```text
mode=0：跟随音色
mode=1：情感参考音频
mode=2：8维向量
mode=3：情感文本
```

8 维向量：

```text
happy, angry, sad, afraid,
disgusted, melancholic, surprised, calm
```

情感预设高亮条件：向量每一维和 weight 都匹配，误差小于 0.001。用户手动修改向量或权重后，原标签取消高亮。

## 9. 已完成的功能演进

### 基础版本

- 三层分离架构。
- TTS 单段生成接口。
- 双人逐行合成和 WAV 拼接。
- 项目保存、加载和删除。
- JSONL / 文本导入。
- 角色 A/B 配置。

### 音色与角色

- 预设音色库及分类。
- 音色搜索、筛选、试听。
- 上传和自定义命名。
- 麦克风录音。
- 自定义音色 CRUD。
- 角色预设保存、加载、重命名、删除。
- 角色独立语速。

### 生成与辅助能力

- 全局语速。
- 情感预设高亮。
- 术语词汇表及展开/收起。
- 任务队列。
- 实时进度。
- 在线播放和下载。
- WAV 格式一致性检查。

### 最近修复

#### 生成按钮不可点击

原因是前端把 `ttsOnline` 作为按钮启用条件，而健康检查和 SSH 隧道状态可能晚于页面初始化。现在只要求：

- A 有音色。
- B 有音色。
- 至少一行非空文本。

TTS 不可用时在实际提交阶段提示错误。

#### 生成完成后一直显示“合成中”

原因是 TTS 返回 `completed`，WebUI 队列只判断 `success`。现在已兼容并转换。

#### 文件已生成但无法播放

已增加：

- WAV 分段格式一致性校验。
- 标准 WAV 头检查。
- 音频代理的 `Content-Length`、`Accept-Ranges`。
- `inline` 播放响应。
- 队列面板真实渲染 `<audio>` 播放器。

## 10. 部署和验证

### GPU TTS 服务

```bash
/root/index-tts/.venv/bin/pip install fastapi uvicorn[standard] pydantic python-multipart
# 角色独立语速还需要 ffmpeg
sudo apt install ffmpeg

/root/index-tts/.venv/bin/python /opt/tts-server/server.py \
  --indextts-home /root/index-tts \
  --model-dir /mnt/storage/index-tts-data/checkpoints \
  --voices-dir /mnt/storage/index-tts-data/voices \
  --output-dir /mnt/storage/index-tts-data/outputs \
  --device cuda:0 --fp16 --host 0.0.0.0 --port 8000
```

### WebUI 后端

```bash
cd webui-backend
bash start.sh
```

### WebUI 前端

```bash
cd webui-frontend
npm install
npm run dev
```

### 验证命令

```bash
curl http://localhost:8000/api/health
curl http://localhost:3001/api/config
curl http://localhost:3001/api/voices
```

项目代码验证：

```bash
python3 -m py_compile \
  webui-backend/server.py \
  tts-server/server.py \
  tts-server/podcast_engine.py

cd webui-frontend
npm run build
npm run typecheck
```

## 11. 常见问题速查

| 现象 | 优先检查 |
|---|---|
| 本地 curl localhost:8000 无响应 | SSH 隧道、`NO_PROXY`、端口监听 |
| 上传返回 500 | multipart 的 `name` 是否使用 Form，服务端依赖是否完整 |
| 预设音色试听 404 | WebUI 后端是否查找 `preset-voices` 和 `emotions` |
| 预设音色出现编辑/删除 | 前端是否根据 source 区分 preset/custom |
| 自定义音色删除 404 | TTS 删除代理和本地 fallback |
| 术语面板点击无反应 | CardHeader 是否透传 HTML 属性 |
| 角色语速不生效 | 前端 `speaker_speeds`、TTS Pydantic 模型、ffmpeg |
| 角色预设没有语速 | 保存 `role_speed`，加载兼容 `role_speed/speed` |
| 情感标签不高亮 | vector、weight 是否完全匹配 |
| 生成按钮不可点击 | A/B 音色和至少一行非空文案 |
| 生成后一直合成中 | `completed` 是否转换成 WebUI `success` |
| WAV 无法播放 | WAV 头、片段格式、代理 MIME、播放器元素 |

## 12. 新会话接续建议

新会话不需要依赖当前超长聊天记录。建议在新会话第一条消息中说明：

> 请继续开发 `/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/podcast-webui/`。先读取项目根目录的 `AGENTS.md`、`PROJECT_GUIDE.md`、`README.md`，再读取 `.workbuddy/memory/MEMORY.md` 和最近工作日志。当前项目是基于 IndexTTS2 的三层分离双人播客 WebUI。不要重新搭建已有功能，先检查当前源码和最近日志，再处理我提出的问题。

随后补充具体问题、复现步骤、请求 URL、服务端日志或截图即可。

## 13. 记录策略

- `AGENTS.md`：稳定的开发规则、架构边界、状态约定和排障顺序。
- `PROJECT_GUIDE.md`：项目介绍、完整演进、数据流和常见问题。
- `.workbuddy/memory/MEMORY.md`：短小的长期项目事实，控制在约 3000 字符内。
- `.workbuddy/memory/YYYY-MM-DD.md`：按日期追加当天完成的工作、原因、验证结果。
- 不把密码、Token、SSH 私钥或其他秘密写入任何记录。

代码始终是最终事实来源；当文档与代码不一致时，应先检查代码，再更新文档。
