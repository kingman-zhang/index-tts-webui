"""全局配置、目录与共享 HTTP 客户端（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import httpx

# ─── .env 加载（零依赖；在解析启动参数前执行，使 .env 成为默认值）───
# 规则：KEY=VALUE 每行一条，# 开头为注释；不支持行内注释；
# 已存在的真实环境变量优先于 .env（os.environ.setdefault 语义），
# 因此临时覆盖仍然方便：TTS_CONCURRENCY=4 python server.py
def _load_dotenv() -> int:
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded

_ENV_LOADED_COUNT = _load_dotenv()

# ─── 启动参数 ───────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Podcast WebUI Backend")
parser.add_argument("--tts-url", default=os.environ.get("TTS_URL", "http://localhost:8000"),
                    help="TTS 服务端地址")
parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="监听地址")
parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3001")), help="监听端口")
parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "./data"),
                    help="项目数据存储目录")
args, _ = parser.parse_known_args()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [webui-backend] %(message)s",
)
logger = logging.getLogger("webui-backend")

if _ENV_LOADED_COUNT:
    logger.info(".env 已加载 %d 项配置（真实环境变量优先）", _ENV_LOADED_COUNT)

TTS_URL = args.tts_url.rstrip("/")
DATA_DIR = Path(args.data_dir)
PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# 本地 fallback 参考音频目录（自 server.py:378 上移到配置层）
LOCAL_VOICES_DIR = DATA_DIR / "voices"
LOCAL_VOICES_DIR.mkdir(parents=True, exist_ok=True)
FAVORITES_PATH = DATA_DIR / "favorite-voices.json"

# 预设音色目录
PRESET_VOICES_DIR = DATA_DIR / "preset-voices"

# 术语词汇表
GLOSSARY_PATH = DATA_DIR / "glossary.json"

# 音色预设
VOICE_PRESETS_DIR = DATA_DIR / "voice-presets"
VOICE_PRESETS_DIR.mkdir(parents=True, exist_ok=True)

# 队列持久化目录
QUEUE_DIR = DATA_DIR / "queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_ORDER_FILE = QUEUE_DIR / "_queue_order.json"

# ─── 共享 HTTP 客户端 ───────────────────────────────────────

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(300.0, connect=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=5, keepalive_expiry=15.0),
)
