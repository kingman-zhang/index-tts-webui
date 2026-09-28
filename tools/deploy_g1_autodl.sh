#!/usr/bin/env bash
# G1 真机回归部署脚本（AutoDL 实例，在服务器上执行）
# 用法: bash tools/deploy_g1_autodl.sh [--skip-frontend]
# 前置: 在**仓库内**执行（仓库根默认取本脚本所在目录的上一级）
# 分支: 默认 main（生产分支）；可用 BRANCH=feat-single 覆盖
set -euo pipefail

# 仓库根 = 脚本所在目录的上一级（脚本位于 <repo>/tools/）。
# 原先硬编码 $HOME/index-tts-webui：仓库放在 /data/website/... 时，脚本会 cd 到
# 不存在的目录并被 set -e 打断，或（更糟）在别处静默跑完、看着像"部署成功"。
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$_SCRIPT_DIR/.." && pwd)}"
BRANCH="${BRANCH:-main}"
SKIP_FRONTEND=0
[[ "${1:-}" == "--skip-frontend" ]] && SKIP_FRONTEND=1

log() { echo -e "\n\033[1;32m==> $*\033[0m"; }

log "0/6 环境自检"
cd "$REPO_DIR"
REMOTE_URL=$(git remote get-url origin)
echo "repo=$REPO_DIR remote=$REMOTE_URL branch=$(git rev-parse --abbrev-ref HEAD)"
if ! git log --oneline -1; then echo "!! git 不可用或仓库异常"; exit 1; fi

# 容器部署的坑：git pull 只改磁盘，运行中的容器还跑着旧镜像里的代码。
# 本脚本重启的是裸进程 server.py，对容器完全无效 —— 宁可中止，也不要给出
# "部署成功"的假象（2026-09-28：改了不生效的排查成本极高）。
if command -v docker >/dev/null 2>&1 \
   && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^podcast-backend$'; then
  echo "!! 检测到容器 podcast-backend 正在运行 —— 本项目是 Docker 部署。"
  echo "   正确升级方式: git pull && docker compose up -d --build"
  echo "   本脚本重启裸进程对容器无效，已中止以避免假成功。"
  exit 1
fi

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

log "3.5/6 校验随代码分发的通用配置"
cd "$REPO_DIR/webui-backend"
if [[ -f data/glossary.json ]]; then
  echo "  全局术语表: $(python3 -c "import json;print(len(json.load(open('data/glossary.json'))))") 条"
else
  echo "!! data/glossary.json 缺失 —— 所有术语替换都不会生效"
  echo "   该文件随 git 分发（.gitignore 白名单）；请确认 git pull 成功且版本含本次改动"
  exit 1
fi

log "4/6 重启 webui-backend (:3001)"
# 停旧进程必须按**端口**找，不能只按命令行字符串匹配。
# 原先的 pgrep -f "webui-backend/server.py" 匹配不到本脚本自己启动的进程
# （`cd webui-backend` 后执行 `python3 server.py`，cmdline 里没有 "webui-backend/" 前缀）
# ⇒ 漏杀 ⇒ 新进程 bind 失败退出、旧进程继续服务 ⇒「脚本跑完了，代码一行没变」。
# 2026-09-28 定位。下面三条按可靠性递减：端口 → 强杀端口 → 命令行兜底。
_port_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:3001 2>/dev/null || true
    return
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser 3001/tcp 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true
    return
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep ':3001 ' | grep -oE 'pid=[0-9]+' | cut -d= -f2 || true
    return
  fi
  pgrep -f "server\.py" || true
}

PIDS=$(_port_pids)
if [[ -n "$PIDS" ]]; then
  echo "停止占用 3001 的进程: $PIDS"
  kill $PIDS || true
  sleep 2
  PIDS=$(_port_pids)
  if [[ -n "$PIDS" ]]; then
    echo "  仍在占用，强杀: $PIDS"
    kill -9 $PIDS || true
    sleep 1
  fi
else
  echo "（无进程占用 3001）"
fi
pkill -f "server\.py" || true
sleep 1

mkdir -p logs
nohup python3 server.py --port 3001 > logs/webui-backend.log 2>&1 &
NEW_PID=$!
echo "backend PID: $NEW_PID"
for i in $(seq 1 20); do
  if curl -sf http://localhost:3001/api/health >/dev/null 2>&1; then break; fi
  # 新进程若已死（例如 3001 被占），没必要再等满 20 秒
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    echo "!! 新进程已退出 —— 看 logs/webui-backend.log"
    tail -20 logs/webui-backend.log || true
    exit 1
  fi
  sleep 1
done
echo "-- /api/health --"
curl -s http://localhost:3001/api/health || echo "!! health 检查失败，看 logs/webui-backend.log"
echo

log "4.5/6 部署后自检：进程里跑的是哪份代码"
# 回答「我明明 pull 了、为什么没变化」。判据两条，任一不符即失败：
#   ① /api/version 报告的 git_head 必须等于磁盘上的 HEAD
#   ② stale_sources 必须为空（源文件不能在进程启动之后被改过）
DISK_HEAD=$(git rev-parse --short HEAD)
VER=$(curl -s http://localhost:3001/api/version || true)
if [[ -z "$VER" ]]; then
  echo "!! /api/version 无响应 —— 可能是旧版本代码（该端点本次新增）。重启后重试。"
  exit 1
fi
echo "$VER" | python3 -c '
import json,sys
v = json.load(sys.stdin)
tp = v.get("text_pipeline") or {}
print("  磁盘 HEAD      :", sys.argv[1])
print("  进程 HEAD      :", v.get("git_head"))
print("  进程启动时刻   :", v.get("process_started_at"))
print("  未加载的新改动 :", v.get("stale_sources") or "（无）")
print("  人名分隔号归一 :", tp.get("name_punct_enabled"), "/ 目标", tp.get("name_punct_target"))
print("  数字读法归一   :", tp.get("number_norm_enabled"))
print("  全局词条 / 合成:", tp.get("glossary_terms"), "/", tp.get("glossary_terms_for_synthesis"))
print("  中点变体展开   :", tp.get("glossary_sep_variants"), "上限", tp.get("glossary_sep_variants_max"))
assert v.get("git_head") == sys.argv[1], (
    "!! 进程 HEAD 与磁盘 HEAD 不一致 —— 进程跑的是旧代码，重启未生效")
assert not (v.get("stale_sources") or []), (
    "!! 下列源文件在进程启动后才被改动，进程里仍是旧版本: %s" % v["stale_sources"])
assert tp.get("name_punct_enabled") is not False, (
    "!! 人名分隔号归一化被关闭（.env NAME_PUNCT_NORMALIZE=0）—— 中点会进词表变 unk")
' "$DISK_HEAD" || { echo "!! 部署后自检未通过，见上"; exit 1; }

echo "  文本链路试算（仓库根 tools/diagnose_text.py）:"
cd "$REPO_DIR"
if python3 tools/diagnose_text.py "作家卡仑・墨菲" 2>/dev/null | grep -q "分隔号 :"; then
  echo "    [ok] 中点已被收敛（行首「分隔号 :」出现即表示改动生效）"
else
  echo "    !! 中点未被收敛 —— 磁盘上的代码或数据目录有问题，请人工看 diagnose_text.py 输出"
  exit 1
fi

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
