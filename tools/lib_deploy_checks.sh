#!/usr/bin/env bash
# 部署自检的纯函数（被 tools/deploy_g1_autodl.sh source）。
#
# 单独成文件是为了能**脱离 docker 单独测试** —— 见 tools/test_deploy_checks.sh。
# 本文件只定义函数，不产生副作用、不依赖 docker 存在。

# ISO8601 → epoch 秒；取不到时输出 0。
#
# 交给 python3 而不是 date(1)：`docker inspect` 给的是 9 位小数秒
# （2026-09-29T00:15:03.123456789Z），GNU date 能吞、BSD date 不能，而
# Python 3.7–3.10 的 fromisoformat 也拒收 9 位小数 ⇒ 先剥掉小数部分再解析。
parse_iso_ts() {
  python3 -c '
import datetime as d, re, sys
s = re.sub(r"\.\d+", "", (sys.argv[1] or "").strip()).replace("Z", "+00:00")
try:
    print(int(d.datetime.fromisoformat(s).timestamp()))
except Exception:
    print(0)
' "${1:-}"
}

# check_image_fresh <镜像名> <构建输入路径...>
#
# 判据：**镜像创建时间 >= 最后一次改动「该镜像构建输入」的提交时间**，
# 且这些输入在工作区里没有未提交改动。
#
# 为什么基线不能是 HEAD 的提交时间（2026-09-29 线上实测的假失败）：
#   commit ff0cfb0 只改了 tools/deploy_g1_autodl.sh，app/ 一个字节没动；
#   Docker 各层全部 CACHED、镜像 manifest 的 sha256 与上一次构建**完全相同**，
#   但 HEAD 的提交时间晚于镜像创建时间 ⇒ 拿 HEAD 比会误报「重建没生效」。
#   同理适用于「只改文档」「只改前端且 --skip-frontend」等一切不进镜像的改动。
#
# 为什么要查工作区是否干净：构建输入有未提交改动时，镜像里装的仍是旧内容，
#   光比时间戳看不出来。
check_image_fresh() {
  local image="${1:-}"
  shift || true
  if [[ -z "$image" || $# -eq 0 ]]; then
    echo "    !! check_image_fresh 用法: check_image_fresh <镜像名> <构建输入路径...>"
    return 2
  fi

  local img img_ts src_ts src_sha dirty
  img=$(docker inspect -f '{{.Created}}' "$image" 2>/dev/null || true)
  img_ts=$(parse_iso_ts "$img")
  src_ts=$(git log -1 --format=%ct -- "$@" 2>/dev/null || true); src_ts=${src_ts:-0}
  src_sha=$(git log -1 --format='%h %cI' -- "$@" 2>/dev/null || true)
  dirty=$(git status --porcelain -- "$@" 2>/dev/null || true)

  echo "  $image"
  echo "    镜像创建时间    : ${img:-（docker inspect 无输出）}"
  echo "    构建输入最后改动: ${src_sha:-（无提交记录）}"

  if [[ -n "$dirty" ]]; then
    echo "    !! 构建输入在工作区里有未提交改动 —— 镜像里装的不是它们："
    while IFS= read -r line; do [[ -n "$line" ]] && echo "       $line"; done <<< "$dirty"
    echo "       处理：提交并推送后在服务器重新拉取部署（服务器不手工改源码）。"
    return 1
  fi
  if [[ "$img_ts" -eq 0 ]]; then
    echo "    !! 取不到镜像创建时间 —— 镜像可能不存在或 docker 不可用"
    return 1
  fi
  if [[ "$img_ts" -lt "$src_ts" ]]; then
    echo "    !! 镜像早于「最后一次改动这些构建输入的提交」—— 重建没生效，容器仍在跑旧代码"
    return 1
  fi
  echo "    [ok] 镜像不旧于构建输入"
  return 0
}
