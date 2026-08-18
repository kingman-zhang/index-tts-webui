# AGENTS.md

## 项目定位

这是一个基于 IndexTTS2 的独立双人播客 WebUI 项目，目标是让用户在浏览器中配置两位角色、脚本、情感、语速和采样参数，并通过远程 GPU TTS 服务生成可试听、可下载的播客音频。

项目与 `index-tts-main/` 源码分离部署，不要把 WebUI 业务逻辑直接改进 IndexTTS 源码目录，除非明确是在修复上游兼容问题。

## 目录与组件

- `tts-server/`：GPU 服务器上的 FastAPI 服务，加载 IndexTTS2 模型，逐行合成并拼接 WAV。
- `webui-backend/`：无 GPU 的 FastAPI 网关，负责项目存储、队列、术语替换、TTS 请求代理和音频代理。
- `webui-frontend/`：React + TypeScript + Vite + Tailwind 前端。
- `../index-tts-main/`：上游 IndexTTS 源码，只作为模型运行时依赖和接口参考。

## 核心架构

```text
浏览器 / React 前端
        |
        v
WebUI 后端 FastAPI（通常 3001）
        |
        v
TTS 服务端 FastAPI（通常 8000，GPU）
        |
        v
IndexTTS2 + CUDA + 模型
```

音频交付必须优先通过 WebUI 后端代理，避免把 TTS 服务器地址暴露给浏览器。

## 关键约定

### 任务状态

TTS 服务端内部状态：

- `pending`
- `running`
- `completed`
- `failed`

WebUI 队列状态：

- `queued`
- `running`
- `success`
- `failed`
- `cancelled`

两层状态不要混用。WebUI 后端轮询 TTS 时必须兼容 TTS 的 `completed`，并转换为队列的 `success`。

### 进度值

- TTS 服务端：`0-100`。
- WebUI 队列和前端进度条：`0-1`。
- 跨层传递时必须显式转换，不能直接复制。

### 音频合成

双人播客采用：

```text
每行调用 IndexTTS2.infer()
 -> 每行得到独立 WAV
 -> 依据角色语速用 ffmpeg atempo 变速
 -> 按行间静音拼接
 -> 生成最终 WAV
```

`IndexTTS2.infer()` 当前没有连续语速参数，因此角色语速通过 `ffmpeg atempo` 实现。服务器需要安装 ffmpeg。

拼接前必须保证所有 WAV 的采样率、声道数和位深一致；最终文件使用标准 PCM WAV，并通过临时文件原子替换生成。

### 语速

- `params.speed`：全局语速，兼容旧项目和缺省配置。
- `params.speaker_speeds.A/B`：角色独立语速，优先级高于全局语速。
- 前端角色配置中的 `voices.A.speed` / `voices.B.speed` 会在提交时转换到 `speaker_speeds`。

### 情感

情感模式：

- `0`：跟随音色参考音频。
- `1`：独立情感参考音频。
- `2`：8 维情感向量。
- `3`：情感描述文本。

向量顺序：`happy, angry, sad, afraid, disgusted, melancholic, surprised, calm`。

情感预设只有在向量和权重完全匹配时高亮；手动改动向量或权重后应取消高亮。

## 启动与验证

### TTS 服务端

GPU 服务器上使用 IndexTTS venv：

```bash
/root/index-tts/.venv/bin/python /opt/tts-server/server.py \
  --indextts-home /root/index-tts \
  --model-dir /mnt/storage/index-tts-data/checkpoints \
  --voices-dir /mnt/storage/index-tts-data/voices \
  --output-dir /mnt/storage/index-tts-data/outputs \
  --device cuda:0 --fp16 --host 0.0.0.0 --port 8000
```

基本检查：

```bash
curl http://localhost:8000/api/health
curl http://localhost:8000/api/voices
```

### 本地 SSH 隧道

```bash
ssh -N -p 32363 -L 8000:127.0.0.1:8000 root@111.127.52.27
```

本机使用代理时，必须让 localhost 不经过代理：

```bash
export NO_PROXY="localhost,127.0.0.1,::1,*.local"
```

### WebUI 后端

```bash
cd webui-backend
bash start.sh
```

默认端口：`3001`。健康检查：

```bash
curl http://localhost:3001/api/config
```

### WebUI 前端

```bash
cd webui-frontend
npm run dev
```

前端默认端口：`5173`，开发代理指向 WebUI 后端。

## 常用检查命令

Python 语法：

```bash
python3 -m py_compile webui-backend/server.py tts-server/server.py tts-server/podcast_engine.py
```

前端构建和类型检查：

```bash
npm run build
npm run typecheck
```

检查 WAV 文件：

```bash
ffprobe podcast_xxx.wav
```

或：

```python
import wave
with wave.open("podcast_xxx.wav", "rb") as f:
    print(f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getnframes())
```

## 排查顺序

### 生成后一直显示“合成中”

1. 查询 TTS：`GET /api/task/{tts_task_id}`。
2. 确认 TTS 状态是否为 `completed`。
3. 确认 WebUI 后端是否把 `completed` 转为队列 `success`。
4. 确认队列任务是否保存了 `tts_task_id` 和 `audio_url`。
5. 确认前端 `QueuePanel` 是否识别 `success`。

### 文件已生成但无法播放

1. 用 `wave.open()` 或 `ffprobe` 检查 WAV 头。
2. 检查所有分段的采样率、声道、位深是否一致。
3. 检查 TTS `/api/task/{id}/audio` 是否返回 200。
4. 检查 WebUI `/api/podcast/audio/{id}` 的 MIME 是否为 `audio/wav`。
5. 检查浏览器是否收到完整的 `Content-Length`，以及播放器是否实际渲染 `<audio>`。

### 上传或删除音色异常

1. 区分预设音色和自定义音色。
2. 预设音色禁止改名和删除。
3. 自定义音色优先操作 TTS 服务端，TTS 不可用时才使用 WebUI 本地 fallback。
4. 浏览器录音通常是 `.webm`，上传和 MIME 映射必须支持该格式。

## 修改原则

- 优先修改现有组件和 API，不重复创建同类实现。
- 修改后至少运行对应的语法检查或前端构建。
- 涉及跨层字段时，同时检查前端类型、WebUI 后端模型、TTS 服务端模型和引擎数据类。
- 不要删除 `.workbuddy/`，其中保存项目工作记忆。
- 不要把密钥、服务器密码或个人凭据写入项目文档。
- 面向中国用户的金融类视觉默认使用“涨红跌绿”；当前项目暂未涉及金融行情展示。

## 强制会话记录与记忆固化

这是本项目的强制工作约定，不需要用户额外提醒：

1. 每次完成实质性开发、排障、方案决策或用户明确提供项目约定后，主动更新项目记录。
2. 先确认当天日志 `/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/.workbuddy/memory/YYYY-MM-DD.md` 存在；不存在时创建，然后只能追加，不能覆盖历史内容。
3. 稳定、跨会话仍然有效的项目事实，主动更新 `.workbuddy/memory/MEMORY.md`。
4. 当聊天上下文接近过长、完成一个阶段、修复复杂问题或形成新架构决定时，主动把重要结论同步到 `PROJECT_GUIDE.md` 或本文件。
5. 记录应包含：做了什么、为什么这样做、改动涉及哪些文件、验证结果、剩余风险和下一步；不要只记录“已完成”。
6. 不记录密码、Token、SSH 私钥或其他敏感凭据。
7. 记录完成后再向用户汇报，不要把“是否需要记录”交给用户确认。

项目记录分工：

- `AGENTS.md`：稳定的工作规则和不可遗漏的流程。
- `PROJECT_GUIDE.md`：面向人阅读的项目介绍和排障指南。
- `.workbuddy/memory/MEMORY.md`：精简的长期项目事实。
- `.workbuddy/memory/YYYY-MM-DD.md`：每日追加工作日志。

## 新会话接续

新会话开始时按以下顺序读取：

1. 本文件 `AGENTS.md`。
2. `README.md` 了解项目全貌。
3. `.workbuddy/memory/MEMORY.md` 了解长期架构约定。
4. 最近的 `.workbuddy/memory/YYYY-MM-DD.md` 了解最近改动。
5. 根据任务读取对应组件 README 和源码，不要一次性读取整个 `index-tts-main/`。

当前会话结束时，把可复用的架构决定、故障原因、验证结果追加到当天工作日志；只有稳定的长期约定才写入项目长期记忆。
