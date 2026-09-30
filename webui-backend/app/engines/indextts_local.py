"""自建 tts-server 适配器（IndexTTS 2.0，GPU 实例）。

对应 tts-server/server.py 的：
  GET  /api/health
  POST /api/synthesize   同步单段合成（阻塞，最长 300s）
  GET  /api/audio/{f}    取回音频字节

**为什么要二次下载**：/api/synthesize 返回的是 JSON（output_filename），
不是音频字节。这层差异在本适配器内部消化掉，上层拿到的仍是 bytes ——
与云引擎同构。这就是「自建服务缺接口」的现状：缺的是一个直接返回音频的
端点，补齐后本适配器可以退化为一次请求。

**为什么提交前要过一次 `voice_sync`**：tts-server 只认**服务器本地路径**，
而 backend 的「音色选择」与「服务器上有什么文件」是两份状态。新上线一台
服务器、或任务里存着陈旧路径时，就会 400「参考音频不存在」。本适配器在
提交前确保服务器有这个文件（缺了就按需上传），把三种路径形态在引擎层统一掉。
"""

from __future__ import annotations

from dataclasses import replace

import httpx

from ..config import logger
from . import voice_sync
from .base import EMO_VECTOR_ORDER, EngineCapabilities, SegmentRequest


class IndexttsLocalEngine:
    name = "indextts_local"
    capabilities = EngineCapabilities(
        display_name="自建 tts-server（IndexTTS-2，GPU）",
        # 不限：整段文本交给 tts-server 侧的播客引擎自行分段
        max_input_chars=None,
        # GPU 实例串行推理；原先靠上层判断 `engine_name == "indextts_local"` 得到，
        # 现在改成引擎自己声明
        max_concurrency=1,
        supports_speed=True,      # payload.params.speed
        supports_emotion=True,    # mode 2 的 8 维情绪向量
        # 响度归一：**由服务自述，不写死**（见 _apply_capabilities）。
        # 默认 False = 保守（上层照旧归一，不会错，只多跑一次 ffmpeg）。
        normalizes_loudness=False,
    )

    def __init__(self, tts_url: str, client: httpx.AsyncClient):
        self.tts_url = tts_url.rstrip("/")
        self.client = client
        # 最近一次探活失败的原因（成功时 None）。由池读取并透出到 /api/version。
        # 为什么需要它：健康位只有一个 bool，但「连不上」和「连上了、模型没加载好」
        # 要查的地方完全不同 —— 前者查网络/端口/隧道，后者必须上 GPU 机器看加载日志。
        # 2026-09-30 用户遇到「所有合成都走 art」，而 /api/version 只说 unavailable，
        # 就是这个 bool 把关键信息吞掉的。
        self.last_health_note: str | None = None

    async def health(self) -> bool:
        try:
            # 2s 超时：tts-server 离线时 resolve() 不至于在探活上白等 5s
            resp = await self.client.get(f"{self.tts_url}/api/health", timeout=2.0)
        except Exception as exc:
            self.last_health_note = f"连接失败：{type(exc).__name__}"
            return False
        if resp.status_code != 200:
            self.last_health_note = f"HTTP {resp.status_code}"
            return False
        try:
            data = resp.json()
        except Exception:
            self.last_health_note = "响应不是合法 JSON（是不是连到了别的服务？）"
            return False
        self._apply_capabilities(data)
        if not bool(data.get("model_loaded", True)):
            # 关键分支：tts-server 的模型加载**失败不会退进程**（server.py:108-111
            # 捕获异常后把 tts 置 None），而且加载是**同步阻塞在 uvicorn 启动之前**的
            # ⇒ 端口既然能通，加载那一步必然已经跑完。所以 status=no_model 读作
            # 「加载失败」，而不是「还在加载」。
            self.last_health_note = (
                f"模型未加载（status={data.get('status') or 'no_model'}）"
                " —— 端口是通的，去该服务器看启动日志里的 model load failed"
            )
            return False
        self.last_health_note = None
        return True

    def _apply_capabilities(self, health: dict) -> None:
        """按服务**自述**更新本适配器的能力（2026-09-30）。

        为什么 `normalizes_loudness` 不能写死在类属性里：**同一个适配器连的可能是两个
        不同的壳** —— 仓库里有 `tts-server/`（IndexTTS 2.0：`/api/synthesize` 必经
        `_apply_speed`，做 ebur128 → 固定增益 → alimiter 的 -16 LUFS 归一，自述 True）
        和 `tts-server-2.5/`（IndexTTS 2.5：每段跑一次**单遍** `_apply_loudness`，
        但单遍 loudnorm 因门限效应达不到 -16、实测只到 -21.7 ⇒ 自述 False，让上层
        兜底再归一一次）。TTS_URL 指向哪台，能力就不同，静态声明必然有一边是错的：
        若写 True 而实际连了 2.5，上层就会跳过归一 ⇒ 那段响度停在 -21.7、音色之间
        音量不齐（静默的回归）。

        所以让服务在 `/api/health` 里自报；自报缺失或类型不对就**保持保守值 False**
        （上层照旧归一 —— 多跑一次 ffmpeg，但绝不会漏）。
        """
        value = health.get("normalizes_loudness")
        if not isinstance(value, bool) or value == self.capabilities.normalizes_loudness:
            return
        self.capabilities = replace(self.capabilities, normalizes_loudness=value)
        logger.info("[engine] indextts_local 响度归一能力更新为 %s（服务自述）", value)

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
        # 提交前确保服务器上有这个参考音频（缺失就从 backend 按需上传）。
        # 返回的路径来自服务器自身的 /api/voices，因此不再受 backend cwd 与
        # 路径形态影响 —— 新上线的服务器、陈旧相对路径都在这一层被统一。
        voice_path = await voice_sync.ensure_voice_on_server(self.client, self.tts_url, req.voice)
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
