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
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

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


class TTSEngine(Protocol):
    """引擎适配器协议。MiniMax/CosyVoice 未来实现同接口。"""

    name: str

    async def health(self) -> bool:
        """引擎是否可用（用于故障切换）。"""
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
    """按优先级排列的引擎列表；主引擎 health 失败时自动切换备援。"""

    engines: list = field(default_factory=list)

    def register(self, engine: TTSEngine) -> None:
        self.engines.append(engine)

    async def resolve(self) -> TTSEngine:
        """返回第一个可用的引擎；全部不可用抛 RuntimeError。"""
        for engine in self.engines:
            try:
                if await engine.health():
                    return engine
            except Exception:
                continue
        raise RuntimeError("所有 TTS 引擎均不可用")

    async def synthesize(self, req: SegmentRequest) -> tuple[TTSEngine, bytes]:
        """带故障切换的单段合成：主引擎失败自动尝试下一个。"""
        last_error: Exception | None = None
        for engine in self.engines:
            try:
                if not await engine.health():
                    continue
                audio = await engine.synthesize_segment(req)
                return engine, audio
            except Exception as e:  # noqa: BLE001 - 引擎级容错，逐个降级
                last_error = e
                continue
        raise RuntimeError(f"所有 TTS 引擎均不可用: {last_error}")
