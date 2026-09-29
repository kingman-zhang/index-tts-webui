"""返回进程级池门面；实际选择与探测发生在段级租约内。"""
from __future__ import annotations

import logging
from .base import TTSEngine
from .factory import build_registry

logger = logging.getLogger(__name__)


async def select_engine() -> TTSEngine:
    engine = await build_registry().resolve()
    logger.info("[engine] 使用资源池，单次上限=%s，总槽位=%s",
                engine.capabilities.max_input_chars,
                engine.capabilities.max_concurrency)
    return engine


def mark_engine_failed(engine_name: str) -> None:
    if engine_name == 'pool':
        return  # 段级异常已经反馈给实际资源，不允许整体熔断。
    build_registry().mark_failed(engine_name)
