#!/usr/bin/env bash
# G1 真机回归部署脚本（在服务器上执行）
# 用法: bash tools/deploy_g1_autodl.sh [--skip-frontend]
# 前置: 在**仓库内**执行（仓库根默认取本脚本所在目录的上一级）
# 分支: 默认 main（生产分支）；可用 BRANCH=feat-single 覆盖
# 形态: 自动识别 —— 检测到容器 podcast-backend 走 Docker 路径（重建镜像），
#       否则走裸进程路径（重启 server.py）。两条路的「生效」条件不同，详见下方分叉处。
# 注意: Docker 部署下**不要**随手加 --skip-frontend，前端也是镜像；
#       前后端都有改动时必须两个镜像一起重建。
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

# 部署自检的纯函数（parse_iso_ts / check_image_fresh）。抽成单独文件是为了能
# 脱离 docker 单测，见 tools/test_deploy_checks.sh。
# shellcheck source=tools/lib_deploy_checks.sh
source "$_SCRIPT_DIR/lib_deploy_checks.sh"

# ── Docker 部署路径（2026-09-29 新增）──────────────────────────────────────
# 背景：容器跑的是**镜像里 COPY 进去的** app/，`git pull` 只改磁盘 ⇒ 不重建镜像
# 一行都不生效。旧版脚本遇到容器直接 exit 1 —— 判断正确但没解决问题，用户还得
# 自己记住「git pull && docker compose up -d --build」。现在直接接管。
# 依据 webui-backend/Dockerfile：只有 `COPY app ./app` + `COPY server.py .`
# （⇒ 改代码必须 rebuild）；data/ 与 .env 是挂载卷（⇒ 改词表靠宿主机、
# 改 .env 只需 restart 不需 rebuild）。
docker_upgrade() {
  cd "$REPO_DIR"
  local compose="docker compose"
  docker compose version >/dev/null 2>&1 || compose="docker-compose"

  log "1/4 拉取最新代码（$BRANCH）"
  git fetch origin
  if [[ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]]; then git checkout "$BRANCH"; fi
  git pull --ff-only origin "$BRANCH"
  echo "HEAD -> $(git log --oneline -1)"

  log "2/4 校验随代码分发的通用配置"
  if [[ -f webui-backend/data/glossary.json ]]; then
    echo "  全局术语表: $(python3 -c "import json;print(len(json.load(open('webui-backend/data/glossary.json'))))") 条（宿主机侧；容器以挂载卷读取）"
  else
    echo "!! webui-backend/data/glossary.json 缺失 —— 容器内术语表会静默失效"
    echo "   该文件随 git 分发（.gitignore 白名单）；请确认 git pull 成功且版本含本次改动"
    exit 1
  fi
  if ! docker network inspect software_app-net >/dev/null 2>&1; then
    echo "!! 外部网络 software_app-net 不存在（docker-compose.yml 声明为 external: true）"
    echo "   先执行: docker network create software_app-net"
    exit 1
  fi

  log "3/4 重建镜像并重启容器"
  if [[ $SKIP_FRONTEND -eq 1 ]]; then
    echo "（--skip-frontend：只重建 backend —— 前端也有改动时不要用这个参数）"
    $compose up -d --build backend
  else
    $compose up -d --build
  fi

  echo "等待容器健康（最多 60s）..."
  local st="unknown"
  for _ in $(seq 1 30); do
    st=$(docker inspect -f '{{.State.Health.Status}}' podcast-backend 2>/dev/null || echo "none")
    [[ "$st" == "healthy" ]] && break
    sleep 2
  done
  echo "  podcast-backend 健康状态: $st"
  if [[ "$st" != "healthy" ]]; then
    echo "!! 容器未达 healthy，日志如下:"
    docker logs --tail 30 podcast-backend || true
    exit 1
  fi

  log "4/4 部署后自检：容器里跑的是哪份代码"
  # 容器内 /app **没有 .git**（.dockerignore 排除），/api/version 的 git_head
  # 恒为 null ⇒ 不能像裸进程那样拿它比磁盘 HEAD。改用三条独立证据：
  #   ① 镜像里的 app/ 是不是当前代码（check_image_fresh，按**构建输入**判，
  #      不是按 HEAD —— 只改文档/tools 时 HEAD 会新于镜像，那是正常的）；
  #   ② /api/version 返回 200 且含 text_pipeline / engines（这些端点的字段是
  #      新代码才有的符号，镜像里若是旧代码会直接 404）；
  #   ③ 挂载卷里词表条数 > 0。
  #
  # 基线为什么不能是 HEAD 的提交时间：见 lib_deploy_checks.sh 里
  # check_image_fresh 的注释（2026-09-29 ff0cfb0 的线上假失败）。
  BACKEND_SRC="webui-backend/app webui-backend/server.py webui-backend/Dockerfile webui-backend/Dockerfile.base webui-backend/requirements.txt"
  FRONTEND_SRC="webui-frontend/src webui-frontend/Dockerfile webui-frontend/nginx.conf webui-frontend/index.html webui-frontend/package.json webui-frontend/package-lock.json webui-frontend/vite.config.ts"
  check_image_fresh podcast-backend:latest $BACKEND_SRC || exit 1
  check_image_fresh podcast-web:latest $FRONTEND_SRC || exit 1

  local ver
  ver=$(docker exec podcast-backend python -c \
    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:3001/api/version',timeout=5).read().decode())" \
    2>/dev/null || true)
  if [[ -z "$ver" ]]; then
    echo "!! 容器内 /api/version 无响应 —— 镜像里是旧代码（该端点本次新增）"
    docker logs --tail 20 podcast-backend || true
    exit 1
  fi
  printf '%s' "$ver" > /tmp/wb-docker-version.json
  python3 -c '
import json,sys
v = json.load(open(sys.argv[1]))
tp = v.get("text_pipeline") or {}
print("  容器内数据目录 :", v.get("data_dir"), "存在" if v.get("data_dir_exists") else "!! 不存在")
print("  词表真源       :", v.get("glossary_path"), "存在" if v.get("glossary_exists") else "!! 不存在")
print("  全局词条 / 合成:", tp.get("glossary_terms"), "/", tp.get("glossary_terms_for_synthesis"))
print("  人名分隔号归一 :", tp.get("name_punct_enabled"), "/ 目标", tp.get("name_punct_target"))
print("  年份读法归一   :", tp.get("year_norm_enabled"), "| 逐位读位数 >=", tp.get("year_norm_min_digits"))
print("  时间读法归一   :", tp.get("time_norm_enabled"), "| 受理段数 >=", tp.get("time_norm_max_parts"))
print("  数值读法归一   :", tp.get("num_value_normalize"), "| 受理位数 <=", tp.get("num_value_max_digits"))
print("  数字读法归一   :", tp.get("number_norm_enabled"))
print("  中点变体展开   :", tp.get("glossary_sep_variants"), "上限", tp.get("glossary_sep_variants_max"))
eng = ((v.get("engines") or {}).get("registered")) or []
print("  引擎优先级     :", " → ".join(e.get("name", "?") for e in eng) or "!! 无引擎")
for e in eng:
    print("  资源池         : schema v%s" % e.get("pool_schema_version"),
          "| speed_guaranteed =", e.get("speed_guaranteed"),
          "| normalizes_loudness =", e.get("normalizes_loudness"),
          "| 资源数", len(e.get("resources") or []),
          "| 总槽位", e.get("max_concurrency"))
sm = v.get("source_mtimes") or {}
print("  受监视源文件数 :", sum(1 for x in sm.values() if x is not None), "/", len(sm))
stale = v.get("stale_sources") or []
print("  进程启动后改动 :", stale or "无")
assert v.get("glossary_exists"), "!! 容器读到的数据目录里没有 glossary.json —— 挂载卷指错了，所有词条静默失效"
assert tp.get("glossary_terms"), "!! 词表条数为 0 —— 术语替换不会生效"
assert tp.get("name_punct_enabled") is not False, "!! 人名分隔号归一化被关闭 —— 中点会进词表变 unk"
assert tp.get("year_norm_enabled") is not False, "!! 年份读法归一化被关闭（YEAR_NORMALIZE=0）—— 2011 年会被读成数值"
# year_norm_min_digits 是 2026-09-29「三位年份逐位读」引入的新符号。它的值本身就是判据：
# 只有 year_norm_enabled 时，「四位版」与「三位版」都报 true，**分辨不出来** ——
# 于是会出现「2011 读对、1550～1850 读对，偏偏 850 读成八百五十」这种只在旧版发生的现象。
assert (tp.get("year_norm_min_digits") or 9) <= 3, (
    "!! 年份逐位读的下限不是 3（或字段缺失）—— 跑的是只有四位规则的旧代码，"
    "公元850年 会被读成「八百五十」")
# num_value_max_digits 是 2026-09-29「数值读法」引入的新符号，同理报**取值域**而非开关：
# 字段缺失 = 镜像里还没有这层（`230 倍` 会以阿拉伯数字进模型、读成「二三零」）。
assert (tp.get("num_value_max_digits") or 0) >= 8, (
    "!! 数值读法这一层不在（字段缺失或上限偏小）—— 230 倍 / 110 元 这类会原样进模型变 unk")
# engines 字段是 2026-09-29 引擎层拆分后的新符号；注册数为 0 意味着任何合成都必然失败
assert eng, "!! 没有任何 TTS 引擎被注册 —— 所有合成都将失败（检查 .env 里的 API Key / Token）"
# pool_schema_version=3 / speed_guaranteed 是 2026-09-29「语速只在资源侧应用一次」
# 引入的新符号，同样报**取值**而不是开关：只比 supports_speed 分辨不出来 ——
# 池门面在混池时两版都报 False。缺这两个字段 = 跑的是会把语速叠加两遍的旧代码
# （播客里把语速调到 1.5 实际听到 ≈ 2.25 倍）。
assert all((e.get("pool_schema_version") or 0) >= 3 and e.get("speed_guaranteed") is True
           for e in eng), (
    "!! 资源池 schema 不是 v3（或 speed_guaranteed 不为 true）—— 镜像里是语速会被叠加的旧代码，"
    "播客加速听起来明显偏快（实际 ≈ speed²）")
# normalizes_loudness 是 2026-09-30「响度归一去重」引入的新符号。它**可以为 False**
# 且属正常（混池/纯云池、以及**本地 2.5 壳**都报 False），所以只断言**字段存在** ——
# 缺了说明是「对本地段重复做一次响度归一」的旧代码。
# 不再断言「池内全是本地资源时必须为 True」：本地壳有两代，tts-server/（2.0）自述
# True、tts-server-2.5/（单遍 loudnorm 达不到 -16）自述 False，硬断言必然误报。
# 实际值打印在下面，请与 tts-server 的 /api/health 自述核对（2.0 应 true、2.5 应 false）。
assert all("normalizes_loudness" in e for e in eng), (
    "!! 引擎能力里没有 normalizes_loudness 字段 —— 镜像会对本地段重复做响度归一"
    "（白跑一次 ffmpeg + 一次重采样）")
for e in eng:
    if (e.get("resources") or []) and all(
            r.get("tier") == "local" for r in (e.get("resources") or [])):
        print("  [note] 全 local 池的响度保证 normalizes_loudness =",
              e.get("normalizes_loudness"), "（2.0 壳应 true / 2.5 壳应 false）")
# source_mtimes 全为 null ⇒ 探测路径算错了（曾因 REPO_ROOT 取成 `/` 在容器里恒为空，
# stale_sources 也就永远查不出「进程跑的是旧代码」）。Docker 下必须能读到。
assert any(x is not None for x in sm.values()), "!! 受监视源文件一个都没找到 —— build_info 的路径解析在容器里失效了"
assert not stale, "!! 源文件在进程启动之后被改过 —— 容器里跑的仍是旧代码（重建镜像后要重启容器）"
print("  [ok] 镜像含新代码（/api/version 存在、字段齐全、引擎已注册）")
' /tmp/wb-docker-version.json || { echo "!! 部署后自检未通过，见上"; exit 1; }
  rm -f /tmp/wb-docker-version.json

  echo
  echo "注意：容器内 /app 无 .git ⇒ /api/version 的 git_head 恒为 null 属正常，"
  echo "      不要据此判断「没部署成功」；以上三条证据才是判据。"

  # 上面那几条证据都是在容器里取的。宿主机侧核对必须走 web 的对外端口：
  # compose 里 backend 用的是 expose（仅内网可达），3001 在宿主机上是空的
  # —— 直接 curl localhost:3001/api/version 会静默返回空，很容易被误读成
  # 「部署失败」或「端点不存在」。这里把实际端口查出来直接印出来。
  local webport
  webport=$(docker port podcast-web 80 2>/dev/null | head -1 | sed 's/.*://')
  echo
  echo "宿主机侧核对版本（走 web 反代；backend 只在 compose 内网可达，3001 在宿主机上是空的）:"
  echo "  curl -s localhost:${webport:-8088}/api/version"
  echo "  （端口取自 docker port podcast-web 80；也可 docker exec podcast-backend \\"
  echo "    python -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:3001/api/version').read().decode())\"）"
}

log "0/6 环境自检"
cd "$REPO_DIR"
REMOTE_URL=$(git remote get-url origin)
echo "repo=$REPO_DIR remote=$REMOTE_URL branch=$(git rev-parse --abbrev-ref HEAD)"
if ! git log --oneline -1; then echo "!! git 不可用或仓库异常"; exit 1; fi

# 部署形态分叉：容器 vs 裸进程。两条路的「生效」条件完全不同 ——
# 容器要重建镜像，裸进程要重启进程。走错一条就是「脚本跑完了、代码一行没变」。
if command -v docker >/dev/null 2>&1 \
   && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^podcast-backend$'; then
  echo "==> 检测到容器 podcast-backend 正在运行 —— 走 Docker 升级路径（重建镜像）"
  docker_upgrade
  log "完成（Docker 部署）—— 请在浏览器打开前端做回归（见文末清单）"
  echo "回归清单:"
  echo "  1. 打开前端页面（Docker 部署看 docker-compose.yml 里 PODCAST_WEB_PORT，默认 8088）"
  echo "  2. 新稿子提交一次，确认预估积分按实际字数显示（不再不足千字按整千扣）"
  echo "  3. 试听含人名中点的句子（如「作家卡仑・墨菲」）确认无怪音"
  echo "  4. 改一段文字重新提交，确认只按新字数扣费"
  echo "  5. 宿主机核对版本与引擎（端点新符号）：curl -s localhost:8088/api/version"
  echo "     要能看到 engines.registered 里有引擎；看不到说明进程仍是旧代码"
  exit 0
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
  pgrep -f "python3 server\.py --port 3001" || true
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
# 兜底清残留：只认本脚本自己的启动签名（`python3 server.py --port 3001`）。
# 不要写成 pkill -f "server\.py" —— 同机上 tts-server 也跑 server.py，会被一起带走。
pkill -f "python3 server\.py --port 3001" || true
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
print("  数据目录       :", v.get("data_dir"), "存在" if v.get("data_dir_exists") else "!! 不存在")
print("  词表真源       :", v.get("glossary_path"), "存在" if v.get("glossary_exists") else "!! 不存在")
print("  人名分隔号归一 :", tp.get("name_punct_enabled"), "/ 目标", tp.get("name_punct_target"))
print("  年份读法归一   :", tp.get("year_norm_enabled"), "| 逐位读位数 >=", tp.get("year_norm_min_digits"))
print("  时间读法归一   :", tp.get("time_norm_enabled"), "| 受理段数 >=", tp.get("time_norm_max_parts"))
print("  数值读法归一   :", tp.get("num_value_normalize"), "| 受理位数 <=", tp.get("num_value_max_digits"))
print("  数字读法归一   :", tp.get("number_norm_enabled"))
print("  全局词条 / 合成:", tp.get("glossary_terms"), "/", tp.get("glossary_terms_for_synthesis"))
print("  中点变体展开   :", tp.get("glossary_sep_variants"), "上限", tp.get("glossary_sep_variants_max"))
eng = ((v.get("engines") or {}).get("registered")) or []
print("  引擎优先级     :", " → ".join(e.get("name", "?") for e in eng) or "!! 无引擎")
for e in eng:
    print("    -", e.get("name"), "| schema v%s" % e.get("pool_schema_version"),
          "| 单次上限", e.get("max_input_chars") or "不限",
          "| 并发", e.get("max_concurrency") or "env",
          "| 语速", e.get("supports_speed"), "| 语速保证", e.get("speed_guaranteed"),
          "| 响度保证", e.get("normalizes_loudness"),
          "| 情绪", e.get("supports_emotion"),
          "| 冷却中" if e.get("in_cooldown") else "")
# 引擎层（2026-09-29 拆分）：engines 字段是新代码才有的符号；注册数为 0 意味着
# 任何合成都必然失败（通常是没有可用 Key/Token，或 .env 没被读到）。
assert eng, "!! 没有任何 TTS 引擎被注册 —— 所有合成都将失败（检查 .env 里的 API Key / Token）"
# 见 Docker 路径同名断言：语速只应用一次靠 schema v3 + speed_guaranteed 取值来判，
# 光看 supports_speed 分辨不出来（混池两版都报 False）。
assert all((e.get("pool_schema_version") or 0) >= 3 and e.get("speed_guaranteed") is True
           for e in eng), (
    "!! 资源池 schema 不是 v3（或 speed_guaranteed 不为 true）—— 跑的是语速会被叠加的旧代码，"
    "播客加速听起来明显偏快（实际 ≈ speed²）")
# normalizes_loudness 是 2026-09-30「响度归一去重」引入的新符号，见 Docker 路径同名断言：
# 只断言**字段存在**；**允许为 False**（混池/纯云池，以及本地 2.5 壳都属正常）。
# 不再断言「全 local ⇒ True」：2.0 壳 True、2.5 壳 False，硬断言必然误一边。
assert all("normalizes_loudness" in e for e in eng), (
    "!! 引擎能力里没有 normalizes_loudness 字段 —— 跑的是会对本地段重复做响度归一的旧代码")
for e in eng:
    if (e.get("resources") or []) and all(
            r.get("tier") == "local" for r in (e.get("resources") or [])):
        print("  [note] 全 local 池的响度保证 normalizes_loudness =",
              e.get("normalizes_loudness"), "（2.0 壳应 true / 2.5 壳应 false）")
assert v.get("git_head") == sys.argv[1], (
    "!! 进程 HEAD 与磁盘 HEAD 不一致 —— 进程跑的是旧代码，重启未生效")
assert v.get("glossary_exists"), (
    "!! 进程读到的数据目录里没有 glossary.json —— 数据目录指错了，所有词条静默失效")
assert tp.get("name_punct_enabled") is not False, (
    "!! 人名分隔号归一化被关闭（.env NAME_PUNCT_NORMALIZE=0）—— 中点会进词表变 unk")
assert tp.get("year_norm_enabled") is not False, (
    "!! 年份读法归一化被关闭（.env YEAR_NORMALIZE=0）—— 四位年份会被读成数值"
    "（2011 年 → 两千零一十一年）")
# 见 Docker 路径同名断言：字段值本身就是「三位规则在不在」的判据。
assert (tp.get("year_norm_min_digits") or 9) <= 3, (
    "!! 年份逐位读的下限不是 3（或字段缺失）—— 跑的是只有四位规则的旧代码，"
    "公元850年 会被读成「八百五十」")
# 见 Docker 路径同名断言：数值读法这一层在不在，看取值域而不看开关。
assert (tp.get("num_value_max_digits") or 0) >= 8, (
    "!! 数值读法这一层不在（字段缺失或上限偏小）—— 230 倍 / 110 元 会原样进模型变 unk")
# time_norm_max_parts 是 2026-09-30「时间读法」引入的新符号。同样看取值域：
# 字段缺失 = 镜像里还没有这层 —— 带空格的 `12 : 30` 与小时为 0 的 `0:30` 会被 TN
# 读成「十二比三十 / 零比三十」（比例），`12:30` 也只是碰巧读对。
assert (tp.get("time_norm_max_parts") or 0) >= 2, (
    "!! 时间读法这一层不在（字段缺失）—— 12:30 / 12 : 30 / 0:30 会原样进模型，"
    "带空格与 0 点开头的会被读成「比」")
# stale_sources 放最后断言：它监视的是 build_info.WATCHED 里那组文本/引擎链路文件
# （name_punct / year_norm / time_norm / num_value_norm / number_norm / stores /
# queue_worker / engines 三件），比较用的是浮点 mtime，秒级不模糊。正常「先 pull 再重启」
# 的顺序下它必然为空；若非空，说明确有文件在这次启动之后被写过，是真问题。
assert not (v.get("stale_sources") or []), (
    "!! 下列源文件在进程启动后才被改动，进程里仍是旧版本: %s" % v["stale_sources"])
' "$DISK_HEAD" || { echo "!! 部署后自检未通过，见上"; exit 1; }

echo "  文本链路试算（仓库根 tools/diagnose_text.py）:"
cd "$REPO_DIR"
if python3 tools/diagnose_text.py "作家卡仑・墨菲" 2>/dev/null | grep -q "分隔号 :"; then
  echo "    [ok] 中点已被收敛（行首「分隔号 :」出现即表示改动生效）"
else
  echo "    !! 中点未被收敛 —— 磁盘上的代码或数据目录有问题，请人工看 diagnose_text.py 输出"
  exit 1
fi
# 年份读法（2026-09-29）：这条规则以前只在 tts-server，云引擎链路绕过它 ⇒ 必须显式核。
if python3 tools/diagnose_text.py "在 2011 年发生的事" 2>/dev/null | grep -q "二零一一年"; then
  echo "    [ok] 年份读法生效（2011 年 → 二零一一年）"
else
  echo "    !! 年份未被逐位读 —— 见 webui-backend/app/year_norm.py"
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
