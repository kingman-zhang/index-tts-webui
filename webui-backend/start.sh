#!/bin/bash
# ============================================================
# WebUI 后端启动脚本
# 部署到 WebUI 服务器（无需 GPU）
# ============================================================

# ── 配置 ──
# ⚠️ TTS 服务端地址**不在这里配**，唯一来源是同目录 `.env` 的 `TTS_URL`
#   （与部署路径 tools/deploy_g1_autodl.sh 一致：它也只用 .env、不传 --tts-url）。
#   曾经这里写死 TTS_URL="http://localhost:8000" 并在下面当作 `--tts-url` 传给进程，
#   而**命令行参数优先级高于 .env**（app/config.py:52）⇒ .env 里改的地址被静默忽略，
#   表现为「.env 明明写了新地址，探活却永远是 localhost:8000 的 unavailable」。
#   临时改地址别改本文件，直接在 shell 里 `export TTS_URL=...` 再跑本脚本。
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
echo "  Listen:  $HOST:$PORT"
echo "  Python:  $PYTHON"
# 不在这里回显 TTS 地址：本脚本不解析 .env，回显等于再造一个会漂移的「第二真源」。
# 实际生效值由 server.py 启动横幅打印（app/main.py:190）。
echo "  TTS URL: 见下方 server.py 启动横幅（取自 .env 的 TTS_URL）"
echo "========================================="

"$PYTHON" server.py --host "$HOST" --port "$PORT"
