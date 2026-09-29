"""进程级资源池工厂；旧环境变量仅转译配置，不保留旧优先级算法。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from ..config import DATA_DIR, TTS_URL, http_client
from .base import EngineRegistry, ResourceConfig, EngineCapabilities, effective_concurrency
from .indextts_302ai import Indextts302aiEngine, DEFAULT_BASE_URL as AI302_URL
from .indextts_art import IndexttsArtEngine
from .indextts_local import IndexttsLocalEngine
from .indextts_siliconflow import IndexttsSiliconflowEngine, DEFAULT_BASE_URL as SF_URL

logger = logging.getLogger(__name__)
_REGISTRY = None
_LOCK = threading.Lock()


def _resource_specs() -> list[ResourceConfig]:
    if "TTS_RESOURCES" in os.environ or os.environ.get("TTS_RESOURCES_FILE"):
        try:
            raw = os.environ.get("TTS_RESOURCES")
            if raw is None:
                raw = Path(os.environ["TTS_RESOURCES_FILE"]).read_text(encoding="utf-8")
            items = json.loads(raw)
            if not isinstance(items, list) or not items:
                raise ValueError()
            specs = [ResourceConfig(**item) for item in items]
        except (ValueError, TypeError):
            raise ValueError("TTS_RESOURCES 必须为非空有效资源列表") from None
        if len({s.id for s in specs}) != len(specs):
            raise ValueError("资源 id 重复")
        locals_ = [s for s in specs if s.provider == "local"]
        if len(locals_) > 1 and not all(s.shared_voice_paths for s in locals_):
            raise ValueError("多本地节点必须显式确认 shared_voice_paths=true：所有节点共享相同绝对音色路径")
        return specs
    concurrency = effective_concurrency(EngineCapabilities("旧配置"))
    specs = [ResourceConfig("indextts_local", "local", base_url=TTS_URL, shared_voice_paths=True)]
    for provider, env, url in [
        ("302ai", "INDEXTTS302_API_KEY", os.environ.get("INDEXTTS302_BASE_URL") or AI302_URL),
        ("siliconflow", "SILICONFLOW_API_KEY", os.environ.get("SILICONFLOW_BASE_URL") or SF_URL),
        ("art", "AUTODL_API_TOKEN", None),
    ]:
        if os.environ.get(env):
            specs.append(ResourceConfig("indextts_" + provider, provider, env, url, concurrency))
    if os.environ.get("TTS_ENGINE_PREFERRED"):
        logger.warning("TTS_ENGINE_PREFERRED 已弃用；资源池始终本地优先、云端公平分配")
    return specs


def _create_provider(spec):
    key = os.environ.get(spec.api_key_env, "") if spec.api_key_env else ""
    if spec.base_url:
        parsed = urlsplit(spec.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url 必须是无内嵌凭据、查询参数的 HTTP 地址")
    if spec.provider == "local":
        return IndexttsLocalEngine(spec.base_url or TTS_URL, http_client)
    if not key:
        raise ValueError("云资源缺少 api_key_env 指定的凭据")
    # 凭据轮换和 endpoint 更换也不可复用旧账号音色 URI；摘要不输出该指纹。
    digest = hashlib.sha256((spec.provider + "\0" + (spec.base_url or "") + "\0" + key).encode()).hexdigest()
    cache = DATA_DIR / "engine-cache" / spec.id / digest / "voices.json"
    if spec.provider == "302ai":
        return Indextts302aiEngine(api_key=key, base_url=spec.base_url or AI302_URL, cache_path=cache, client=http_client)
    if spec.provider == "siliconflow":
        return IndexttsSiliconflowEngine(api_key=key, base_url=spec.base_url or SF_URL, cache_path=cache, client=http_client)
    if spec.provider == "art":
        kwargs = {}
        if spec.base_url:
            root = spec.base_url.rstrip("/")
            kwargs = {"submit_url": root + "/api/v1/comfyui/comfyui_workflow/indextts2-v1",
                      "result_url": root + "/api/v1/comfyui/comfyui_workflow/result/{task_id}"}
        return IndexttsArtEngine(token=key, client=http_client, **kwargs)
    raise ValueError("未知资源 provider")


def _create_registry():
    registry = EngineRegistry()
    registry.register_pool([(s, _create_provider(s)) for s in _resource_specs()])
    return registry


def build_registry(force=False):
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None or force:
            _REGISTRY = _create_registry()
        return _REGISTRY


def reset_registry():
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None


def engine_summary():
    return [{"name": e.name, "pool_schema_version": 3, "resources": e.resource_snapshot(),
             "display_name": e.capabilities.display_name,
             "max_input_chars": e.capabilities.max_input_chars,
             "max_concurrency": e.capabilities.max_concurrency,
             "supports_speed": e.capabilities.supports_speed,
             # speed_guaranteed=True ⇒ 上层不得再变速（v3 起语速只在资源侧应用一次）
             "speed_guaranteed": e.capabilities.speed_guaranteed,
             "supports_emotion": e.capabilities.supports_emotion,
             "in_cooldown": False} for e in build_registry().engines]
