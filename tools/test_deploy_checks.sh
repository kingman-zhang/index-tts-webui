#!/usr/bin/env bash
# 脱离 docker 单测 tools/lib_deploy_checks.sh。
# 用法: bash tools/test_deploy_checks.sh
#
# 为什么要有它：2026-09-29 线上出现过一次**假失败** —— 提交只改了 tools/，
# Docker 全层 CACHED、镜像 manifest 完全相同，但脚本拿 HEAD 的提交时间当基线，
# 于是误报「重建没生效」。判据错了比没有判据更糟（会让人开始不信任自检），
# 所以这里把正确情形与三种真失败情形都钉成用例。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tools/lib_deploy_checks.sh
source "$HERE/lib_deploy_checks.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
mkdir -p "$REPO/webui-backend/app" "$REPO/tools"
cd "$REPO" || exit 1
git init -q .
git config user.email t@example.com
git config user.name t

pass=0
fail=0
check() { # check <名称> <实际> <期望>
  if [[ "$2" == "$3" ]]; then
    echo "  PASS  $1"
    pass=$((pass + 1))
  else
    echo "  FAIL  $1   实际=$2  期望=$3"
    fail=$((fail + 1))
  fi
}

# ── 造历史：先改后端源码（09-10），再改一个**不进镜像**的文件（09-20）──
# 这正是线上 ff0cfb0 的形状：HEAD 比镜像新，但构建输入没变。
echo "v1" > webui-backend/app/a.py
git add -A
GIT_AUTHOR_DATE="2026-09-10T10:00:00+08:00" GIT_COMMITTER_DATE="2026-09-10T10:00:00+08:00" \
  git commit -q -m "app: v1"
echo "v1" > tools/deploy_g1_autodl.sh
git add -A
GIT_AUTHOR_DATE="2026-09-20T10:00:00+08:00" GIT_COMMITTER_DATE="2026-09-20T10:00:00+08:00" \
  git commit -q -m "tools: 只改脚本"

SRC_PATHS=(webui-backend/app webui-backend/server.py webui-backend/Dockerfile)

# docker 桩：只实现 inspect -f '{{.Created}}' <image>
FAKE_CREATED=""
docker() {
  if [[ "${1:-}" == "inspect" ]]; then
    if [[ -n "${FAKE_CREATED:-}" ]]; then
      printf '%s\n' "$FAKE_CREATED"
      return 0
    fi
    return 1
  fi
  return 1
}

rc_of() { # rc_of <Created> → 输出 check_image_fresh 的返回码
  FAKE_CREATED="$1"
  if check_image_fresh podcast-backend:latest "${SRC_PATHS[@]}" >/dev/null 2>&1; then
    echo 0
  else
    echo $?
  fi
}

echo "== 时间戳解析（docker inspect 的 9 位小数秒 / Z 后缀）=="
expect_ts=$(python3 -c '
import datetime as d
print(int(d.datetime.fromisoformat("2026-09-29T12:14:14+08:00").timestamp()))')
check "9 位小数秒可解析" "$(parse_iso_ts '2026-09-29T12:14:14.099777392+08:00')" "$expect_ts"
expect_utc=$(python3 -c '
import datetime as d
print(int(d.datetime.fromisoformat("2026-09-29T04:14:14+00:00").timestamp()))')
check "Z 后缀按 UTC 解析" "$(parse_iso_ts '2026-09-29T04:14:14.000000000Z')" "$expect_utc"
check "空串 → 0" "$(parse_iso_ts '')" "0"
check "垃圾串 → 0" "$(parse_iso_ts 'not-a-date')" "0"

echo "== check_image_fresh =="
check "基线回归：镜像(09-15)旧于HEAD(09-20)但新于构建输入(09-10) ⇒ 通过" \
  "$(rc_of '2026-09-15T12:00:00.000000000+08:00')" "0"
check "真失败：镜像(09-05)早于构建输入(09-10) ⇒ 拦截" \
  "$(rc_of '2026-09-05T12:00:00.000000000+08:00')" "1"
check "真失败：镜像不存在 ⇒ 拦截" "$(rc_of '')" "1"

echo "v2-dirty" >> webui-backend/app/a.py
check "真失败：构建输入有未提交改动 ⇒ 拦截（哪怕镜像很新）" \
  "$(rc_of '2026-09-25T12:00:00.000000000+08:00')" "1"
git checkout -q -- webui-backend/app/a.py
check "工作区恢复干净后 ⇒ 又通过" \
  "$(rc_of '2026-09-25T12:00:00.000000000+08:00')" "0"

echo
echo "===== $pass passed, $fail failed ====="
[[ "$fail" -eq 0 ]]
