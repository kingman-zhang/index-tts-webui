"""全局配置、目录与共享 HTTP 客户端（自 server.py 拆出，行为不变）。"""

from __future__ import annotations

import argparse
import json
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

# TTS 状态栏探测开关：1 = /api/config 才去探 tts-server；0/缺省 = 不探测（前端状态栏静默）
TTS_STATUS_POLL = os.environ.get("TTS_STATUS_POLL", "0") == "1"
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

# ─── 双人播客默认静音与生成参数（管理员在 .env 调整即可，无需动代码）───
# 静音三项（毫秒）：段内分句间 / 同行连续发言间隔 / 说话人切换间隔。
# 前端不再提供配置 UI，提交时不带 silence/params，由这里的值兜底。
PODCAST_SILENCE_WITHIN_MS = max(0, int(os.environ.get("PODCAST_SILENCE_WITHIN_MS", "200")))
PODCAST_SILENCE_BETWEEN_MS = max(0, int(os.environ.get("PODCAST_SILENCE_BETWEEN_MS", "250")))
PODCAST_SILENCE_SWITCH_MS = max(0, int(os.environ.get("PODCAST_SILENCE_SWITCH_MS", "250")))

PODCAST_DEFAULT_SILENCE = {
    "within_segment": PODCAST_SILENCE_WITHIN_MS,
    "between_lines": PODCAST_SILENCE_BETWEEN_MS,
    "speaker_switch": PODCAST_SILENCE_SWITCH_MS,
}

# 生成参数默认值：PODCAST_GEN_PARAMS 为 JSON 对象，可覆盖任意子集，例如：
#   PODCAST_GEN_PARAMS={"temperature":0.5,"top_p":0.8,"repetition_penalty":4.0}
# 未写的键用内置默认；写错键/解析失败会告警并忽略，不影响启动。
_PODCAST_GEN_DEFAULTS: dict = {
    "speed": 1.0,
    "max_text_tokens_per_segment": 120,
    "do_sample": True,
    "top_p": 0.75,
    "top_k": 20,
    "temperature": 0.6,
    "length_penalty": 0.0,
    "num_beams": 2,
    "repetition_penalty": 5.0,
    "max_mel_tokens": 1500,
}
PODCAST_GEN_PARAMS: dict = dict(_PODCAST_GEN_DEFAULTS)
_raw_gen_params = os.environ.get("PODCAST_GEN_PARAMS", "").strip()
if _raw_gen_params:
    try:
        _overrides = json.loads(_raw_gen_params)
        if isinstance(_overrides, dict):
            PODCAST_GEN_PARAMS.update(
                {k: v for k, v in _overrides.items() if k in _PODCAST_GEN_DEFAULTS}
            )
            _ignored = set(_overrides) - set(_PODCAST_GEN_DEFAULTS)
            if _ignored:
                logger.warning("PODCAST_GEN_PARAMS 含未知键已忽略: %s", sorted(_ignored))
        else:
            logger.warning("PODCAST_GEN_PARAMS 必须是 JSON 对象，已忽略")
    except json.JSONDecodeError as e:
        logger.warning("PODCAST_GEN_PARAMS 解析失败（%s），使用内置默认", e)

# ─── 共享 HTTP 客户端 ───────────────────────────────────────

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(300.0, connect=10.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=5, keepalive_expiry=15.0),
)
