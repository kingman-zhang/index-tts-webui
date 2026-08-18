#!/bin/bash
# ============================================================
# WebUI 后端启动脚本
# 部署到 WebUI 服务器（无需 GPU）
# ============================================================

# ── 配置 ──
TTS_URL="http://localhost:8000"    # TTS 服务端地址（通过 SSH 隧道或直连）
HOST="0.0.0.0"
PORT="3001"
LOG_DIR="$(cd "$(dirname "$0")" && pwd)/logs"
LOG_FILE="$LOG_DIR/webui-backend.log"

# ── Python 解释器 ──
# 优先用 managed venv，fallback 到系统 python
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$HOME/.workbuddy/binaries/python/envs/default/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="python3"
fi

# ── 启动 ──
cd "$SCRIPT_DIR"
mkdir -p "$LOG_DIR"
# 同时输出到当前终端和日志文件。
exec > >(tee -a "$LOG_FILE") 2>&1
export PYTHONUNBUFFERED=1

echo "========================================="
echo "  Podcast WebUI Backend"
echo "  TTS URL: $TTS_URL"
echo "  Listen:  $HOST:$PORT"
echo "  Python:  $PYTHON"
echo "========================================="

"$PYTHON" server.py --tts-url "$TTS_URL" --host "$HOST" --port "$PORT"
