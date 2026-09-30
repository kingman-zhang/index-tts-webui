# TTS 服务端 (tts-server)

将 IndexTTS2 包装为 HTTP API，部署在与 index-tts 源码同一台的 GPU 服务器上。

## 部署

### 1. 安装额外依赖

在 index-tts 的 venv 中安装额外依赖：

```bash
/root/index-tts/.venv/bin/pip install fastapi uvicorn[standard] pydantic python-multipart
```

### 2. 放置文件

将 `tts-server/` 目录（`server.py` + `podcast_engine.py`）放到 GPU 服务器上，例如 `/opt/tts-server/`。

### 3. 启动服务

```bash
/root/index-tts/.venv/bin/python /opt/tts-server/server.py \
  --indextts-home /root/index-tts \
  --model-dir /mnt/storage/index-tts-data/checkpoints \
  --voices-dir /mnt/storage/index-tts-data/voices \
  --output-dir /mnt/storage/index-tts-data/outputs \
  --device cuda:0 \
  --fp16 \
  --host 0.0.0.0 \
  --port 8000
```

也可用环境变量：

```bash
export INDEXTTS_HOME=/root/index-tts
export MODEL_DIR=/mnt/storage/index-tts-data/checkpoints
export VOICES_DIR=/mnt/storage/index-tts-data/voices
export OUTPUT_DIR=/mnt/storage/index-tts-data/outputs
export DEVICE=cuda:0
export USE_FP16=1
export HOST=0.0.0.0
export PORT=8000
/root/index-tts/.venv/bin/python /opt/tts-server/server.py
```

## 可选加速项与它们的 JIT 依赖（踩过坑）

`--deepspeed`、`--cuda-kernel`（`start_spacehpc.sh` 里的两个可选开关，目前只开着后者）
不是「填个参数就好」的开关：它们要在**启动时现场编译 CUDA 扩展**，因此需要 `ninja` 和与
本机 torch 匹配的 `nvcc`。

缺依赖的失败方式非常隐蔽 —— **模型整体加载失败**，但进程照常监听、`/api/health` 仍返回 200，
只是 `status=no_model`、`model_loaded=false`；backend 探活据此判该资源不可用，于是**合成全部溢出
到云端**。2026-09-30 在 spacehpc 新机器上就是这样烧了一轮钱才发现。

```bash
# 先体检（会单独列一组「JIT 编译」）
/root/index-tts/.venv/bin/python doctor.py --deep

# 要保留加速项：装 ninja，并让它出现在 PATH 上
/root/index-tts/.venv/bin/pip install ninja
ln -sf /root/index-tts/.venv/bin/ninja /usr/local/bin/ninja
# ⚠ 光是 pip install 不够：ninja 是被当**子进程**调用的（按 PATH 查找），
#   而启动脚本直接调用 venv 的 python、并没有 activate venv。

# 不想为这点加速折腾：把 start_spacehpc.sh 里对应的变量改成空字符串
#   DEEPSPEED=""       # 本机已关掉（它没有兜底）
#   CUDA_KERNEL=""     # cuda-kernel 失败会自己降级，一般不用关
```

两者的兜底能力不同，选关哪个时注意：

| 开关 | 建不起来时的行为 | 代价 |
|------|------------------|------|
| `--cuda-kernel` | 自己打印 fallback 并降级回 torch（`infer_v2.py:117-126`） | 稍慢，能跑 |
| `--deepspeed` | **没有兜底**（`infer_v2.py:115` 未捕获）⇒ 整个模型加载失败 | 服务 `no_model`，合成全走云端 |

`server.py` 会在加载失败时打印定位、本次开关、以及体检命令。

⚠ 关于 `--deepspeed` 值不值得修：**2.5 那边已经明确不用它**（`tts-server-2.5/start.sh:14`：
DeepSpeed 把 max tokens 限制为 1024，GPT 生成超限后概率 nan 会触发 CUDA assert，2.5 改用
accel engine）。2.0 这边的收益**没有实测数据**，所以两条路都不算错 —— 想省事就直接关掉。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查，返回模型加载状态 |
| GET | `/api/voices` | 列出可用参考音频 |
| POST | `/api/voices/upload` | 上传参考音频 |
| POST | `/api/synthesize` | 同步单段合成（快速试听） |
| POST | `/api/podcast` | 提交双人播客合成任务（异步），返回 task_id |
| GET | `/api/task/{task_id}` | 查询任务状态与进度 |
| GET | `/api/task/{task_id}/audio` | 下载任务生成的音频 |
| GET | `/api/audio/{filename}` | 按文件名下载音频 |
| GET | `/api/tasks` | 列出最近的任务 |
| DELETE | `/api/task/{task_id}` | 删除任务及其音频 |

### 双人播客请求示例

```bash
curl -X POST http://localhost:8000/api/podcast \
  -H "Content-Type: application/json" \
  -d '{
    "lines": [
      {"speaker": "A", "text": "大家好，欢迎收听今天的节目。", "emotion": {"mode": 0}},
      {"speaker": "B", "text": "今天我们来聊聊人工智能语音。", "emotion": {"mode": 0}}
    ],
    "voices": {
      "A": "/mnt/storage/index-tts-data/voices/host_a.wav",
      "B": "/mnt/storage/index-tts-data/voices/host_b.wav"
    },
    "silence": {"within_segment": 200, "between_lines": 300, "speaker_switch": 500},
    "params": {"max_text_tokens_per_segment": 120, "do_sample": true, "top_p": 0.8}
  }'
```

返回：
```json
{"task_id": "a1b2c3d4e5f6", "status": "pending", "total_lines": 2}
```

### 情感控制说明

每行可独立设置 `emotion.mode`：

| mode | 方式 | 需要的字段 |
|------|------|-----------|
| 0 | 跟随音色参考音频 | 无（默认） |
| 1 | 情感参考音频 | `audio_path` + `weight` |
| 2 | 8 维情感向量 | `vector` (8 个 0-1 值) + `weight` |
| 3 | 情感描述文本 | `text` |

情感向量 8 维顺序：`[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]`
