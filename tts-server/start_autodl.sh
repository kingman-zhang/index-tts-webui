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
ACCEL=""               # 可选："--accel"，GPT2 acceleration engine
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
# 模型缓存放数据盘，避免撑爆系统盘（w2v-bert-2.0 快照约 4.4GB）。
# ⚠ 注意：indextts/infer_v2.py 第 4 行硬写 os.environ['HF_HUB_CACHE']='./checkpoints/hf_cache'
# （相对路径 + 直接赋值），会覆盖这里的 HF_HOME。所以 v2.0.0 真正读的缓存目录是
# $(pwd)/checkpoints/hf_cache，已软链 -> /root/autodl-tmp/hf_home/hub。
export HF_HOME="${HF_HOME:-/root/autodl-tmp/hf_home}"

# 容器里 OMP_NUM_THREADS 可能是空值/非法值，libgomp 会打印
# "Invalid value for environment variable OMP_NUM_THREADS"，显式给一个干净值。
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"

# 4 个辅助模型已备齐后打开离线模式：完全不联网、启动秒过这一段；
# 万一缓存缺文件会明确报错，而不是像现在这样静默卡住。
# 需要临时恢复联网：HF_HUB_OFFLINE=0 bash start_autodl.sh
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"

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
