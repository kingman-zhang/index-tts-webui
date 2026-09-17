#!/usr/bin/env bash
# G1 真机回归部署脚本（AutoDL 实例，在服务器上执行）
# 用法: bash tools/deploy_g1_autodl.sh [--skip-frontend]
# 前置: 仓库位于 ~/index-tts-webui（远程 kingman-zhang/index-tts-webui，分支 feat-single）
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/index-tts-webui}"
BRANCH="feat-single"
SKIP_FRONTEND=0
[[ "${1:-}" == "--skip-frontend" ]] && SKIP_FRONTEND=1

log() { echo -e "\n\033[1;32m==> $*\033[0m"; }

log "0/6 环境自检"
cd "$REPO_DIR"
REMOTE_URL=$(git remote get-url origin)
echo "repo=$REPO_DIR remote=$REMOTE_URL branch=$(git rev-parse --abbrev-ref HEAD)"
if ! git log --oneline -1; then echo "!! git 不可用或仓库异常"; exit 1; fi

log "1/6 拉取最新代码（$BRANCH）"
git fetch origin
CURRENT=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT" != "$BRANCH" ]]; then git checkout "$BRANCH"; fi
git pull --ff-only origin "$BRANCH"
echo "HEAD -> $(git log --oneline -1)"

log "2/6 安装新增 Python 依赖（python-docx pypdf）"
PIP="${PIP:-pip}"
$PIP install -q python-docx pypdf 2>&1 | tail -2 || \
  $PIP install -q -i https://pypi.tuna.tsinghua.edu.cn/simple python-docx pypdf
python3 -c "import docx, pypdf; print('python-docx OK, pypdf', pypdf.__version__)"

log "3/6 冒烟测试（停顿切分单测，无需 GPU）"
cd "$REPO_DIR/webui-backend"
if python3 tests/test_mono_pauses.py > /tmp/mono_pauses_test.log 2>&1; then
  tail -1 /tmp/mono_pauses_test.log
else
  echo "!! 停顿切分单测未通过："; cat /tmp/mono_pauses_test.log; exit 1
fi

log "4/6 重启 webui-backend (:3001)"
if pgrep -f "webui-backend/server.py" >/dev/null 2>&1; then
  echo "发现旧进程，停止..."
  pkill -f "webui-backend/server.py" || true
  sleep 2
fi
mkdir -p logs
nohup python3 server.py --port 3001 > logs/webui-backend.log 2>&1 &
echo "backend PID: $!"
for i in $(seq 1 20); do
  if curl -sf http://localhost:3001/api/tts/health >/dev/null 2>&1; then break; fi
  sleep 1
done
echo "-- /api/tts/health --"
curl -s http://localhost:3001/api/tts/health || echo "!! health 检查失败，看 logs/webui-backend.log"
echo

log "5/6 重启前端 dev server (:6008)"
if [[ $SKIP_FRONTEND -eq 1 ]]; then
  echo "跳过前端（--skip-frontend）"
else
  cd "$REPO_DIR/webui-frontend"
  if [[ ! -d node_modules ]]; then
    echo "首次安装 node_modules..."
    npm install --no-audit --no-fund 2>&1 | tail -3
  fi
  if pgrep -f "vite" >/dev/null 2>&1; then
    pkill -f "vite" || true
    sleep 2
  fi
  nohup npm run dev > "$REPO_DIR/webui-backend/logs/vite.log" 2>&1 &
  echo "vite PID: $!"
  for i in $(seq 1 20); do
    if curl -sf http://localhost:6008/ >/dev/null 2>&1; then break; fi
    sleep 1
  done
  curl -s -o /dev/null -w "前端 HTTP %{http_code}\n" http://localhost:6008/ || echo "!! 前端未起来，看 logs/vite.log"
fi

log "6/6 完成 —— 请在浏览器打开 AutoDL 端口代理地址（6008 端口对应的外网 URL）做配音模式回归"
echo "回归清单:"
echo "  1. 打开 /dubbing 页面"
echo "  2. 选音色 -> 输入带 [pause:0.5] 的文本 -> 插入情绪芯片"
echo "  3. 生成配音 -> 队列转完成 -> 试听"
echo "  4. 导入一个 docx/pdf 文件（验证 python-docx/pypdf）"
echo "  5. 保存/切换项目存档"
