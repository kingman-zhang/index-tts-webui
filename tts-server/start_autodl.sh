#!/bin/bash
# ============================================================
# TTS 服务端启动脚本
# 部署到 GPU 服务器，与 index-tts 源码同一台机器
# ============================================================

# ── 配置（按你的服务器环境修改） ──
INDEXTTS_HOME="/root/index-tts"
MODEL_DIR="/root/index-tts/checkpoints"
VOICES_DIR="/root/autodl-tmp/index-tts/voices"
OUTPUT_DIR="/root/autodl-tmp/index-tts/outputs"
DEVICE="cuda:0"
FP16="--fp16"         # 不需要 FP16 就删掉这行（改成空字符串）
DEEPSPEED="--deepspeed"           # 可选："--deepspeed"，需先确认环境支持
CUDA_KERNEL="--cuda-kernel"         # 可选："--cuda-kernel"，BigVGAN CUDA kernel
ACCEL="--accel"               # 可选："--accel"，GPT2 acceleration engine
TORCH_COMPILE=""       # 可选："--torch-compile"，首次推理会编译
HOST="0.0.0.0"
PORT="8000"
LOG_DIR="$(cd "$(dirname "$0")" && pwd)/logs"
LOG_FILE="$LOG_DIR/tts-server.log"

# ── 启动 ──
cd "$(dirname "$0")"
mkdir -p "$LOG_DIR"
# 同时输出到当前终端和日志文件，便于排查远程任务。
exec > >(tee -a "$LOG_FILE") 2>&1
export PYTHONUNBUFFERED=1

# ── HuggingFace 镜像 ────────────────────────────────────────
# 本机无法访问 huggingface.co（Network is unreachable），而 IndexTTS-2.0
# 加载时需从 HF 拉取 facebook/w2v-bert-2.0 等辅助模型，故改走国内镜像。
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
# 模型缓存放数据盘，避免撑爆系统盘（w2v-bert-2.0 约 2GB+）。
export HF_HOME="${HF_HOME:-/root/autodl-tmp/hf_home}"

echo "========================================="
echo "  IndexTTS2 TTS Server"
echo "  Model:  $MODEL_DIR"
echo "  Voices: $VOICES_DIR"
echo "  Output: $OUTPUT_DIR"
echo "  Device: $DEVICE  FP16: ${FP16:-no}"
echo "  Listen: $HOST:$PORT"
echo "  Log Dir: $LOG_DIR"
echo "  HF Mirror: $HF_ENDPOINT"
echo "========================================="

python server.py \
  --indextts-home "$INDEXTTS_HOME" \
  --model-dir "$MODEL_DIR" \
  --voices-dir "$VOICES_DIR" \
  --output-dir "$OUTPUT_DIR" \
  --device "$DEVICE" \
  $FP16 \
  $DEEPSPEED \
  $CUDA_KERNEL \
  $ACCEL \
  $TORCH_COMPILE \
  --host "$HOST" \
  --port "$PORT"
