"""Pydantic 请求模型（自 server.py 拆出，定义原样保留）。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ─── 项目存储 ───────────────────────────────────────────────

class ProjectModel(BaseModel):
    id: Optional[str] = None
    name: str = "未命名播客"
    voices: dict = Field(default_factory=lambda: {
        "A": {"name": "主持人A", "voice_path": None, "voice_name": None},
        "B": {"name": "主持人B", "voice_path": None, "voice_name": None},
    })
    lines: list = Field(default_factory=list)
    silence: dict = Field(default_factory=lambda: {
        "within_segment": 200, "between_lines": 300, "speaker_switch": 500,
    })
    params: dict = Field(default_factory=lambda: {
        "speed": 1.0, "speaker_speeds": {}, "max_text_tokens_per_segment": 120,
        "do_sample": True, "top_p": 0.75, "top_k": 20, "temperature": 0.6,
        "length_penalty": 0.0, "num_beams": 2, "repetition_penalty": 5.0,
        "max_mel_tokens": 1500, "infer_concurrency": 1,
    })
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ─── 请求模型（透传给 TTS 服务） ────────────────────────────

class EmotionModel(BaseModel):
    mode: int = 0
    audio_path: Optional[str] = None
    vector: list = Field(default_factory=lambda: [0.0] * 8)
    weight: float = 0.65
    text: Optional[str] = None
    random: bool = False


class PodcastLineModel(BaseModel):
    speaker: str
    text: str
    emotion: EmotionModel = Field(default_factory=EmotionModel)
    silence_after_ms: Optional[int] = None


class SilenceModel(BaseModel):
    within_segment: int = 200
    between_lines: int = 300
    speaker_switch: int = 500


class GenerationParamsModel(BaseModel):
    speed: float = 1.0
    speaker_speeds: dict[str, float] = Field(default_factory=dict)
    max_text_tokens_per_segment: int = 120
    do_sample: bool = True
    top_p: float = 0.75
    top_k: int = 20
    temperature: float = 0.6
    length_penalty: float = 0.0
    num_beams: int = 2
    repetition_penalty: float = 5.0
    max_mel_tokens: int = 1500
    infer_concurrency: int = 1  # GPU 模型串行推理，固定为 1


class PodcastRequestModel(BaseModel):
    lines: list[PodcastLineModel]
    voices: dict
    silence: SilenceModel = Field(default_factory=SilenceModel)
    params: GenerationParamsModel = Field(default_factory=GenerationParamsModel)


class SynthesizeRequestModel(BaseModel):
    voice: str
    text: str
    emotion: EmotionModel = Field(default_factory=EmotionModel)
    interval_silence: int = 200
    max_text_tokens_per_segment: int = 120
    params: GenerationParamsModel = Field(default_factory=GenerationParamsModel)


# ─── 收藏 / 术语 / 音色预设 / 队列 ─────────────────────────

class FavoriteVoicesModel(BaseModel):
    paths: list[str] = Field(default_factory=list)


class GlossaryTerm(BaseModel):
    original: str
    replacement: str


class VoicePresetModel(BaseModel):
    id: Optional[str] = None
    name: str
    voice_path: Optional[str] = None
    voice_name: Optional[str] = None
    speed: float = 1.0
    role_speed: Optional[float] = None
    emotion: dict = Field(default_factory=lambda: {
        "mode": 0, "audio_path": None, "vector": [0.0]*8,
        "weight": 0.65, "text": None, "random": False,
    })
    is_preset: bool = False  # 是否为内置预设（vs 用户自建）
    created_at: Optional[str] = None


class QueueTaskModel(BaseModel):
    project_name: str = "未命名"
    kind: str = "podcast"  # podcast=双人播客（tts-server 播客引擎）；mono=单音色配音（引擎适配层）
    lines: list
    voices: dict
    silence: dict = Field(default_factory=lambda: {"within_segment": 200, "between_lines": 300, "speaker_switch": 500})
    params: dict = Field(default_factory=lambda: {
        "speed": 1.0, "speaker_speeds": {}, "max_text_tokens_per_segment": 120, "do_sample": True, "top_p": 0.75,
        "top_k": 20, "temperature": 0.6, "length_penalty": 0.0,
        "num_beams": 2, "repetition_penalty": 5.0, "max_mel_tokens": 1500,
        "infer_concurrency": 1,
    })
    glossary_enabled: bool = True


class QueueTaskNameModel(BaseModel):
    project_name: str
