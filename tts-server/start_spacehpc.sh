#!/bin/bash
# ============================================================
# TTS 服务端启动脚本
# 部署到 GPU 服务器，与 index-tts 源码同一台机器
# ============================================================

# ── 配置（按你的服务器环境修改） ──
INDEXTTS_HOME="/root/index-tts"
MODEL_DIR="/mnt/storage/index-tts-data/checkpoints"
VOICES_DIR="/mnt/storage/index-tts-data/voices"
OUTPUT_DIR="/mnt/storage/index-tts-data/outputs"
DEVICE="cuda:0"
FP16="--fp16"         # 不需要 FP16 就删掉这行（改成空字符串）
# ── 加速项（可选，可被环境变量覆盖）────────────────────────────
# ⚠️ 这两个开关都靠 **JIT 现场编译 CUDA 扩展**，因此需要 ninja + 与 torch 匹配的 nvcc。
#    缺任何一个的后果非常隐蔽：模型**整体**加载失败，但进程照常监听、/api/health 仍
#    返回 200，只是 model_loaded=false ⇒ backend 探活判该资源不可用 ⇒ 合成全部溢出到
#    云端（在花钱）。2026-09-30 实际踩到：新机器没装 ninja，`--deepspeed` 直接拖垮加载。
#    两者的兜底能力也不同：`--cuda-kernel` 失败会自己降级回 torch（只慢不挂），
#    `--deepspeed` 失败**没有兜底**，会连带整个模型加载失败。
#    不想折腾就关掉（不改文件）：USE_DEEPSPEED=0 bash start_spacehpc.sh
# USE_DEEPSPEED="${USE_DEEPSPEED:-1}"
USE_DEEPSPEED=0
USE_CUDA_KERNEL="${USE_CUDA_KERNEL:-1}"
DEEPSPEED="";    [ "$USE_DEEPSPEED" = "1" ]    && DEEPSPEED="--deepspeed"
CUDA_KERNEL="";  [ "$USE_CUDA_KERNEL" = "1" ]  && CUDA_KERNEL="--cuda-kernel"
ACCEL=""               # 可选："--accel"，GPT2 acceleration engine
TORCH_COMPILE=""       # 可选："--torch-compile"，首次推理会编译
HOST="0.0.0.0"
PORT="7860"
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

# 文本 token 日志：每合成一行前，把「归一化后的 token / token id / unk」
# 写进日志，用于排查多音字与拼音标注（如 CHONG2 是否存活、有无 unk）。
# 关闭：TTS_LOG_TOKENS=0 bash start_autodl.sh
export TTS_LOG_TOKENS="${TTS_LOG_TOKENS:-1}"
# 日志级别（LOG_LEVEL=DEBUG 可看到更详细信息）
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

echo "========================================="
echo "  IndexTTS2 TTS Server"
echo "  Model:  $MODEL_DIR"
echo "  Voices: $VOICES_DIR"
echo "  Output: $OUTPUT_DIR"
echo "  Device: $DEVICE  FP16: ${FP16:-no}"
echo "  Accel:  deepspeed=${DEEPSPEED:-off}  cuda_kernel=${CUDA_KERNEL:-off}  (JIT，需 ninja + nvcc)"
echo "  Listen: $HOST:$PORT"
echo "  Log Dir: $LOG_DIR"
echo "  HF Mirror: $HF_ENDPOINT"
echo "  TokenLog: $TTS_LOG_TOKENS  (TTS_LOG_TOKENS, 0=off)  Level: $LOG_LEVEL"
echo "========================================="

# ── JIT 前置自检（只在开了加速项时执行）───────────────────────
# 目的是把「模型整体加载失败」提前成一眼能看懂的提示：这类失败不会让进程退出，
# 只是 model_loaded=false，事后从日志里非常难定位（见本文件顶部注释）。
PYTHON_BIN="$INDEXTTS_HOME/.venv/bin/python"
VENV_BIN="$(dirname "$PYTHON_BIN")"
if [ -n "$DEEPSPEED$CUDA_KERNEL" ]; then
  echo "[预检] 启用 ${DEEPSPEED:-} ${CUDA_KERNEL:-} ⇒ 需要 ninja + 与 torch 匹配的 nvcc"
  if command -v ninja >/dev/null 2>&1; then
    echo "       ninja: $(command -v ninja)"
  elif [ -x "$VENV_BIN/ninja" ]; then
    echo "       ninja: ✗ 装在 $VENV_BIN/ninja，但不在 PATH 上"
    echo "        它被当**子进程**调用（按 PATH 查找），而本脚本没 activate venv ⇒"
    echo "        照样会报「Ninja is required to load C++ extensions」。任选一条："
    echo "          export PATH=\"$VENV_BIN:\$PATH\""
    echo "          ln -sf $VENV_BIN/ninja /usr/local/bin/ninja"
  else
    echo "       ninja: ✗ 没装 —— 这会让**模型整体加载失败**（进程照常起，但 model_loaded=false）"
    echo "        修：$VENV_BIN/pip install ninja && ln -sf $VENV_BIN/ninja /usr/local/bin/ninja"
    echo "        或先关掉：USE_DEEPSPEED=0 bash $(basename "$0")"
  fi
  NVCC="$(command -v nvcc 2>/dev/null || true)"
  if [ -z "$NVCC" ] && [ -x "${CUDA_HOME:-/usr/local/cuda}/bin/nvcc" ]; then
    NVCC="${CUDA_HOME:-/usr/local/cuda}/bin/nvcc"
  fi
  if [ -n "$NVCC" ]; then
    echo "       nvcc:  $NVCC"
  else
    echo "       nvcc:  ✗ 没找到（CUDA_HOME=${CUDA_HOME:-未设置}）⇒ 只装 ninja 也编译不了"
    echo "        建议关掉加速项：USE_DEEPSPEED=0 USE_CUDA_KERNEL=0 bash $(basename "$0")"
  fi
fi

"$PYTHON_BIN" server.py \
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