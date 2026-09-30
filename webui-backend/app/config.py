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

# DATA_DIR 缺省值**相对本文件**而非 cwd（2026-09-28）。
# 原缺省是 "./data"（cwd 相对）：从仓库根跑 `python webui-backend/server.py`，
# 或在别的目录用绝对路径启动，都会静默指到另一份 data/ —— 症状是「词条明明配了
# 却不生效」「诊断工具报 0 条」而日志里没有任何线索。改成相对 backend 根之后，
# 只有「显式给了 --data-dir / DATA_DIR」和「从这个目录启动」两种情形会命中，
# 二者本来就一致；其余的模糊情形一律收敛到正确的那份。
# 所有既有调用方式解析结果不变：cd webui-backend && python server.py、
# 容器内 WORKDIR=/app + 挂载 /app/data，新旧缺省算出同一个目录。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DATA_DIR = str(BACKEND_ROOT / "data")

parser = argparse.ArgumentParser(description="Podcast WebUI Backend")
parser.add_argument("--tts-url", default=os.environ.get("TTS_URL"),
                    help="音色管理面/旧播客端点指向的 tts-server（合成走资源池，不看它）")
parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="监听地址")
parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3001")), help="监听端口")
parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", _DEFAULT_DATA_DIR),
                    help="项目数据存储目录")
args, _ = parser.parse_known_args()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [webui-backend] %(message)s",
)
logger = logging.getLogger("webui-backend")

if _ENV_LOADED_COUNT:
    logger.info(".env 已加载 %d 项配置（真实环境变量优先）", _ENV_LOADED_COUNT)

# ─── 资源池配置源（2026-09-30 收敛为「一份配置」）─────────────
# 自建 tts-server / 302.ai / SiliconFlow / autodl.art 都是**同一种东西**：池里的一条资源。
# 所以它们只有一种写法（一个 JSON 列表），没有第二套变量名要记。
#
# 三种形态（优先级即此顺序，见 engines/factory.py:_resource_specs）：
#   "env"    TTS_RESOURCES 内联 JSON          —— 启动读一次，改它必须重启
#   "file"   TTS_RESOURCES_FILE 指向的 JSON   —— **支持热加载**，改文件即生效（推荐）
#   "legacy" 两者都没有                        —— 用旧式分散变量转译（TTS_URL + 各平台 Key）
def resources_source() -> tuple[str, str | None]:
    """资源池配置的来源：`(kind, 路径)`。kind ∈ {"env", "file", "legacy"}。

    ⚠️ `TTS_RESOURCES` 只要**存在**就算配置了（哪怕值是空串）：空值会在解析时明确报错，
    而**不是**静默退到 legacy —— 否则「手滑写成空」会悄悄换一套账号去合成，
    这与「配置错误直接失败，不静默退回其它账号」的原则一致。
    """
    if "TTS_RESOURCES" in os.environ:
        return "env", None
    path = (os.environ.get("TTS_RESOURCES_FILE") or "").strip()
    if path:
        return "file", path
    return "legacy", None


def resource_file_path() -> Path | None:
    """`TTS_RESOURCES_FILE` 解析成绝对路径（非 file 形态返回 None）。

    相对路径按 **backend 根**解析、不按 cwd：`DATA_DIR` 曾经缺省成 cwd 相对的
    `./data`，从错误的目录启动会静默读另一份（空的）data，症状是「配了却不生效」。
    同一个坑不踩第二次 —— `data/config/tts-resources.json` 这种写法在
    「cd webui-backend && python server.py」与容器 `WORKDIR=/app` 下都指同一处。
    """
    _, raw = resources_source()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (BACKEND_ROOT / p)


def read_resources_text() -> str | None:
    """读取资源池配置正文；`legacy` 形态返回 None（由调用方按旧变量转译）。"""
    kind, _ = resources_source()
    if kind == "env":
        return os.environ.get("TTS_RESOURCES", "")
    if kind == "file":
        p = resource_file_path()
        if p is None or not p.is_file():
            raise ValueError(f"TTS_RESOURCES_FILE 指向的文件不存在：{p}")
        return p.read_text(encoding="utf-8")
    return None


def _pool_first_local_url() -> str | None:
    """资源池里第一个 local 资源的 base_url —— 音色管理面的默认目标。

    为什么要有这个：多台 tts-server 时，用户维护的**唯一真理源**应该是资源列表；
    再单独填一条 `TTS_URL` 描述「哪一台管音色」是重复配置，也最容易配错 ——
    典型症状是音色被传到了旧机器，合成时 400「参考音频不存在」。
    所以 `TTS_URL` 没显式配置时，直接取池里第一个 local 的地址。
    """
    try:
        raw = read_resources_text()
    except ValueError:
        return None   # 文件缺失/读不了：让 factory 去报那条更明确的错
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except ValueError:
        return None   # 非法 JSON 同上
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("provider") == "local" and item.get("base_url"):
            return str(item["base_url"])
    return None


# TTS 服务地址：**只有音色管理面与旧播客端点读它，合成链路不读**（合成走资源池）。
# 优先级（高→低）：--tts-url > 真实环境变量 > .env 的 TTS_URL > 资源池里第一个 local > 内置默认。
# 最后那条兜底是关键：配好资源列表之后不必再手填 TTS_URL，少一处会配错的地方。
_explicit_tts_url = args.tts_url
_derived_tts_url = _pool_first_local_url()
TTS_URL = (_explicit_tts_url or _derived_tts_url or "http://localhost:8000").rstrip("/")
TTS_URL_SOURCE = ("显式配置（--tts-url / 环境变量 / .env）" if _explicit_tts_url
                  else "资源池里第一个 local" if _derived_tts_url
                  else "内置默认")
logger.info("TTS 服务地址 = %s（来源：%s）；它只服务音色管理与探针，合成走资源池",
            TTS_URL, TTS_URL_SOURCE)

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

# 术语词汇表（全局库：超管维护，对所有用户生效）
GLOSSARY_PATH = DATA_DIR / "glossary.json"

# 用户自定义术语库目录（按 user_id 分文件；用户同名条目优先于全局库）
GLOSSARY_USERS_DIR = DATA_DIR / "glossary_users"
GLOSSARY_USERS_DIR.mkdir(parents=True, exist_ok=True)

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
