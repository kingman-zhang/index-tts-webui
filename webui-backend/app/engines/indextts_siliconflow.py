"""SiliconFlow 托管 TTS 适配器（备援：GPU 不在线时的第一优先）。

模型实测结论（2026-09-17 真机，国内站 api.siliconflow.cn）：
  - **IndexTeam/IndexTTS-2 仅国际站（api.siliconflow.com）可用**，国内站
    返回 403 "Model disabled"（/v1/models 列表也只有 CosyVoice2/MOSS-TTSD）。
  - 国内站可用且真机实测通过：**FunAudioLLM/CosyVoice2-0.5B**
    （克隆上传 → speech: URI → 合成 24kHz/16bit/单声道，与自建引擎
    输出对齐，拼接链路无感；克隆+合成无需实名认证）。
  - 默认模型取 CosyVoice2；要打国际站 IndexTTS-2 时用
    SILICONFLOW_MODEL=IndexTeam/IndexTTS-2 + 国际站 Key。

接口事实（官方文档 + 真机核实）：
  POST {base}/audio/speech          同步合成，返回音频二进制
       字段: model, input, voice, response_format(mp3/opus/wav/pcm),
             sample_rate, speed(0.25-4.0), gain(-10~10dB)
  POST {base}/uploads/audio/voice   上传参考音频克隆音色（multipart：
       file / model / customName / text——text 是参考音频的文字转写，必填）
       → {"uri": "speech:..."}
       真机坑1：customName 只允许字母/数字/_/-（不允许中文）。
       文档坑：docs.siliconflow.cn 的示例写了 IndexTTS-2，但国内站并不开放。
  GET  {base}/audio/voice/list      用户动态音色列表（作 health 轻量探活）
  POST {base}/audio/voice/deletions 删除音色（body: {"uri": ...}）
  计费：按输入文本 UTF-8 字节数，与调用次数无关（真机验证 2048 字长可用）。
  真机坑2：**wav 输出的 data 块大小字段是 0xFFFFFFFF 占位**（流式写法没
  收尾），wave 模块按头读会得到天文数字帧数 → 引擎内统一修复头部后再返回。

情绪控制（CosyVoice2 特有，真机验证可用）：
  内联富文本提示 "请用X的语气说。<|endofprompt|>正文"，引擎按统一
  8 标签自动加前缀；IndexTTS-2 无此机制，emotion_label 忽略。

引擎行为约定（与 base.py 协议对齐）：
  - 音色解析顺序：voice_map 显式映射（display_name 或路径 → speech: URI）→
    本地缓存（cache_path JSON，键=路径+大小+mtime）→ 参考音频在 backend
    侧可读时自动上传克隆并缓存。不可读且无映射报 ValueError。
  - 停顿：[pause:x] 已由 backend split_by_pauses 切分，本引擎只收纯文本。
  - 语速：直传 SiliconFlow speed 参数（0.25–4.0，超范围钳制）。
  - 输出：wav / 24000 Hz（修复头部后返回）。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import time
import wave
from pathlib import Path

import httpx

from .base import SegmentRequest

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
# 国内站真机可用的克隆模型（IndexTTS-2 仅国际站，见模块 docstring）
DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
MODEL_COSYVOICE2 = "FunAudioLLM/CosyVoice2-0.5B"
MODEL_ASR = "FunAudioLLM/SenseVoiceSmall"  # 免费额度内，克隆转写自动生成用
SPEED_MIN, SPEED_MAX = 0.25, 4.0
SAMPLE_RATE = 24000
# 输入长度上限未官方文档化，真机 2048 字符验证可用；保守沿用
MAX_INPUT_CHARS = 2048
HEALTH_TTL = 300.0  # health 结果缓存秒数（避免每个任务都打一次探活）

# 统一 8 标签 → CosyVoice2 内联情绪提示词（neutral 不加前缀）
_COSY_EMOTION_PROMPT = {
    "happy": "开心喜悦",
    "sad": "悲伤难过",
    "angry": "愤怒生气",
    "afraid": "害怕恐惧",
    "disgusted": "厌恶嫌弃",
    "melancholic": "忧郁低落",
    "surprised": "惊讶",
    "calm": "平静",
}


class IndexttsSiliconflowEngine:
    name = "indextts_siliconflow"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        cache_path: str | Path | None = None,
        voice_map: dict[str, str] | None = None,
        default_transcript: str = "你好，这是一段用于音色克隆的参考音频。",
        transcripts: dict[str, str] | None = None,
        auto_transcribe: bool = True,
        extra_params: dict | None = None,
    ):
        import os

        self.api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.model = model or os.environ.get("SILICONFLOW_MODEL") or DEFAULT_MODEL
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
        self.cache_path = Path(cache_path) if cache_path else None
        # 显式映射：键可为 display_name / local_path / tts_path
        self.voice_map = dict(voice_map or {})
        self.default_transcript = default_transcript
        # 每个参考音频的文字转写（键=display_name），克隆上传必填，转写不准影响克隆质量
        self.transcripts = dict(transcripts or {})
        # 克隆时无显式转写则用平台免费 ASR 自动转写参考音频
        self.auto_transcribe = auto_transcribe
        self.extra_params = dict(extra_params or {})
        self._cache: dict = {}
        self._cache_loaded = False
        self._health_ok: bool | None = None
        self._health_ts = 0.0

    # ---------- health ----------

    async def health(self) -> bool:
        if not self.api_key:
            return False
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_ts < HEALTH_TTL:
            return self._health_ok
        try:
            resp = await self.client.get(
                f"{self.base_url}/audio/voice/list",
                headers=self._headers(),
                timeout=10.0,
            )
            self._health_ok = resp.status_code == 200
        except Exception:
            self._health_ok = False
        self._health_ts = time.monotonic()
        return self._health_ok

    # ---------- 音色解析 ----------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    @staticmethod
    def _source_path(voice) -> Path | None:
        for p in (getattr(voice, "local_path", None), getattr(voice, "tts_path", None)):
            if p and Path(p).is_file():
                return Path(p)
        return None

    @staticmethod
    def _cache_key(path: Path) -> str:
        st = path.stat()
        return f"{path.resolve()}:{st.st_size}:{int(st.st_mtime_ns)}"

    @staticmethod
    def _sanitize_name(name: str, digest: str) -> str:
        # 平台实测（2026-09-17）：customName 仅允许字母/数字/_/-，不允许中文等
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "voice"
        return f"{stem[:48]}_{digest[:8]}"

    async def _upload_voice(self, path: Path, display_name: str) -> str:
        """上传参考音频克隆音色，返回 speech: URI。

        转写文本三级来源：显式 transcripts → ASR 自动转写（免费
        SenseVoiceSmall）→ default_transcript 兜底。真机实证：转写与
        参考音频内容不符会污染克隆，错误文本会被漏进合成结果。
        """
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
        custom_name = self._sanitize_name(display_name or path.name, digest)
        text = self.transcripts.get(display_name)
        if text is None and self.auto_transcribe:
            text = await self._asr_transcribe(path)
        if text is None:
            text = self.default_transcript
        with path.open("rb") as f:
            resp = await self.client.post(
                f"{self.base_url}/uploads/audio/voice",
                headers=self._headers(),
                files={"file": (path.name, f)},
                data={"model": self.model, "customName": custom_name, "text": text},
                timeout=120.0,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"SiliconFlow 音色上传失败 HTTP {resp.status_code}: {resp.text[:400]}"
            )
        uri = (resp.json() or {}).get("uri")
        if not uri or not str(uri).startswith("speech:"):
            raise RuntimeError(f"SiliconFlow 未返回有效 speech: URI: {resp.text[:300]}")
        return uri

    async def _asr_transcribe(self, path: Path) -> str | None:
        """用 SiliconFlow 免费 ASR（SenseVoiceSmall）转写参考音频。

        失败一律返回 None（降级到 default_transcript），绝不阻塞合成主链路。
        """
        if not self.api_key:
            return None
        try:
            with path.open("rb") as f:
                resp = await self.client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers=self._headers(),
                    files={"file": (path.name, f)},
                    data={"model": MODEL_ASR},
                    timeout=120.0,
                )
            if resp.status_code == 200:
                return ((resp.json() or {}).get("text") or "").strip() or None
        except Exception:
            return None
        return None

    async def _resolve_voice_uri(self, req: SegmentRequest) -> str:
        voice = req.voice
        # 1) 显式映射（display_name / 两个路径字段都试）
        for k in (voice.display_name, voice.local_path, voice.tts_path):
            if k and k in self.voice_map:
                return self.voice_map[k]
        # 2) 本地缓存
        self._load_cache()
        path = self._source_path(voice)
        if path is None:
            raise ValueError(
                f"SiliconFlow 备援需要参考音频在 backend 侧可读，"
                f"或在 voice_map 中预映射 speech: URI（音色: {voice.display_name!r}，"
                f"local_path={voice.local_path!r}）"
            )
        key = self._cache_key(path)
        if key in self._cache:
            return self._cache[key]
        # 3) 自动上传克隆并缓存
        uri = await self._upload_voice(path, voice.display_name or path.name)
        self._cache[key] = uri
        self._save_cache()
        return uri

    # ---------- 合成 ----------

    @staticmethod
    def _emotion_input(model: str, text: str, emotion_label: str | None) -> str:
        """CosyVoice2 用内联富文本提示控制情绪；其他模型忽略情绪标签。

        真机实测（2026-09-17 ASR 校验）：CosyVoice2 必须恒加 instruct 前缀——
        不带前缀时克隆音色合成极不稳定（空音频/截断/复读乱码/转写漏出），
        带前缀 4/4 正确，不带 0/6 正常。neutral 用「自然平稳」前缀兜底。
        """
        if model != MODEL_COSYVOICE2:
            return text
        if emotion_label and emotion_label != "neutral" and emotion_label in _COSY_EMOTION_PROMPT:
            return f"请用{_COSY_EMOTION_PROMPT[emotion_label]}的语气说。<|endofprompt|>{text}"
        return f"请用自然平稳的语气说。<|endofprompt|>{text}"

    @staticmethod
    def _repair_wav(content: bytes) -> bytes:
        """修复 SiliconFlow 流式 wav 的占位长度头（data 块大小=0xFFFFFFFF）。

        真机实测：返回 wav 的 data 块大小字段未收尾，wave 模块按头读会得到
        天文数字帧数，直接进 _concat_wavs 会产出坏音频。此处重写规范头部。
        """
        if len(content) < 44 or content[:4] != b"RIFF":
            return content
        try:
            pos = 12
            fmt_params = None
            data_offset = None
            while pos + 8 <= len(content):
                cid = content[pos:pos + 4]
                sz = struct.unpack("<I", content[pos + 4:pos + 8])[0]
                if cid == b"fmt " and sz >= 16:
                    fmt_params = struct.unpack("<HHIIHH", content[pos + 8:pos + 8 + 16])
                elif cid == b"data":
                    data_offset = pos + 8
                    # 长度字段正常且不越界 → 无需修复
                    if data_offset + sz <= len(content):
                        return content
                    break
                pos += 8 + sz + (sz & 1)
            if fmt_params is None or data_offset is None:
                return content
            _fmt, channels, rate, _byterate, _align, bits = fmt_params
            data = content[data_offset:]
            out = io.BytesIO()
            with wave.open(out, "wb") as w:
                w.setnchannels(channels)
                w.setsampwidth(max(bits // 8, 1))
                w.setframerate(rate)
                w.writeframes(data)
            return out.getvalue()
        except Exception:
            return content  # 解析失败保持原样，交由上层 wave 读取时自然报错

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        if not self.api_key:
            raise RuntimeError("SiliconFlow 引擎缺少 API Key（SILICONFLOW_API_KEY）")
        if len(req.text) > MAX_INPUT_CHARS:
            raise ValueError(
                f"SiliconFlow 单次输入超长（{len(req.text)} > {MAX_INPUT_CHARS}），请走 chunker"
            )
        uri = await self._resolve_voice_uri(req)
        payload = {
            "model": self.model,
            "input": self._emotion_input(self.model, req.text, req.emotion_label),
            "voice": uri,
            "response_format": "wav",
            "sample_rate": SAMPLE_RATE,
            "speed": min(max(float(req.speed or 1.0), SPEED_MIN), SPEED_MAX),
        }
        payload.update(self.extra_params)  # 透传额外参数（进阶参数，按需）
        resp = await self.client.post(
            f"{self.base_url}/audio/speech",
            headers=self._headers(),
            json=payload,
            timeout=300.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"SiliconFlow 合成失败 HTTP {resp.status_code}: {resp.text[:500]}"
            )
        if not resp.content:
            raise RuntimeError("SiliconFlow 返回空音频")
        return self._repair_wav(resp.content)
