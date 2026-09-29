"""引擎注册表工厂 —— 全进程唯一实例。

为什么必须唯一（2026-09-29 改动）：
① 原实现每个任务都调一次 `build_registry()`（mono_runner 与 podcast_runner
   各一处），而 302.ai / SiliconFlow 的 health TTL 缓存、音色解析缓存都是
   **实例级**的 —— 一重建就作废，于是每个任务都重新探活、重新解析音色。
② 熔断状态（`EngineRegistry.cooldown_until`）必须跨任务存活，否则
   「某引擎余额耗尽 ⇒ 每个新任务都先去撞一次 403 再全段失败」永远修不掉。

注册顺序即优先级（与旧行为一致，避免行为漂移）：
    自建 → 302.ai → SiliconFlow → autodl.art
有 Key/Token 才注册对应引擎；`TTS_ENGINE_PREFERRED` 可把指定引擎提到最前。

注意这里**不判断「云引擎还是本地服务器」**：注册表里每个引擎地位平等，
差异只体现在各自声明的 `capabilities` 上。部署在哪儿不影响上层。

下一轮（负载均衡）只需要替换 `select_engine()` 的排序依据，本文件的结构不变。
"""

from __future__ import annotations

import logging
import os
import threading

from ..config import DATA_DIR, TTS_URL, http_client
from .base import EngineRegistry
from .indextts_302ai import Indextts302aiEngine
from .indextts_art import IndexttsArtEngine
from .indextts_local import IndexttsLocalEngine
from .indextts_siliconflow import IndexttsSiliconflowEngine

logger = logging.getLogger(__name__)

_REGISTRY: EngineRegistry | None = None
_LOCK = threading.Lock()


def _create_registry() -> EngineRegistry:
    registry = EngineRegistry()
    # 自建 tts-server：无条件注册（不探活就不知道它在不在线，
    # 离线时 resolve() 会在 2s 内跳过它，这是设计好的降级路径）
    registry.register(IndexttsLocalEngine(TTS_URL, http_client))
    if os.environ.get("INDEXTTS302_API_KEY"):
        registry.register(
            Indextts302aiEngine(cache_path=DATA_DIR / "ai302_voices.json")
        )
    if os.environ.get("SILICONFLOW_API_KEY"):
        # 默认国内站（CosyVoice2）；要接国际站 IndexTTS-2 时：
        #   SILICONFLOW_BASE_URL=https://api.siliconflow.com/v1
        #   SILICONFLOW_MODEL=IndexTeam/IndexTTS-2
        #   （换国际站 Key，国内/国际 Key 不互通）
        registry.register(
            IndexttsSiliconflowEngine(
                base_url=os.environ.get("SILICONFLOW_BASE_URL")
                or "https://api.siliconflow.cn/v1",
                cache_path=DATA_DIR / "siliconflow_voices.json",
            )
        )
    registry.register(IndexttsArtEngine())  # token 从 AUTODL_API_TOKEN 读取

    preferred = os.environ.get("TTS_ENGINE_PREFERRED", "").strip()
    if preferred:
        by_name = {e.name: e for e in registry.engines}
        head: list = []
        for n in (s.strip() for s in preferred.split(",")):
            if not n:
                continue
            if n in by_name:
                head.append(by_name.pop(n))
            else:
                logger.warning(
                    "TTS_ENGINE_PREFERRED 含未注册引擎 %r（缺 Key 或名字写错），已忽略", n
                )
        registry.engines = head + list(by_name.values())
    return registry


def build_registry(force: bool = False) -> EngineRegistry:
    """取进程级注册表。`force=True` 强制重建（测试与热改配置时用）。"""
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None or force:
            _REGISTRY = _create_registry()
        return _REGISTRY


def reset_registry() -> None:
    """丢弃缓存实例。测试在改动环境变量后必须调用，否则读到上一个用例的引擎集。"""
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None


def engine_summary() -> list[dict]:
    """当前注册了哪些引擎、各自能力如何、是否在熔断冷却中。

    用于 `/api/version` 与启动日志 —— 这几项都是「新代码才有的符号」，
    日志里出现即证明进程加载的是哪一版；同时它也是排查
    「为什么这次走了某个引擎」的直接依据（此前完全不可见）。
    """
    registry = build_registry()
    return [
        {
            "name": e.name,
            "display_name": e.capabilities.display_name,
            "max_input_chars": e.capabilities.max_input_chars,
            "max_concurrency": e.capabilities.max_concurrency,
            "supports_speed": e.capabilities.supports_speed,
            "supports_emotion": e.capabilities.supports_emotion,
            "in_cooldown": registry.in_cooldown(e.name),
        }
        for e in registry.engines
    ]
