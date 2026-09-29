"""引擎选择策略 —— 唯一入口。

**本轮的策略**：按注册优先级 + 探活 + 跳过熔断冷却中的引擎。
**下一轮的策略**：负载均衡（哪个可用调哪个，按可用性/负载分发）。

为什么要单独一层（2026-09-29）：`TTS_ENGINE_PREFERRED` 这名字听起来像
「偏好」，实际行为是「把某个引擎提到优先级列表最前」—— 这是**优先级**语义，
不是**可用性分发**。两件事混在一个变量名下，所以先把「选择」抽成一个函数：
将来替换成负载均衡，只改这一个文件，runner 与适配器一行不动。

选择结果与依据都会写日志 —— 此前「这次为什么走了 art」完全不可见，
排查只能靠猜。
"""

from __future__ import annotations

import logging

from .base import TTSEngine
from .factory import build_registry

logger = logging.getLogger(__name__)


async def select_engine() -> TTSEngine:
    """选出本次任务要用的引擎。

    任务内锁定单一引擎（中途不切换）是刻意的：切片策略与音频采样率都取决于
    引擎，混用会产出参数不一致的音频。降级发生在**这里**（逐个探活），
    不是合成阶段。
    """
    registry = build_registry()
    engine = await registry.resolve()
    logger.info(
        "[engine] 选中 %s（%s）｜候选 %s｜单次上限 %s｜并发 %s",
        engine.name,
        engine.capabilities.display_name,
        [e.name for e in registry.engines],
        engine.capabilities.max_input_chars or "不限",
        engine.capabilities.max_concurrency or "读 TTS_CONCURRENCY",
    )
    return engine


def mark_engine_failed(engine_name: str) -> None:
    """把引擎置入冷却，后续任务在选择阶段跳过它。

    触发点应是「重试后仍然失败的段」—— 那种失败基本是引擎级故障
    （余额耗尽 / 鉴权失效 / 服务不可达），重试无用，但会拖垮每一个新任务。
    这正是 autodl.art 余额耗尽时的形态：health() 只能验证 Token 存在，
    于是每个新任务都兴高采烈地选中它，然后全段失败。
    """
    registry = build_registry()
    registry.mark_failed(engine_name)
    logger.warning(
        "[engine] %s 重试后仍失败，进入 %.0fs 冷却；后续任务将跳过它",
        engine_name, registry.cooldown_sec,
    )
