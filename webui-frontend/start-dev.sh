#!/bin/bash
# ============================================================
# WebUI 前端启动脚本（开发模式）
# 本地开发使用，npm run dev 即可
# 前端会自动将 /api 代理到 localhost:3001（WebUI 后端）
# ============================================================

cd "$(dirname "$0")"

echo "========================================="
echo "  Podcast WebUI Frontend (Dev)"
echo "  API proxy → http://localhost:3001"
echo "  Listen  → http://localhost:5173"
echo "========================================="

npm install --silent 2>/dev/null
npm run dev
