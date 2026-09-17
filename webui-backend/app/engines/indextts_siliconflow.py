"""SiliconFlow 托管 IndexTTS-2 适配器（备援：GPU 不在线时的第一优先）。

接口事实（2026-09-17 官方文档核实：docs.siliconflow.cn / api-docs.siliconflow.cn）：
  POST {base}/audio/speech          同步合成，返回音频二进制
       字段: model, input, voice, response_format(mp3/opus/wav/pcm),
             sample_rate, speed(0.25-4.0), gain(-10~10dB)
  POST {base}/uploads/audio/voice   上传参考音频克隆音色（multipart：
       file / model / customName / text——text 是参考音频的文字转写，必填）
       → {"uri": "speech:..."}
  GET  {base}/audio/voice/list      用户动态音色列表（作 health 轻量探活）
  POST {base}/audio/voice/deletions 删除音色（body: {"uri": ...}）
  计费：按输入文本 UTF-8 字节数（$7.15/M bytes），与调用次数无关。
  模型：IndexTeam/IndexTTS-2（base_url 默认 https://api.siliconflow.cn/v1）

引擎行为约定（与 base.py 协议对齐）：
  - 音色解析顺序：
      1) voice_map 显式映射（display_name 或路径 → speech: URI，
         适合在硅基流动控制台预先建好音色、零上传成本直连）；
      2) 本地缓存（cache_path JSON，键=路径+大小+mtime）；
      3) 参考音频在 backend 侧可读 → 自动上传克隆并写缓存。
    参考音频不可读且无映射时报 ValueError（隧道拓扑下 TTS 侧音色文件
    不在本地，请在 UI 上传时走本地副本或预先配置 voice_map）。
  - 情绪：SiliconFlow 未文档化 emo_vector（与魔搭原生接口不同），
    emotion_label 暂不生效（跟随参考音频）；extra_params 可透传
    隐藏参数，待真机实测后再决定是否接情绪映射。
  - 停顿：[pause:x] 已由 backend split_by_pauses 切分，本引擎只收纯文本。
  - 语速：直传 SiliconFlow speed 参数（0.25–4.0，超范围钳制）。
  - 输出：wav / 24000 Hz（与自建引擎产出对齐，方便拼接与试听链路）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from .base import SegmentRequest

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "IndexTeam/IndexTTS-2"
SPEED_MIN, SPEED_MAX = 0.25, 4.0
SAMPLE_RATE = 24000
# 输入长度上限未官方文档化，沿用与 art 一致的保守值（真机实测后可放宽）
MAX_INPUT_CHARS = 2048
HEALTH_TTL = 300.0  # health 结果缓存秒数（避免每个任务都打一次探活）


class IndexttsSiliconflowEngine:
    name = "indextts_siliconflow"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
        cache_path: str | Path | None = None,
        voice_map: dict[str, str] | None = None,
        default_transcript: str = "你好，这是一段用于音色克隆的参考音频。",
        transcripts: dict[str, str] | None = None,
        extra_params: dict | None = None,
    ):
        import os

        self.api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.model = model or os.environ.get("SILICONFLOW_MODEL", DEFAULT_MODEL)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
        self.cache_path = Path(cache_path) if cache_path else None
        # 显式映射：键可为 display_name / local_path / tts_path
        self.voice_map = dict(voice_map or {})
        self.default_transcript = default_transcript
        # 每个参考音频的文字转写（键=display_name），克隆上传必填，转写不准影响克隆质量
        self.transcripts = dict(transcripts or {})
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
        stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", Path(name).stem).strip("_") or "voice"
        return f"{stem[:24]}_{digest[:8]}"

    async def _upload_voice(self, path: Path, display_name: str) -> str:
        """上传参考音频克隆音色，返回 speech: URI。"""
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
        custom_name = self._sanitize_name(display_name or path.name, digest)
        text = self.transcripts.get(display_name) or self.default_transcript
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
            "input": req.text,
            "voice": uri,
            "response_format": "wav",
            "sample_rate": SAMPLE_RATE,
            "speed": min(max(float(req.speed or 1.0), SPEED_MIN), SPEED_MAX),
        }
        payload.update(self.extra_params)  # 透传隐藏参数（情绪等，待真机实测）
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
        return resp.content
