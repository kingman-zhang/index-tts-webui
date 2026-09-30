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
import asyncio
import logging
import mimetypes
import os
import time
import io
import wave
import shutil
import subprocess
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

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
    # 音色归属（任务里的 member_id）：**只用于本地引擎按需上传时的命名隔离**。
    # 预设音色与 BreezeBlue 是全员共享（同名即同内容），用户自上传音色则是独有的，
    # 不同用户可能各有一个同名但内容不同的文件 —— 上传到 tts-server 时用
    # `{音色名}__{owner_id}` 区分，避免互相覆盖（见 engines/voice_sync.py）。
    # None = 未知/未登录（隔离未开启），此时退化为按原名上传（与旧行为一致）。
    owner_id: Optional[str] = None


@dataclass
class SegmentRequest:
    text: str
    voice: VoiceRef
    emotion_label: Optional[str] = None   # None = 跟随音色参考音频
    speed: float = 1.0
    # 调用方的「还要不要这一单」检查钩子（通常是 task.cancel_requested）。
    # 资源池在**等待租约期间**和**拿到租约但尚未提交前**都会调用它：
    # 排队中的段一旦发现用户取消，就直接抛 SynthesisCancelled 退出，
    # 不再向平台提交 —— 这里是唯一能在提交前拦住新费用的地方。
    should_cancel: Optional[Callable[[], bool]] = None


@dataclass(frozen=True)
class ResourceConfig:
    id: str
    provider: str
    api_key_env: str | None = None
    base_url: str | None = None
    max_concurrency: int = 1
    weight: float = 1.0
    tier: str = "cloud"

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.id):
            raise ValueError("资源 id 必须为安全的字母数字下划线或短横线")
        if self.tier not in ("local", "cloud") or self.max_concurrency < 1 or not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("资源 tier、并发或 weight 无效")
        if self.provider == "local":
            object.__setattr__(self, "tier", "local")
            object.__setattr__(self, "max_concurrency", 1)


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
    # 是否**原生**支持语速（把 speed 作为参数传给平台/模型）。False 表示引擎
    # 会忽略 speed（当前 302.ai 与 autodl.art 的请求体都没有 speed 字段）。
    supports_speed: bool = True
    # 语速是否已由**下层**保证生效：True = synthesize_segment 返回的音频已经
    # 就是 req.speed 的语速（原生参数生效，或由池/适配器内部补齐），
    # 因此**上层不得再变一次速** —— 否则实际语速是 speed²（2026-09-29 修的真 bug：
    # 播客链路对原生支持语速的引擎又套了一层 ffmpeg atempo）。
    # 池门面恒为 True：不支持的资源由池在规范化时用 ffmpeg atempo 补齐，
    # 每个段恰好变速一次。单引擎适配器默认 False（诚实声明：上层需自行后处理）。
    speed_guaranteed: bool = False
    # 响度是否已由**下层**保证：True = synthesize_segment 返回的音频已经做过响度
    # 归一（-16 LUFS、峰值 ≤ -1.5 dBFS），因此**上层不得再归一一次**。
    #
    # 背景（2026-09-30）：本地引擎（`/api/synthesize` → `_apply_speed`）必然归一，
    # 而 backend `podcast_runner._normalize_segment` 又归一一次 —— 两份实现的 6 个
    # 常量逐项相同，属**完全重复**：第二次测到的已是 ~-16，增益≈0，白跑一次 ffmpeg
    # 加一次重采样。**云引擎一律 False**：我们不知道它做了什么，宁可多归一次。
    # 池门面取 all()（混池时保证不了就老实做），与 supports_speed 同一写法。
    normalizes_loudness: bool = False
    # 是否支持情绪表达。False 表示合成时只能跟随参考音频。
    supports_emotion: bool = True


class NonRetryableSynthesisError(RuntimeError):
    """请求可能已经提交或计费，调用方不得自动重试。"""


class SynthesisCancelled(RuntimeError):
    """调用方在提交前取消（等待租约期间发现 cancel 标记），未产生任何平台提交。

    与 asyncio.CancelledError 的区别：这不是协程被外部 cancel，而是业务上的
    主动放弃，所以**不会被**资源池当成「取消活跃远端任务」而隔离资源 ——
    它压根还没提交过。
    """


# 统一输出规格：24kHz / 单声道 / PCM16（与 tts-server 播客引擎输出对齐）
NORM_RATE = 24000
NORM_CHANNELS = 1
NORM_SAMPLE_WIDTH = 2


def find_ffmpeg() -> Optional[str]:
    """定位 ffmpeg：PATH 优先，兜底常见安装路径（macOS homebrew / Linux 发行版）。

    为什么不能只信 shutil.which：本机与部分容器里 ffmpeg 装在 /opt/homebrew/bin
    或 /usr/local/bin 但不在 PATH 上，只查 PATH 会误判「没有 ffmpeg」而退化到
    低质量重采样。
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(cand).is_file():
            return cand
    return None


def fix_wav_header(data: bytes) -> bytes:
    """修正 ffmpeg 管道输出的 wav 头：RIFF/data size 是流式占位（0xFFFFFFFF 或 0）。

    wave 模块按头部声明的 size 读取，坏头会导致 readframes 读错帧数、
    拼接产物错乱（与 302.ai 引擎 _repair_wav 同款问题）。按实际字节数回写。
    """
    if len(data) < 44 or data[:4] != b"RIFF":
        return data
    b = bytearray(data)
    pos = 12
    while pos + 8 <= len(b):
        cid = bytes(b[pos:pos + 4])
        size = int.from_bytes(b[pos + 4:pos + 8], "little")
        remaining = len(b) - pos - 8
        if size > remaining:  # 占位/坏 size：按实际剩余字节数修正
            size = remaining
            b[pos + 4:pos + 8] = size.to_bytes(4, "little")
        if cid == b"data":
            b[4:8] = (len(b) - 8).to_bytes(4, "little")
            return bytes(b)
        pos += 8 + size + (size & 1)  # chunk 按 2 字节对齐
    return data


def _parse_wav(data: bytes) -> Optional[tuple[int, int, int, bytes]]:
    """解析 wav 为 (采样率, 声道数, 采样宽度, PCM 帧字节)；非 wav/坏头返回 None。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            return (reader.getframerate(), reader.getnchannels(), reader.getsampwidth(),
                    reader.readframes(reader.getnframes()))
    except (wave.Error, EOFError):
        return None


def _wrap_pcm16_mono(pcm: bytes, rate: int = NORM_RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setparams((NORM_CHANNELS, NORM_SAMPLE_WIDTH, rate, 0, "NONE", "not compressed"))
        writer.writeframes(pcm)
    return out.getvalue()


def _pcm_linear_normalize(rate: int, channels: int, width: int, frames: bytes) -> bytes:
    """标准库兜底：整数/浮点 PCM → 24kHz 单声道 PCM16（线性插值）。

    无 ffmpeg 时才会走到这里，日志会明确提示。线性插值没有抗混叠滤波，
    高频可能略有混叠 —— 生产环境应安装 ffmpeg。
    """
    step = channels * width
    samples: list[float] = []
    for offset in range(0, len(frames), step):
        values = []
        for channel in range(channels):
            raw = frames[offset + channel * width:offset + (channel + 1) * width]
            if len(raw) < width:
                break
            if width == 1:
                value = raw[0] - 128
            else:
                value = int.from_bytes(raw, "little", signed=True)
            values.append(value * 2 ** (16 - width * 8))
        if values:
            samples.append(sum(values) / len(values))
    if not samples:
        raise NonRetryableSynthesisError("音频为空，无法规范化")
    pcm = bytearray()
    for i in range(round(len(samples) * NORM_RATE / rate)):
        position = i * rate / NORM_RATE
        left = min(int(position), len(samples) - 1)
        right = min(left + 1, len(samples) - 1)
        value = round(samples[left] + (samples[right] - samples[left]) * (position - left))
        pcm.extend(max(-32768, min(32767, value)).to_bytes(2, "little", signed=True))
    return _wrap_pcm16_mono(bytes(pcm))


def atempo_filters(speed: float) -> list[str]:
    """把任意语速折算成 ffmpeg atempo 滤镜链（单个 atempo 只接受 0.5–2.0）。

    speed=1.0 返回空列表（= 不变速）。上层与池共用这一个入口，
    避免各处自己写 `max(0.5, min(2.0, speed))` 把用户设置的极端语速悄悄吞掉。
    """
    try:
        remaining = float(speed)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(remaining) or remaining <= 0 or abs(remaining - 1.0) < 1e-3:
        return []
    chain: list[str] = []
    while remaining > 2.0:
        chain.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        chain.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) >= 1e-3:
        chain.append(f"atempo={remaining:g}")
    return chain


def normalize_pcm(data: bytes, speed: float = 1.0) -> bytes:
    """统一为 24kHz 单声道 PCM16 WAV，并按需变速一次（speed≠1.0 时）。

    顺序刻意如此（2026-09-29 修正）：
      ① 已经是目标格式且**不需要变速** → 零转换直接返回（无损、最快，
         池里绝大多数段走这条）；
      ② 有 ffmpeg → 交给 ffmpeg 重采样（带抗混叠滤波，音质最好），
         需要变速时把 atempo 与重采样合并进**同一次** ffmpeg 调用；
      ③ 无 ffmpeg（或 ffmpeg 转换失败）→ 标准库整数 PCM 线性插值兜底，
         并**明确记 warning**，不再假装和 ffmpeg 等价。

    旧实现把低质量的线性插值放在第一优先级，等于只要有 wav 头就永远绕过
    ffmpeg —— ffmpeg 装了也用不上。

    speed 参数的语义是「**本层**要补的语速」：调用方（池）在资源已经原生
    支持语速时传 1.0，只有资源不支持时才把用户语速传进来 —— 这就是
    「语速只应用一次」在代码里的落点。

    转换失败一律抛 NonRetryableSynthesisError：音频已经计费，禁止整段重做。
    """
    atempo = atempo_filters(speed)
    parsed = _parse_wav(data)
    if parsed and parsed[:3] == (NORM_RATE, NORM_CHANNELS, NORM_SAMPLE_WIDTH) and not atempo:
        return data

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        command = [ffmpeg, "-v", "error", "-i", "pipe:0"]
        if atempo:
            command += ["-filter:a", ",".join(atempo)]
        command += ["-ar", str(NORM_RATE), "-ac", str(NORM_CHANNELS),
                    "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"]
        try:
            result = subprocess.run(command, input=data, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("[engine] ffmpeg 调用失败，回退标准库线性插值: %s", exc)
        else:
            if result.returncode == 0 and result.stdout:
                fixed = fix_wav_header(result.stdout)
                reparsed = _parse_wav(fixed)
                if reparsed:
                    frames = reparsed[3]
                    return _wrap_pcm16_mono(frames)
                logger.warning("[engine] ffmpeg 输出无法解析，回退标准库线性插值")
            else:
                logger.warning("[engine] ffmpeg 规范化失败，回退标准库线性插值: %s",
                               result.stderr.decode("utf-8", "ignore")[-200:])
    else:
        logger.warning("[engine] 未找到 ffmpeg，使用标准库线性插值重采样（高频可能混叠）；建议部署 ffmpeg")

    if not parsed:
        raise NonRetryableSynthesisError("音频非 WAV 且无法用 ffmpeg 转换；不能重试已合成段")
    if atempo:
        # 标准库兜底不做变速（线性插值变速会变调），只能明确告知语速没生效
        logger.warning("[engine] 无 ffmpeg，%.2fx 语速未能应用（该资源不支持原生变速）", speed)
    return _pcm_linear_normalize(*parsed)


class ResourcePoolEmptyError(RuntimeError):
    """资源池为空，配置必须修复后才能启动合成。"""


class EnginePoolFacade:
    """按段选择资源的引擎门面；资源租约跨任务共享。"""

    def __init__(self, resources: list[tuple[ResourceConfig, TTSEngine]], cooldown_sec: float = 120.0,
                 health_ttl: float = 15.0, cancel_poll_sec: float = 0.5):
        if not resources:
            raise ResourcePoolEmptyError("TTS 资源池不能为空")
        if len({cfg.id for cfg, _ in resources}) != len(resources):
            raise ValueError("资源 id 不得重复")
        self.resources = resources
        self.cooldown_sec = cooldown_sec
        self.health_ttl = health_ttl
        # 排队等待时的取消轮询间隔（秒）：越短取消越灵敏，越长越省 CPU
        self.cancel_poll_sec = cancel_poll_sec
        self.cooldown_until: dict[str, float] = {}
        self.health_state: dict[str, tuple[bool, float]] = {}
        # 最近一次探活失败的**原因**（成功时无键）。健康位只有一个 bool，而
        # 「连不上」与「连上了但模型没加载好」要查的地方完全不同（网络/端口 vs
        # GPU 机器上的模型加载日志）—— 2026-09-30 用户看到「全部走 art」却拿不到
        # 任何线索，就是这个 bool 吞掉的。只透出失败原因，不含 URL 与凭据。
        self.health_note: dict[str, str] = {}
        self._health_tasks: dict[str, asyncio.Task] = {}
        self._condition = asyncio.Condition()
        self._inflight: dict[str, int] = {cfg.id: 0 for cfg, _ in self.resources}
        self._failures: dict[str, int] = {cfg.id: 0 for cfg, _ in self.resources}
        self._served = {cfg.id: 0 for cfg, _ in self.resources}
        self.name = "pool"

    @property
    def capabilities(self) -> EngineCapabilities:
        """池的能力 = 池内资源的**当前**能力的保守合成。

        为什么是 property 而不是 `__init__` 里算一次的实例属性（2026-09-30 改）：
        资源的能力会随探活更新 —— `IndexttsLocalEngine._apply_capabilities` 按
        `/api/health` 的自述把 `normalizes_loudness` 从保守的 False 翻成 True。
        缓存在构造那一刻就取不到了（那时还没探过活），于是「服务自述的能力」永远
        晚一步、优化永不生效。改成每次读时重算，代价是几次列表推导，可忽略。
        """
        return self._conservative_capabilities()

    def _conservative_capabilities(self) -> EngineCapabilities:
        if not self.resources:
            return EngineCapabilities(display_name="资源池", max_concurrency=1)
        caps = [engine.capabilities for _, engine in self.resources]
        limits = [c.max_input_chars for c in caps if c.max_input_chars is not None]
        return EngineCapabilities(
            display_name="多资源 TTS 池",
            max_input_chars=min(limits) if limits else None,
            max_concurrency=sum(max(1, cfg.max_concurrency) for cfg, _ in self.resources),
            supports_speed=all(c.supports_speed for c in caps),
            supports_emotion=all(c.supports_emotion for c in caps),
            # 池保证语速：原生支持的资源直接传参，其余资源在规范化时由
            # normalize_pcm(atempo) 补齐 ⇒ 上层永远不要再变速。
            speed_guaranteed=True,
            # 池**不**做响度归一（normalize_pcm 只管重采样与变速），所以这里不能
            # 恒为 True：只有池内每个资源都保证时才算保证。混池（本地 + 云端）⇒
            # False ⇒ 上层照旧归一一次，代价是多跑一遍本地段的 ffmpeg，
            # 换来的是「不依赖运气」—— 宁可多归一次，也不要漏归。
            normalizes_loudness=all(c.normalizes_loudness for c in caps),
        )

    def _available(self, cfg: ResourceConfig) -> bool:
        health = self.health_state.get(cfg.id)
        return (
            self._inflight[cfg.id] < max(1, cfg.max_concurrency)
            and not self.in_cooldown(cfg.id)
            and health is not None
            and health[0]
            and time.monotonic() - health[1] < self.health_ttl
        )

    async def _probe(self, cfg: ResourceConfig, engine: TTSEngine) -> bool:
        try:
            if getattr(engine, "has_free_probe", True):
                # 池统一管理 TTL，恢复时不能复用适配器的旧阴性缓存。
                if hasattr(engine, "_health_ts"):
                    engine._health_ts = 0
                ok = bool(await asyncio.wait_for(engine.health(), 12))
            else:
                ok = bool(engine.token)
        except Exception:
            ok = False
        self.health_state[cfg.id] = (ok, time.monotonic())
        # 失败原因由引擎自己写（`last_health_note`），池只负责留存与透出。
        note = None if ok else getattr(engine, "last_health_note", None)
        if note:
            self.health_note[cfg.id] = note
        else:
            self.health_note.pop(cfg.id, None)
        if not ok:
            self.cooldown_until[cfg.id] = time.monotonic() + self.cooldown_sec
        verdict = ("reachable" if getattr(engine, "has_free_probe", True) else "unverified") if ok else "unavailable"
        logger.info("[engine] resource=%s probe=%s%s", cfg.id, verdict,
                    f" —— {note}" if note else "")
        return ok

    async def _refresh_health(self) -> None:
        now = time.monotonic()
        probes = []
        for cfg, engine in self.resources:
            cached = self.health_state.get(cfg.id)
            if self.in_cooldown(cfg.id):
                continue
            if cached and cached[0] and now - cached[1] < self.health_ttl:
                continue
            task = self._health_tasks.get(cfg.id)
            if task is None or task.done():
                task = asyncio.create_task(self._probe(cfg, engine))
                self._health_tasks[cfg.id] = task
            probes.append(task)
        if probes:
            await asyncio.gather(*(asyncio.shield(task) for task in probes))

    def in_cooldown(self, resource_id: str) -> bool:
        until = self.cooldown_until.get(resource_id)
        if until is None:
            return False
        if time.monotonic() >= until:
            self.cooldown_until.pop(resource_id, None)
            return False
        return True

    async def _lease(self, should_cancel: Optional[Callable[[], bool]] = None
                     ) -> tuple[ResourceConfig, TTSEngine]:
        async with self._condition:
            while True:
                if should_cancel is not None and should_cancel():
                    raise SynthesisCancelled("等待资源期间收到取消，未提交任何合成请求")
                await self._refresh_health()
                candidates = [(cfg, engine) for cfg, engine in self.resources if self._available(cfg)]
                if candidates:
                    if should_cancel is not None and should_cancel():
                        raise SynthesisCancelled("取得资源后、提交前收到取消，未提交任何合成请求")
                    # 本地优先；云端比较 capacity 归一化负载，并用轮转序号打破完全相同的 tie。
                    local = [item for item in candidates if item[0].tier == "local"]
                    candidates = local or candidates
                    candidates.sort(key=lambda item: (
                        self._inflight[item[0].id] / item[0].max_concurrency / item[0].weight,
                        self._served[item[0].id] / item[0].weight,
                    ))
                    cfg, engine = candidates[0]
                    self._served[cfg.id] += 1
                    self._inflight[cfg.id] += 1
                    return cfg, engine
                if all(self.in_cooldown(cfg.id) for cfg, _ in self.resources):
                    raise RuntimeError("所有 TTS 资源均处于冷却中")
                # 带超时等待：只要用户在该段排队期间点了取消，最多 0.5s 就能跳出
                # 等待并抛 SynthesisCancelled —— 这是「提交前拦住新费用」的关键。
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=self.cancel_poll_sec)
                except asyncio.TimeoutError:
                    pass

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        cfg, engine = await self._lease(req.should_cancel)
        # 语速只应用一次，且由**实际被选中的那个资源**的能力决定：
        #   原生支持 → 交给引擎（speed 传进去，规范化不再变）；
        #   不支持 → 引擎忽略 speed，由本池在 normalize_pcm 里用 atempo 补齐。
        # 混池（本地原生 + 云端不支持）下两种资源混在一篇里也各自恰好一次。
        native_speed = bool(engine.capabilities.supports_speed)
        try:
            result = await engine.synthesize_segment(req)
            self._failures[cfg.id] = 0
        except asyncio.CancelledError:
            # HTTP 取消不代表远端任务终止，隔离一段时间避免立刻再提交。
            self.cooldown_until[cfg.id] = time.monotonic() + self.cooldown_sec
            raise
        except Exception as exc:
            self.cooldown_until[cfg.id] = time.monotonic() + self.cooldown_sec
            self.health_state.pop(cfg.id, None)
            self.health_note.pop(cfg.id, None)   # 与健康位同生共死，别留下过期原因
            logger.warning("[engine] resource=%s 合成失败，隔离 %.0fs，不自动重提交", cfg.id, self.cooldown_sec)
            raise NonRetryableSynthesisError(f"资源 {cfg.id} 合成失败，提交状态可能未知，禁止自动重试") from exc
        finally:
            async with self._condition:
                self._inflight[cfg.id] -= 1
                self._condition.notify_all()
        post_speed = 1.0 if native_speed else float(req.speed or 1.0)
        try:
            return await asyncio.to_thread(normalize_pcm, result, post_speed)
        except Exception as exc:
            raise NonRetryableSynthesisError("已取得音频但规范化失败，禁止自动重新合成") from exc

    async def health(self) -> bool:
        await self._refresh_health()
        return any(not self.in_cooldown(cfg.id) and self.health_state.get(cfg.id, (False, 0))[0]
                   for cfg, _ in self.resources)

    def resource_snapshot(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "id": cfg.id,
                "provider": cfg.provider,
                "tier": cfg.tier,
                "max_concurrency": cfg.max_concurrency,
                "weight": cfg.weight,
                "inflight": self._inflight[cfg.id],
                "in_cooldown": self.in_cooldown(cfg.id),
                "health": ("unavailable" if self.in_cooldown(cfg.id) else
                           "unverified" if not getattr(engine, "has_free_probe", True) else
                           "reachable" if self.health_state.get(cfg.id, (False, 0))[0] else "unknown"),
                "health_age_sec": round(now - (self.health_state.get(cfg.id) or (None, now))[1], 2),
                # 探活失败的原因（成功时为 None）。**只放引擎自述的短句**：
                # 不含 endpoint URL、凭据、响应正文 —— 脱敏契约不变。
                "health_note": self.health_note.get(cfg.id),
            }
            for cfg, engine in self.resources
        ]


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
    """资源池注册表 + 进程级熔断状态。

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

    def register_pool(self, resources: list[tuple[ResourceConfig, TTSEngine]]) -> EnginePoolFacade:
        pool = EnginePoolFacade(resources)
        self.engines.append(pool)
        return pool

    def mark_failed(self, engine_name: str) -> None:
        """把引擎置入冷却 —— 后续任务在选择阶段跳过它。"""
        for engine in self.engines:
            if isinstance(engine, EnginePoolFacade):
                if engine.name == engine_name:
                    logger.warning("[engine] 忽略 pool 整体熔断，请由资源租约按资源隔离")
                    return
                continue
            if engine.name == engine_name:
                self.cooldown_until[engine_name] = time.monotonic() + self.cooldown_sec
                return

    def in_cooldown(self, engine_name: str) -> bool:
        if isinstance(engine_name, EnginePoolFacade):
            return not any(not engine_name.in_cooldown(cfg.id) for cfg, _ in engine_name.resources)
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
            if isinstance(engine, EnginePoolFacade):
                return engine
            try:
                if await engine.health():
                    return engine
            except Exception:  # noqa: BLE001 - 探活失败即视为不可用
                continue
        raise RuntimeError("所有 TTS 引擎均不可用")
