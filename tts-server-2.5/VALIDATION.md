# 第一阶段验证清单（隔离验证）

本清单用于在 GPU 服务器上验证 `tts-server-2.5` 是否能独立承担现有 WebUI 的合成任务。
验证通过后，再把 WebUI backend 的 `TTS_URL` 切到 2.5 服务。

## 前置准备

### 1. 上游源码与环境

- [ ] 拉取包含 `indextts/infer_v2_5.py` 的 IndexTTS 源码，放到独立目录（建议 `/root/index-tts-2.5`）。
- [ ] 用 `uv sync --all-extras` 在该目录创建独立 `.venv`，不要复用 2.0 的 venv。
- [ ] 确认 `.venv` 内能成功 `from indextts.infer_v2_5 import IndexTTS2`。

### 2. 模型下载

- [ ] 下载 2.5 模型到独立目录：

```bash
modelscope download \
  --model IndexTeam/IndexTTS-2.5 \
  --local_dir /mnt/storage/index-tts-data/checkpoints_25
```

- [ ] 确认目录下存在 `config_v2_5.yaml`。
- [ ] 确认 `checkpoints_25` 与 2.0 的 `checkpoints` 目录完全独立，没有软链接覆盖。

### 3. 输出目录

- [ ] 创建独立输出目录：`/mnt/storage/index-tts-data/outputs_25`。
- [ ] 参考音频目录可与 2.0 共用：`/mnt/storage/index-tts-data/voices`。

### 4. 启动脚本

- [ ] 修改 `start.sh` 中 `INDEXTTS_HOME`、`MODEL_DIR`、`VOICES_DIR`、`OUTPUT_DIR` 为实际路径。
- [ ] 确认 `PORT=8001`，不要和 2.0 的 8000 冲突。
- [ ] 确认 `BF16="--bf16"`，2.5 推荐 BF16 而非 FP16。

---

## 启动验证

### 5. 启动服务

```bash
cd tts-server-2.5
bash start.sh
```

- [ ] 启动日志显示 `IndexTTS-2.5 TTS Server`。
- [ ] 启动日志显示 `model-dir: .../checkpoints_25`。
- [ ] 启动日志显示 `bf16: True`。
- [ ] 出现 `>> model loaded successfully`。
- [ ] 没有 traceback 或 `model load failed`。

### 6. 健康检查

```bash
curl http://localhost:8001/api/health
```

- [ ] `status` 为 `ok`。
- [ ] `model_loaded` 为 `true`。
- [ ] `model_version` 为 `"2.5"`。
- [ ] `sample_rate` 为 `22050`。
- [ ] `bf16` 为 `true`。

### 7. 音色列表

```bash
curl http://localhost:8001/api/voices
```

- [ ] 返回的音色列表与 2.0 服务一致（因为共用 voices 目录）。
- [ ] 路径字段指向 `/mnt/storage/index-tts-data/voices/...`。

---

## 单段合成验证

### 8. 中文普通合成

```bash
curl -X POST http://localhost:8001/api/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "voice": "/mnt/storage/index-tts-data/voices/女-温润女声.wav",
    "text": "大家好，欢迎收听今天的节目。",
    "interval_silence": 200,
    "params": {"speed": 1.0}
  }'
```

- [ ] 返回 `output_filename` 和 `duration_sec`。
- [ ] 下载音频试听，音色正确，无明显异常。
- [ ] 用 `ffprobe` 确认采样率为 22050 Hz。

### 9. 语速验证（原生 duration_factor）

```bash
# 加速
curl -X POST http://localhost:8001/api/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "voice": "/mnt/storage/index-tts-data/voices/女-温润女声.wav",
    "text": "这段话应该比正常速度更快。",
    "params": {"speed": 1.3}
  }'

# 减速
curl -X POST http://localhost:8001/api/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "voice": "/mnt/storage/index-tts-data/voices/女-温润女声.wav",
    "text": "这段话应该比正常速度更慢。",
    "params": {"speed": 0.8}
  }'
```

- [ ] speed=1.3 时音频时长明显短于 speed=1.0。
- [ ] speed=0.8 时音频时长明显长于 speed=1.0。
- [ ] 变速后音质自然，没有 FFmpeg atempo 的金属感。
- [ ] 对比 2.0 的 FFmpeg 变速，2.5 原生 duration_factor 是否更自然。

### 10. 情绪参考音频

```bash
curl -X POST http://localhost:8001/api/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "voice": "/mnt/storage/index-tts-data/voices/女-温润女声.wav",
    "text": "酒楼丧尽天良，开始借机竞拍房间，哎，一群蠢货。",
    "emotion": {"mode": 1, "audio_path": "/mnt/storage/index-tts-data/voices/emo_sad.wav", "weight": 0.9}
  }'
```

- [ ] 合成成功，情绪符合参考音频的悲伤感。

### 11. 情绪向量

```bash
curl -X POST http://localhost:8001/api/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "voice": "/mnt/storage/index-tts-data/voices/女-温润女声.wav",
    "text": "对不起嘛！我的记性真的不太好，但是和你在一起的事情，我都会努力记住的~",
    "emotion": {"mode": 2, "vector": [0, 0, 0.8, 0, 0, 0, 0, 0], "weight": 0.65}
  }'
```

- [ ] 合成成功，情绪偏悲伤。
- [ ] `normalize_emo_vec` 调用没有报错。

### 12. 情感文本

```bash
curl -X POST http://localhost:8001/api/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "voice": "/mnt/storage/index-tts-data/voices/女-温润女声.wav",
    "text": "快躲起来！是他要来了！他要来抓我们了！",
    "emotion": {"mode": 3, "text": "你吓死我了！你是鬼吗？", "weight": 0.6}
  }'
```

- [ ] 合成成功，情绪符合情感描述文本。
- [ ] Qwen emotion 模型加载正常（启动时 `use_qwen_emo` 默认为 False，此测试需要确认 2.5 是否自动加载 Qwen emotion）。

> 注意：2.5 构造函数默认 `use_qwen_emo=False`。如果情感文本模式报 `RuntimeError: qwen_emo is None`，需要在 server.py 初始化时加上 `use_qwen_emo=True`。

---

## 双人播客验证

### 13. 短脚本播客

用 WebUI 前端创建一个 5-10 行的中文双人播客脚本，先不切 TTS_URL，直接用 curl 向 2.5 服务提交：

```bash
curl -X POST http://localhost:8001/api/podcast \
  -H "Content-Type: application/json" \
  -d '{
    "lines": [
      {"speaker": "A", "text": "你好，欢迎收听今天的节目。"},
      {"speaker": "B", "text": "今天我们来聊聊认知觉醒这本书。"},
      {"speaker": "A", "text": "这本书的核心观点是什么？"},
      {"speaker": "B", "text": "它认为人类的大脑有本能和理智两个系统。"}
    ],
    "voices": {
      "A": "/mnt/storage/index-tts-data/voices/女-温润女声.wav",
      "B": "/mnt/storage/index-tts-data/voices/男-温润男声.wav"
    },
    "silence": {"within_segment": 200, "between_lines": 300, "speaker_switch": 500},
    "params": {"speed": 1.0}
  }'
```

- [ ] 返回 `task_id`。
- [ ] 轮询 `GET /api/task/{task_id}` 直到 `status=completed`。
- [ ] 下载 `GET /api/task/{task_id}/audio` 试听。
- [ ] 拼接处静音长度正确，无明显拼接痕迹。
- [ ] 整体采样率为 22050 Hz。
- [ ] A/B 音色正确，没有串音。

### 14. 语速播客

同上脚本，但 `params` 改为：

```json
{"speed": 1.0, "speaker_speeds": {"A": 1.1, "B": 0.95}}
```

- [ ] A 语音明显快于 B。
- [ ] 变速自然，无金属感。

### 15. 撕音对比

用同一个容易触发撕音的脚本和音色，分别在 2.0 和 2.5 上合成：

- [ ] 2.5 是否仍有撕音。
- [ ] 如果 2.5 撕音消失或减轻，记录结论。
- [ ] 如果 2.5 仍有撕音，记录触发文本和音色，留待后续排查。

---

## 切换验证

### 16. 切换 WebUI backend 到 2.5

确认上述 1-15 全部通过后：

```bash
# 停止 WebUI backend
# 修改 start.sh 中的 TTS_URL
TTS_URL=http://localhost:8001
# 重新启动 WebUI backend
```

- [ ] 前端页面正常加载。
- [ ] 音色列表正常显示。
- [ ] 单段试听正常。
- [ ] 双人播客提交、进度推送、音频下载正常。
- [ ] 前端不需要任何代码改动。

### 17. 回滚验证

```bash
# 停止 WebUI backend
# 把 TTS_URL 改回 2.0
TTS_URL=http://localhost:8000
# 重新启动 WebUI backend
```

- [ ] 2.0 服务恢复正常。
- [ ] 2.0 的音色、参数、输出目录未受影响。
- [ ] 之前的 2.0 播客项目仍可正常合成。

---

## 已知风险点

| 风险 | 说明 | 应对 |
|------|------|------|
| `use_qwen_emo` 默认关闭 | 2.5 构造函数默认 `use_qwen_emo=False`，情感文本模式可能报错 | 如果需要情感文本，在 server.py 初始化加 `use_qwen_emo=True` |
| 生成参数差异 | 2.5 的 `to_infer_kwargs` 不再传 `length_penalty`/`num_beams`/`max_mel_tokens` | 已在 podcast_engine.py 中适配，前端旧参数会被忽略 |
| 显存占用 | 2.0 和 2.5 同时启动会占两份显存 | 第一阶段只启动一个；切换时先停旧的 |
| `normalize_emo_vec` 行为 | 2.5 的归一化策略可能与 2.0 不同 | 验证清单第 11 项专门检查 |
| 参考音频采样率 | 2.5 内部会重采样到 16 kHz 提取特征，不影响输出 | 无需改参考音频 |
