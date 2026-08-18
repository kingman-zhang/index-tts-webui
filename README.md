# 双人播客工作室 (Podcast WebUI)

基于 [IndexTTS2](https://github.com/index-tts/index-tts) 的双人播客可视化生成平台。
与 index-tts 源码完全分离部署，通过 HTTP API 通信，适合商业化场景。

## 架构

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────────┐
│   浏览器     │ <─> │  WebUI 服务器      │ <─> │   TTS 服务器     │
│  (前端 SPA)  │     │  (后端 API 网关)    │     │  (IndexTTS2)    │
└─────────────┘      └──────────────────┘      └─────────────────┘
                     无需 GPU                    需要 GPU + 模型
```

三个组件可独立部署到不同服务器：

| 组件 | 目录 | 部署位置 | 硬件需求 |
|------|------|---------|---------|
| TTS 服务端 | `tts-server/` | GPU 服务器（与 index-tts 同机） | NVIDIA GPU |
| WebUI 后端 | `webui-backend/` | 任意服务器 | 无特殊需求 |
| WebUI 前端 | `webui-frontend/` | 静态托管 / WebUI 服务器 | 无 |

## 快速开始

### 1. TTS 服务端（GPU 服务器）

```bash
# 在 index-tts 的 venv 中安装额外依赖
/root/index-tts/.venv/bin/pip install fastapi uvicorn[standard] pydantic python-multipart

# 启动（指向 index-tts 源码和模型目录）
/root/index-tts/.venv/bin/python /path/to/tts-server/server.py \
  --indextts-home /root/index-tts \
  --model-dir /mnt/storage/index-tts-data/checkpoints \
  --voices-dir /mnt/storage/index-tts-data/voices \
  --output-dir /mnt/storage/index-tts-data/outputs \
  --device cuda:0 --fp16 --host 0.0.0.0 --port 8000
```

### 2. WebUI 后端

```bash
cd webui-backend
pip install -r requirements.txt

# 启动（--tts-url 指向 TTS 服务端）
python server.py --tts-url http://gpu-server:8000 --host 0.0.0.0 --port 3001
```

### 3. WebUI 前端

```bash
cd webui-frontend
npm install
npm run dev    # 开发模式，默认 http://localhost:5173
# 或 npm run build 后用 Nginx 托管 dist/
```

开发模式下，前端自动将 `/api` 代理到 `localhost:3001`（WebUI 后端）。

## 功能特性

### 角色与音色
- 双主持人配置，各自独立设置音色参考音频
- 支持上传新参考音频或从已有列表选择
- 参考音频试听

### 对话脚本编辑
- 可视化对话列表，逐行编辑
- 说话人一键切换（A/B 带颜色标识）
- 拖拽排序、复制、删除、上下移动
- 批量导入（解析 `A: 文本` / `B: 文本` 格式）
- 实时统计行数与字数

### 情感控制（每行独立）
4 种方式可选：
- **跟随音色**：使用参考音频自带的情感
- **参考音频**：用独立情感参考音频 + 权重混合
- **情感向量**：8 维滑块（喜/怒/哀/惧/厌/低落/惊喜/平静）+ 预设快捷按钮
- **文本描述**：用自然语言描述情感

### 生成参数
- 静音节奏：段内分句静音、同行间隔、说话人切换间隔
- GPT2 采样：temperature、top_p、top_k、num_beams、repetition_penalty 等
- 分句参数：max_text_tokens_per_segment

### 生成与输出
- 异步任务 + SSE 实时进度推送（当前行/总行数 + 百分比）
- 在线试听 + 一键下载 WAV
- 项目保存/加载/删除

## API 契约

详细接口见各组件 README：
- [TTS 服务端 API](tts-server/README.md)
- [WebUI 后端 API](webui-backend/README.md)

### 核心数据流

```
前端提交配置 → WebUI 后端 → TTS 服务端（逐行合成 + WAV 拼接）
                                      ↓
前端试听/下载 ← WebUI 后端（音频代理）← TTS 服务端（返回音频文件）
```

## 目录结构

```
podcast-webui/
├── tts-server/           # TTS 服务端（FastAPI + IndexTTS2）
│   ├── server.py         # HTTP API 服务
│   ├── podcast_engine.py # 双人播客合成引擎（逐行合成 + WAV 拼接）
│   ├── requirements.txt
│   └── README.md
├── webui-backend/        # WebUI 后端（API 网关 + 任务管理 + 音频代理）
│   ├── server.py
│   ├── requirements.txt
│   └── README.md
├── webui-frontend/       # WebUI 前端（React + Tailwind）
│   ├── src/
│   │   ├── App.tsx           # 主应用（状态管理 + 布局）
│   │   ├── api/client.ts     # API 客户端
│   │   ├── types/index.ts    # 类型定义 + 默认值
│   │   └── components/       # UI 组件
│   │       ├── ui.tsx            # 基础组件库
│   │       ├── Header.tsx        # 顶部导航
│   │       ├── SpeakerPanel.tsx  # 角色配置面板
│   │       ├── ScriptEditor.tsx  # 脚本编辑器
│   │       ├── EmotionEditor.tsx # 情感编辑器
│   │       ├── ParamsPanel.tsx   # 全局参数面板
│   │       └── OutputPanel.tsx   # 生成输出区
│   ├── package.json
│   └── README.md
└── README.md             # 本文件
```

## 兼容性

TTS 服务端兼容 index-tts 的 `indextts2 batch --concat` 命令逻辑。
原有的 JSONL 批量合成格式可直接在 WebUI 中通过对话脚本编辑器实现：

```jsonl
{"text":"大家好","voice":"/path/host_a.wav","silence_after_ms":400}
```
对应 WebUI 中：
- 说话人 A 的参考音频 = `host_a.wav`
- 对话行 speaker=A, text="大家好"
- 全局静音 between_lines=400
