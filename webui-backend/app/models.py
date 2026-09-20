"""Pydantic 请求模型（自 server.py 拆出；静音/生成参数默认值可在 .env 配置）。

默认值来源 app/config.py：PODCAST_SILENCE_* 与 PODCAST_GEN_PARAMS，
前端提交时不带 silence/params 时由这里的默认值兜底。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .config import PODCAST_DEFAULT_SILENCE, PODCAST_GEN_PARAMS


# ─── 项目存储 ───────────────────────────────────────────────

def _default_params_dict() -> dict:
    return {**PODCAST_GEN_PARAMS, "speaker_speeds": {}, "infer_concurrency": 1}


class ProjectModel(BaseModel):
    id: Optional[str] = None
    name: str = "未命名播客"
    voices: dict = Field(default_factory=lambda: {
        "A": {"name": "主持人A", "voice_path": None, "voice_name": None},
        "B": {"name": "主持人B", "voice_path": None, "voice_name": None},
    })
    lines: list = Field(default_factory=list)
    script: Optional[str] = None  # 画布标记文本（前端唯一真源）；旧项目无此字段时由 lines 迁移
    silence: dict = Field(default_factory=lambda: dict(PODCAST_DEFAULT_SILENCE))
    params: dict = Field(default_factory=_default_params_dict)
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
    within_segment: int = Field(default=PODCAST_DEFAULT_SILENCE["within_segment"])
    between_lines: int = Field(default=PODCAST_DEFAULT_SILENCE["between_lines"])
    speaker_switch: int = Field(default=PODCAST_DEFAULT_SILENCE["speaker_switch"])


class GenerationParamsModel(BaseModel):
    speed: float = Field(default=PODCAST_GEN_PARAMS["speed"])
    speaker_speeds: dict[str, float] = Field(default_factory=dict)
    max_text_tokens_per_segment: int = Field(default=PODCAST_GEN_PARAMS["max_text_tokens_per_segment"])
    do_sample: bool = Field(default=PODCAST_GEN_PARAMS["do_sample"])
    top_p: float = Field(default=PODCAST_GEN_PARAMS["top_p"])
    top_k: int = Field(default=PODCAST_GEN_PARAMS["top_k"])
    temperature: float = Field(default=PODCAST_GEN_PARAMS["temperature"])
    length_penalty: float = Field(default=PODCAST_GEN_PARAMS["length_penalty"])
    num_beams: int = Field(default=PODCAST_GEN_PARAMS["num_beams"])
    repetition_penalty: float = Field(default=PODCAST_GEN_PARAMS["repetition_penalty"])
    max_mel_tokens: int = Field(default=PODCAST_GEN_PARAMS["max_mel_tokens"])
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
    silence: dict = Field(default_factory=lambda: dict(PODCAST_DEFAULT_SILENCE))
    params: dict = Field(default_factory=_default_params_dict)
    glossary_enabled: bool = True


class QueueTaskNameModel(BaseModel):
    project_name: str
