"""自建 tts-server 适配器（IndexTTS 2.0，GPU 实例）。

对应 tts-server/server.py 的：
  GET  /api/health
  POST /api/synthesize   同步单段合成（阻塞，最长 300s）
"""

from __future__ import annotations

import httpx

from .base import EMO_VECTOR_ORDER, SegmentRequest, VoiceRef


class IndexttsLocalEngine:
    name = "indextts_local"

    def __init__(self, tts_url: str, client: httpx.AsyncClient):
        self.tts_url = tts_url.rstrip("/")
        self.client = client

    async def health(self) -> bool:
        try:
            resp = await self.client.get(f"{self.tts_url}/api/health", timeout=5.0)
            if resp.status_code != 200:
                return False
            return bool(resp.json().get("model_loaded", True))
        except Exception:
            return False

    def _emotion_vector(self, emotion_label: str | None) -> list[float]:
        """统一标签 → IndexTTS2 8 维向量（mode 2）。

        向量顺序必须是 EMO_VECTOR_ORDER（podcast_engine.py:26），
        neutral = 全零向量（跟随语气）。
        """
        vec = [0.0] * 8
        if emotion_label and emotion_label != "neutral" and emotion_label in EMO_VECTOR_ORDER:
            vec[EMO_VECTOR_ORDER.index(emotion_label)] = 1.0
        return vec

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        voice_path = req.voice.tts_path or req.voice.local_path
        if not voice_path:
            raise ValueError(f"音色缺少参考音频路径: {req.voice.display_name!r}")
        payload = {
            "voice": voice_path,
            "text": req.text,
            "emotion": {
                "mode": 2 if req.emotion_label else 0,
                "vector": self._emotion_vector(req.emotion_label),
                "weight": 1.0 if req.emotion_label else 0.65,
                "random": False,
            },
            "params": {"speed": req.speed},
        }
        resp = await self.client.post(f"{self.tts_url}/api/synthesize", json=payload, timeout=300.0)
        if resp.status_code != 200:
            raise RuntimeError(f"自建引擎合成失败 HTTP {resp.status_code}: {resp.text[:500]}")
        # /api/synthesize 返回 JSON（output_filename），音频需再经 /api/audio/{f} 取回。
        output_filename = resp.json().get("output_filename")
        if not output_filename:
            raise RuntimeError(f"自建引擎未返回 output_filename: {resp.text[:300]}")
        audio = await self.client.get(f"{self.tts_url}/api/audio/{output_filename}", timeout=120.0)
        if audio.status_code != 200:
            raise RuntimeError(f"自建引擎音频取回失败 HTTP {audio.status_code}")
        return audio.content
