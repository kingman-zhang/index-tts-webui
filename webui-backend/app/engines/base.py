"""引擎适配层（G0）。

一期两个引擎都基于 IndexTTS2：
  - indextts_local  自建 tts-server（GPU 实例，/api/synthesize + /api/podcast）
  - indextts_art    autodl.art 托管工作流（indextts2-v1，按次计费，单次 ≤2048 字符）

后期补商用 API（MiniMax / CosyVoice）时只需新增适配器，不要改动上层。

统一约定（所有适配器必须遵守）：
  - synthesize_segment(text, voice, emotion_label, speed) -> bytes
    单段合成，返回音频字节（引擎统一在适配器内做转码说明由调用方后处理）。
  - emotion_label 使用统一 8 标签：happy/sad/angry/afraid/disgusted/
    surprised/calm/neutral（None 表示跟随音色参考音频）。
  - voice 统一用 VoiceRef 描述（参考音频文件路径/字节），适配器内部消化差异。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

# IndexTTS2 8 维情感向量的真实顺序（tts-server/podcast_engine.py:26 EMO_VECTOR_LABELS，
# 千万别凭直觉排——angry 在第 2 位、melancholic 占据第 6 维）
EMO_VECTOR_ORDER = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]

# 统一情感标签（对外协议，全引擎映射的源头）：
# happy/sad/angry/afraid/disgusted/melancholic/surprised/calm/neutral
# neutral（中性）= 全零向量；autodl.art 无 neutral 字段，全零滑杆即中性。
EMOTION_LABELS = EMO_VECTOR_ORDER + ["neutral"]


@dataclass
class VoiceRef:
    """统一的音色引用：参考音频是唯一真源（对应方案中的音色目录 ref_audio 型）。"""

    local_path: Optional[str] = None   # 本地（backend 侧）可读的音频文件路径
    tts_path: Optional[str] = None     # TTS 服务器侧路径（自建引擎直用，避免重复上传）
    display_name: str = ""


@dataclass
class SegmentRequest:
    text: str
    voice: VoiceRef
    emotion_label: Optional[str] = None   # None = 跟随音色参考音频
    speed: float = 1.0


@dataclass(frozen=True)
class EngineCapabilities:
    """引擎能力声明 —— 上层唯一的差异化依据。

    为什么要有这个（2026-09-29）：此前上层用 `engine_name == "indextts_art"`
    决定是否需要切片、用 `_concurrency(engine_name)` 决定并发度，等于把
    「引擎叫什么名字」当成了「引擎能做什么」。最直接的恶果：302.ai 单次上限
    2000 字、SiliconFlow 上限 2048 字，两者超限时直接抛错「请走 chunker」，
    而上层只给 art 做了切片 —— 同一段 3000 字无停顿标记的文本，
    art 能成功、302.ai 必失败。引擎名不是能力，能力必须显式声明。

    加一个引擎时只改自己的适配器（声明能力 + 实现 synthesize_segment），
    上层一行不用动。
    """

    display_name: str
    # 单次请求的字符上限。None = 不限（由引擎/服务端自行分段）。
    # 上层据此决定是否切片，不再判断引擎名。
    max_input_chars: Optional[int] = None
    # 并发度。None = 读 TTS_CONCURRENCY（第三方 API 默认 3，钳 1-8）；
    # 自建 GPU 实例串行，显式声明 1。
    max_concurrency: Optional[int] = None
    # 是否原生支持语速。False 表示引擎会忽略 speed（当前仅 302.ai），
    # 需要变速时得在上层做后处理。本轮只声明与上报，不改变合成行为。
    supports_speed: bool = True
    # 是否支持情绪表达。False 表示合成时只能跟随参考音频。
    supports_emotion: bool = True


def effective_concurrency(cap: EngineCapabilities) -> int:
    """上层取并发度的唯一入口 —— 看能力声明，不看引擎名。

    原实现散在 mono_runner._concurrency() 里用 `engine_name == "indextts_local"`
    判断，被 podcast_runner 反向 import 复用。自建 GPU 实例串行这件事是
    **该引擎的能力**（max_concurrency=1），不是它的名字。
    """
    if cap.max_concurrency is not None:
        return max(1, int(cap.max_concurrency))
    try:
        n = int(os.environ.get("TTS_CONCURRENCY", "3").strip())
    except ValueError:
        n = 3
    return max(1, min(n, 8))


class TTSEngine(Protocol):
    """引擎适配器协议。MiniMax/CosyVoice 未来实现同接口。

    唯一约定：`synthesize_segment` 一律返回**音频字节**（wav）。
    各家「提交任务 → 轮询 → 下载 URL」的差异是适配器内部的事，
    不允许泄漏成调用方可见的第二种返回形态。

    反例（正在修）：自建 tts-server 的 /api/synthesize 返回的是 JSON
    `output_filename`，需要二次 GET /api/audio/{f} 才拿到字节 ——
    这段二次下载由 IndexttsLocalEngine 内部消化，上层看不到。
    """

    name: str
    capabilities: EngineCapabilities

    async def health(self) -> bool:
        """凭据/连通性探活（便宜、可缓存），用于选择阶段的候选排序。

        注意探活**不等于**可用：autodl.art 只能验证 Token 存在，
        余额耗尽（HTTP 403）要等真正合成才暴露。所以合成阶段的真实失败
        必须反馈回选择层（见 EngineRegistry.mark_failed 的熔断）。
        """
        ...

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        """合成单段文本，返回音频字节。"""
        ...


def audio_data_uri(path: str | Path) -> str:
    """把本地音频文件转成 data URI（autodl.art prompt_simple 字段格式）。"""
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "audio/wav"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode('ascii')}"


@dataclass
class EngineRegistry:
    """候选引擎列表 + 进程级熔断状态。

    两条历史教训（2026-09-29）：

    ① 原 `synthesize()` 写着「主引擎失败自动尝试下一个」，但两个 runner
       都是 `resolve()` 之后直接 `engine.synthesize_segment()` —— 那段降级
       代码从未被调用，是死代码，注释与行为长期不符。已删除。降级真正发生的
       位置是**选择阶段**（resolve 逐个探活），不是合成阶段；任务内刻意锁定
       单一引擎，理由写在 resolve() 上。

    ② autodl.art 的 health() 只能验证 Token 存在（平台无实例概念），
       余额耗尽时依然「健康」并被选中，然后每一段都失败。所以**合成阶段的
       真实失败必须能反馈回选择层** —— 这就是 mark_failed() 的熔断。

    熔断状态跨任务生效，所以本对象必须是进程级单例（见 factory.get_registry）。
    """

    engines: list = field(default_factory=list)
    # 引擎名 → 熔断到期时刻（time.monotonic）。不参与相等性判断。
    cooldown_until: dict = field(default_factory=dict, repr=False)
    cooldown_sec: float = 120.0

    def register(self, engine: TTSEngine) -> None:
        self.engines.append(engine)

    def mark_failed(self, engine_name: str) -> None:
        """把引擎置入冷却 —— 后续任务在选择阶段跳过它。

        触发点是「重试后仍然失败的段」，那种失败基本是引擎级故障
        （余额耗尽、鉴权失效、服务不可达），重试无用但会拖垮每一个新任务。
        """
        self.cooldown_until[engine_name] = time.monotonic() + self.cooldown_sec

    def in_cooldown(self, engine_name: str) -> bool:
        until = self.cooldown_until.get(engine_name)
        if until is None:
            return False
        if time.monotonic() >= until:
            self.cooldown_until.pop(engine_name, None)
            return False
        return True

    async def resolve(self) -> TTSEngine:
        """选出本次任务要用的引擎：按注册顺序逐个探活。

        **任务内不切换引擎**是刻意的：切片策略与音频采样率都取决于引擎，
        中途换引擎会产出参数不一致的音频（见 _concat_wavs 的一致性强校验）。

        若所有候选都在冷却中，则忽略冷却取优先级最高的一个 —— 宁可让它
        再试一次，也不要因为一个引擎故障就让整个服务拒绝接单。
        """
        if not self.engines:
            raise RuntimeError("没有注册任何 TTS 引擎")
        live = [e for e in self.engines if not self.in_cooldown(e.name)]
        if not live:
            logger.warning(
                "[engine] 全部 %d 个引擎均在冷却中，忽略冷却按优先级重试: %s",
                len(self.engines), [e.name for e in self.engines],
            )
            live = list(self.engines)
        for engine in live:
            try:
                if await engine.health():
                    return engine
            except Exception:  # noqa: BLE001 - 探活失败即视为不可用
                continue
        raise RuntimeError("所有 TTS 引擎均不可用")
