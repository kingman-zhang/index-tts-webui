"""302.ai 托管 IndexTTS-2 适配器（备援：原生情绪向量 + 按 token 计费）。

真机实测结论（2026-09-18，q_f497bf800e 全 13 段 7/7 通过）：
  - 端点：国内接入点 **https://api.302ai.com**（官方文档写 api.302.ai，
    两者同源；实测 .com/.cn 均可用，默认 .com）。
  - **emotion_vector 8 维原生情绪向量可用**：one-hot 对齐 EMO_VECTOR_ORDER
    即生效（happy 探针 ASR 验证）。这是相对 SiliconFlow CosyVoice2
    （提示词 hack）的核心优势。
  - 输出 wav 22050Hz/16bit/mono，头部规范无需修复；拼接链路要求任务内
    单引擎（mono_runner 已保证），与自建 24kHz 不可混拼。
  - 计费：0.015 PTC/1k tokens（1 PTC=$1），实测 2.09 token/字符
    ≈ ¥2.6/万汉字；参考音频上传 0.001 PTC/次（结果缓存复用）。

接口事实（官方文档 doc.302.ai/349711205e0 + 真机核实）：
  POST {base}/302/upload-file      multipart 上传任意文件 → {"code":200,"data":"https://file.302.ai/..."}
                                   （0.001 PTC/次，50MB 上限）
  POST {base}/302/index_tts2/task  提交合成任务 → {"task_id": "..."}
       字段: text / speaker_audio_url(公网 URL) / emotion_vector(8 维) /
             emotion_audio_url / emotion_alpha
  GET  {base}/302/index_tts2/task?task_id=...
       → {"state":"PENDING|STARTED|SUCCESS|...","audio_url":...,
          "duration","total_text_tokens","sample_rate":22050,"generation_time"}
  无 speed 参数、无采样参数（top_p 等）——SegmentRequest.speed 忽略。
  未知 state 一律视为进行中继续轮询，显式失败态才报错。

音频下载域 fallback（2026-09-18 实测）：
  audio_url 指向 file.302.ai；部分网络环境（WorkBuddy 沙箱代理）会掐断该域，
  国内镜像 file.302ai.cn / file.302ai.com 可取回同一路径文件。
  引擎按「原 URL → .cn 镜像 → .com 镜像」顺序降级下载。

引擎行为约定（与 base.py 协议对齐）：
  - 音色解析顺序：voice_map 显式映射（display_name 或路径 → 公网 URL）→
    本地缓存（cache_path JSON，键=路径+大小+mtime）→ 参考音频在 backend
    侧可读时自动上传并缓存 → 文件不可读时按文件名在 voice_map/缓存中
    复用同名参考音频的已上传 URL。都不命中才报 ValueError。
  - 情绪：8 标签 → 带幅度 one-hot 向量（EMOTION_AMPLITUDE 按标签温和化，
    one-hot 1.0 实测偏激动）；neutral/None 不传 emotion_vector（跟随音色）。
  - 停顿：[pause:x] 已由 backend split_by_pauses 切分，本引擎只收纯文本。
  - 轮询：2s 间隔，单段超时 300s。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from .base import EMO_VECTOR_ORDER, SegmentRequest

DEFAULT_BASE_URL = "https://api.302ai.com"
SAMPLE_RATE = 22050  # 平台固定输出采样率
MAX_INPUT_CHARS = 2000  # 官方未文档化上限；backend 停顿切分后单段远小于此
HEALTH_TTL = 300.0
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 300.0
# file.302.ai 直连失败时的下载镜像（host 替换，路径不变）
AUDIO_MIRRORS = ("file.302ai.cn", "file.302ai.com")

# 轮询中判定为终态失败的 state（其余一律视为进行中）
_FAILED_STATES = {"FAILURE", "FAILED", "ERROR", "CANCELLED", "TIMEOUT"}

# 情绪幅度表（0-1）。one-hot 1.0 实测情绪偏激动（用户听感：喜悦/惊喜过烈），
# 按标签给不同幅度做温和化；纯经验值，待听感反馈继续微调。
# 顺序对齐 EMO_VECTOR_ORDER；未列出的标签用 DEFAULT_AMPLITUDE。
DEFAULT_AMPLITUDE = 0.8
EMOTION_AMPLITUDE: dict[str, float] = {
    "happy": 0.75,
    "surprised": 0.75,
    "angry": 0.7,
    "sad": 0.7,
    "afraid": 0.7,
    "disgusted": 0.7,
    "melancholic": 0.6,
    "calm": 0.5,
}

# 提交/上传的连接级重试：仅针对"连接未建立"类错误（请求确定没到服务器，
# 重试不会重复计费）。读超时/响应中断不重试提交（可能已提交，重试会重复扣费），
# 由调用方 queue_worker 的 interrupted 状态兜底。
_CONNECT_RETRIES = 3
_CONNECT_RETRY_BASE_S = 1.5


class Indextts302aiEngine:
    name = "indextts_302ai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        cache_path: str | Path | None = None,
        voice_map: dict[str, str] | None = None,
        extra_params: dict | None = None,
    ):
        self.api_key = api_key or os.environ.get("INDEXTTS302_API_KEY", "")
        self.base_url = (base_url or os.environ.get("INDEXTTS302_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
        self.cache_path = Path(cache_path) if cache_path else None
        # 显式映射：键可为 display_name / local_path / tts_path → 公网 URL
        self.voice_map = dict(voice_map or {})
        self.extra_params = dict(extra_params or {})
        self._cache: dict = {}
        self._cache_loaded = False
        # 并发合成下防止多段同时首传同一参考音频（重复扣上传费）
        self._voice_lock = asyncio.Lock()
        self._health_ok: bool | None = None
        self._health_ts = 0.0
        self._speed_warned = False
        self.poll_timeout = POLL_TIMEOUT  # 单段轮询超时（测试可调小）

    # ---------- health ----------

    async def health(self) -> bool:
        """轻量探活：带 key 查询一个不存在的 task_id。

        200（state=PENDING）= 连通且鉴权有效；401/403 = 不可用。
        """
        if not self.api_key:
            return False
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_ts < HEALTH_TTL:
            return self._health_ok
        try:
            resp = await self.client.get(
                f"{self.base_url}/302/index_tts2/task",
                headers=self._headers(),
                params={"task_id": "00000000-0000-0000-0000-000000000000"},
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

    async def _upload_voice(self, path: Path) -> str:
        """上传参考音频获取公网 URL（0.001 PTC/次，调用方负责缓存）。"""
        with path.open("rb") as f:
            resp = await self._post_with_connect_retry(
                f"{self.base_url}/302/upload-file",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (path.name, f)},
                timeout=120.0,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"302.ai 音频上传失败 HTTP {resp.status_code}: {resp.text[:400]}")
        data = (resp.json() or {}).get("data")
        if not data or not str(data).startswith("http"):
            raise RuntimeError(f"302.ai 上传未返回 URL: {resp.text[:300]}")
        return data

    @staticmethod
    def _key_basename(key: str) -> str:
        """voice_map 键或缓存键（{path}:{size}:{mtime_ns}）→ 文件名部分。"""
        raw = key.rsplit(":", 2)[0] if key.count(":") >= 2 else key
        return Path(raw).name

    async def _resolve_voice_url(self, req: SegmentRequest) -> str:
        # 并发合成下加锁：同一音色只允许一个协程做"查缓存→上传→写缓存"，
        # 其余等锁后直接命中缓存，避免重复上传计费
        async with self._voice_lock:
            return await self._resolve_voice_url_locked(req)

    async def _resolve_voice_url_locked(self, req: SegmentRequest) -> str:
        voice = req.voice
        # 1) 显式映射（display_name / 两个路径字段都试）
        for k in (voice.display_name, voice.local_path, voice.tts_path):
            if k and k in self.voice_map:
                return self.voice_map[k]
        # 2) 本地缓存
        self._load_cache()
        path = self._source_path(voice)
        if path is not None:
            key = self._cache_key(path)
            if key in self._cache:
                return self._cache[key]
            # 3) 自动上传并缓存
            url = await self._upload_voice(path)
            self._cache[key] = url
            self._save_cache()
            return url
        # 4) 兜底：文件在 backend 侧不可读（典型：任务带着旧部署机器的路径，
        #    如 /root/autodl-tmp/...），按文件名在 voice_map / 缓存中找同名
        #    参考音频，复用已上传的 URL（不重复花上传费）
        base = Path(voice.local_path or voice.tts_path or voice.display_name or "").name
        if base:
            for source, mapping in (("voice_map", self.voice_map), ("缓存", self._cache)):
                for k, v in mapping.items():
                    if str(v).startswith("http") and self._key_basename(k) == base:
                        return v
        raise ValueError(
            f"302.ai 备援需要参考音频在 backend 侧可读（speaker_audio_url 要求公网 URL），"
            f"或在 voice_map 中预映射 URL（音色: {voice.display_name!r}，"
            f"local_path={voice.local_path!r}，tts_path={voice.tts_path!r}）"
        )

    # ---------- 合成 ----------

    @staticmethod
    def _emotion_vector(label: str | None) -> list[float] | None:
        """8 标签 → 带幅度的 one-hot 向量；neutral/None → 不传（跟随音色）。"""
        if not label or label == "neutral" or label not in EMO_VECTOR_ORDER:
            return None
        amp = EMOTION_AMPLITUDE.get(label, DEFAULT_AMPLITUDE)
        return [amp if i == EMO_VECTOR_ORDER.index(label) else 0.0 for i in range(8)]

    async def _post_with_connect_retry(self, url: str, **kwargs) -> httpx.Response:
        """POST 带连接级重试（仅 ConnectError/ConnectTimeout——请求未到达服务器，
        重试不产生重复计费；其他异常原样抛出）。"""
        last: Exception | None = None
        for attempt in range(_CONNECT_RETRIES):
            if attempt:
                await asyncio.sleep(_CONNECT_RETRY_BASE_S * attempt)
            try:
                return await self.client.post(url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last = e
        raise RuntimeError(f"302.ai 连接失败（已重试 {_CONNECT_RETRIES - 1} 次）: {last}")

    async def _submit(self, payload: dict) -> str:
        resp = await self._post_with_connect_retry(
            f"{self.base_url}/302/index_tts2/task",
            headers=self._headers(),
            json=payload,
            timeout=60.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"302.ai 任务提交失败 HTTP {resp.status_code}: {resp.text[:400]}")
        data = resp.json() or {}
        task_id = data.get("task_id")
        if not task_id:
            raise RuntimeError(f"302.ai 提交无 task_id: {json.dumps(data, ensure_ascii=False)[:300]}")
        return task_id

    async def _poll_once(self, task_id: str) -> dict:
        resp = await self.client.get(
            f"{self.base_url}/302/index_tts2/task",
            headers=self._headers(),
            params={"task_id": task_id},
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"302.ai 轮询失败 HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() or {}

    async def _download_audio(self, audio_url: str) -> bytes:
        """按「原 URL → 镜像」顺序下载；连接失败时降级 host。"""
        candidates = [audio_url]
        for mirror in AUDIO_MIRRORS:
            if "//file.302.ai/" in audio_url:
                candidates.append(audio_url.replace("//file.302.ai/", f"//{mirror}/"))
        last: Exception | None = None
        for url in candidates:
            try:
                resp = await self.client.get(url, timeout=120.0)
                if resp.status_code == 200 and resp.content:
                    return resp.content
                last = RuntimeError(f"下载失败 HTTP {resp.status_code}: {url[:120]}")
            except httpx.HTTPError as e:
                last = e
                continue
        raise RuntimeError(f"302.ai 音频下载失败（含镜像重试）: {last}")

    async def _wait_result(self, task_id: str, timeout: float | None = None) -> dict:
        timeout = float(timeout or self.poll_timeout)
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        last_state = ""
        poll_failures = 0  # 连续网络失败计数（瞬时断连容错，GET 幂等可安全重试）
        while loop.time() - t0 < timeout:
            try:
                data = await self._poll_once(task_id)
                poll_failures = 0
            except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                poll_failures += 1
                if poll_failures >= 5:
                    raise RuntimeError(f"302.ai 轮询连续断连 {poll_failures} 次: {e}") from e
                await asyncio.sleep(POLL_INTERVAL)
                continue
            state = str(data.get("state", "") or "").upper()
            if state != last_state:
                last_state = state
            if state == "SUCCESS":
                if not data.get("audio_url"):
                    raise RuntimeError(f"302.ai SUCCESS 但无 audio_url: {json.dumps(data, ensure_ascii=False)[:300]}")
                return data
            if state in _FAILED_STATES:
                raise RuntimeError(f"302.ai 任务失败 state={state}: {json.dumps(data, ensure_ascii=False)[:300]}")
            await asyncio.sleep(POLL_INTERVAL)
        raise TimeoutError(f"302.ai 任务轮询超时 {timeout:.0f}s（最后 state={last_state or 'N/A'}）")

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        if not self.api_key:
            raise RuntimeError("302.ai 引擎缺少 API Key（INDEXTTS302_API_KEY）")
        if len(req.text) > MAX_INPUT_CHARS:
            raise ValueError(
                f"302.ai 单次输入超长（{len(req.text)} > {MAX_INPUT_CHARS}），请走 chunker"
            )
        if req.speed and abs(float(req.speed) - 1.0) > 1e-6 and not self._speed_warned:
            self._speed_warned = True  # 平台无 speed 参数，仅提示一次
        voice_url = await self._resolve_voice_url(req)
        payload: dict = {"text": req.text, "speaker_audio_url": voice_url}
        vec = self._emotion_vector(req.emotion_label)
        if vec is not None:
            payload["emotion_vector"] = vec
        payload.update(self.extra_params)  # 透传 emotion_alpha 等（按需）
        task_id = await self._submit(payload)
        result = await self._wait_result(task_id)
        audio = await self._download_audio(result["audio_url"])
        return audio
