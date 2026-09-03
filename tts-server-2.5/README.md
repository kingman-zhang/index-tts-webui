# TTS 服务端 2.5

IndexTTS-2.5 的独立 FastAPI 服务。它与 `tts-server/` 使用相同的 HTTP API 契约，因此第一阶段不需要修改 WebUI backend 或 frontend。

## 与 2.0 的隔离

- 独立源码环境：建议 `/root/index-tts-2.5`
- 独立模型目录：建议 `/mnt/storage/index-tts-data/checkpoints_25`
- 独立输出目录：建议 `/mnt/storage/index-tts-data/outputs_25`
- 默认监听端口：`8001`
- 参考音频目录可与 2.0 共用

## 准备上游代码和模型

上游 IndexTTS 源码必须包含 `indextts/infer_v2_5.py`。模型使用官方 `IndexTeam/IndexTTS-2.5`，并放在独立目录中：

```bash
modelscope download \
  --model IndexTeam/IndexTTS-2.5 \
  --local_dir /mnt/storage/index-tts-data/checkpoints_25
```

2.5 的 Python 环境建议与 2.0 分开，不要覆盖现有 `/root/index-tts/.venv`。

## 启动

先按服务器实际路径修改 `start.sh` 中的配置：

```bash
bash start.sh
```

等价启动参数：

```bash
/root/index-tts-2.5/.venv/bin/python server.py \
  --indextts-home /root/index-tts-2.5 \
  --model-dir /mnt/storage/index-tts-data/checkpoints_25 \
  --voices-dir /mnt/storage/index-tts-data/voices \
  --output-dir /mnt/storage/index-tts-data/outputs_25 \
  --device cuda:0 --bf16 --host 0.0.0.0 --port 8001
```

## 第一阶段 API 兼容策略

请求格式沿用现有 2.0 服务：

- `/api/synthesize` 和 `/api/podcast` 未携带语言时默认使用 `ZH`
- 对外仍使用 `params.speed`
- 服务内部转换为 IndexTTS-2.5 的 `duration_factor = 1 / speed`
- 2.5 原生输出使用 22050 Hz
- 语速不再使用 FFmpeg `atempo`，FFmpeg 仅做响度归一化

因此 WebUI backend 只需将 `TTS_URL` 指向 `http://gpu-server:8001`，frontend 第一阶段无需改动。

## 检查

```bash
curl http://localhost:8001/api/health
curl http://localhost:8001/api/voices
```

健康检查应显示：

```json
{
  "status": "ok",
  "model_loaded": true,
  "model_version": "2.5",
  "sample_rate": 22050,
  "bf16": true
}
```

## 回滚

停止 2.5 服务，将 WebUI backend 的 `TTS_URL` 改回 2.0 服务地址即可。2.0 的 `tts-server/`、源码、模型和输出目录没有被本目录修改。
