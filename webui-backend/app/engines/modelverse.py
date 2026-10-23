"""Modelverse（Compshare 算力平台）托管 IndexTTS-2 适配器。

## 为什么是 api.modelverse.cn，而不是 cp.compshare.cn/audio/v1

Compshare 上有**两个互不相通**的音频入口，同一把 Key 只能进其中一个
（2026-10-22 实测：拿 modelverse 的 Key 打 `cp.compshare.cn/audio/v1/pricing`
返回 `401 {"error":"invalid api key"}`，而打 `api.modelverse.cn/v1/models` 是 200）：

| | **api.modelverse.cn** ★ 本适配器 | cp.compshare.cn/audio/v1 |
|---|---|---|
| 模型 | IndexTTS-**2** | IndexTTS-**2.5** |
| 音色 | `spk_audio_file` 每次直接带参考音频 | 需先建自定义音色（可持久） |
| 预上传 | **不需要** ⇒ 没有 7 天过期问题 | 需要 |
| 单次上限 | 600 字符 | 2000 字符 |
| 计费 | 40 元/百万 token（1 token = 1 字符） | 按积分/计费字符 |
| 凭据 | `sk-…`（model_key 那套） | `sk-ml-…` |

选左边的原因只有一个：**形态和 art 一样（每段直接带参考音频）**，接进资源池
不需要「先上传、再等着过期、过期了补传」这一整套状态机。

## 接口事实（全部实测，2026-10-22）

  POST {base}/v1/audio/infer   multipart/form-data，返回 WAV 字节流
      model          = "IndexTeam/IndexTTS-2"
      payload        = JSON 字符串（见下）
      spk_audio_file = 参考音频文件（**必填**，零样本克隆）
      emo_audio_file = 情感参考音频（method=1 时才用，本适配器不用）
  GET  {base}/v1/models/{model} → 200 + 价目（**免费**，用作探活）

payload 字段（**文档示例里的 `emo_weight` 是过时的，真实参数名是 `emo_alpha`**）：
  input            合成文本，≤600 字符（**按 Unicode 字符计**，一个汉字算 1）
  sample_rate      实测 22050 / 24000 都收，且**原样生效**
  speed            0.25~4.0。**实测真生效**：同一段文字 1.0 → 1.5 时输出时长
                   6.94s → 4.33s（0.624×，理论 0.667×，差值是首尾静音被算进来）
  interval_silence 句间静音 ms（默认 200；本适配器沿用）
  emo_control_method  0=不控情感 1=情感音频 2=情感向量 3=情感文本
  emo_vec          8 维，和 ≤1.5，仅 method=2
  emo_alpha        情感向量融合权重
  gain             音量倍数（不用，响度归一在池门面统一做）

实测性能（`tools/test_compshare_live.py`）：3.25s 与 7.18s 的参考音频都能用
（文档写 5~30s，**但 3s 也过**）⇒ 仓库 preset-voices 里 52 个样本全可用，不必补录。

## 成本（口径已由控制台调用日志锁定，见 tools/test_compshare_live.py 文件头）

1 token = 1 个字符（标点也计费）⇒ 40 元/百万 token = **¥0.40/万字**、
**¥0.69~0.86/音频小时**（按实测 289~357 字/分）。art 是 ¥1.84/万字 ⇒ 便宜 4.6 倍。
注意文档「注意事项」还写着「TTS 限时免费」，若真免费属于**额外收益**，规划仍按 ¥0.40/万字。

## 与上层协议的对齐

  - **单次上限 600**：`max_input_chars=600`，由 chunker 按引擎声明的能力切片。
    ⚠️ 已知口径错位（2026-10-22 复核）：chunker 是「1 汉字 = 2 单位」（art 的历史
    口径），而这里是「1 字符 = 1 token」⇒ 声明 600 后实际按 **≈300 汉字/段** 切。
    根因是 `EnginePoolFacade._conservative_capabilities` 对 `max_input_chars`
    直接 `min()`，不同口径的数字被当同一量纲比较（苹果比橘子）。

    ⚠️ **不是「混池只能按最严口径切」**（早期版本的注释如此断言，算术是错的）：
    若声明带上量纲（art 2048/2=1024 汉字、302ai 2000/2=1000、sf 2048/2=1024、
    本引擎 600/1=600），池取 min = **600 汉字/段**；而 600 汉字落到 art 只占
    1200 单位（≤2048）、302ai 1200（≤2000）——**全都安全**。即混池也能切满，
    前提是按「汉字数」折算而不是比原始数字。

    未修的原因只是**范围**（接入引擎不改池的聚合规则），不是做不到。要修的最小
    形态：能力声明加一个 `units_per_cjk`（或 `max_input_chars` 改成带量纲的元组）
    + 池聚合折算，`split_for_art` 加一个 basis 参数，共约 25 行、不动 runner。
  - **语速**：原生生效 ⇒ `supports_speed=True`，池门面据此不再套 ffmpeg atempo
    （否则实际语速会变成 speed²）。
  - **情感**：8 标签 → `emo_control_method=2` + one-hot 向量。幅度表**复用 302.ai
    那份**（同一个 IndexTTS-2 底座、同一套 8 维语义，「one-hot 1.0 听感偏激动」
    的结论两边同时成立）—— 复制一份表迟早漂移，所以直接 import。
    neutral / None → **不带任何 emo 字段**，让平台走默认（跟随参考音频）。
  - **输出格式**：直接要 24000Hz/单声道 ⇒ 与 `NORM_RATE` 一致，池门面
    `normalize_pcm` 零转换直通（省掉每段一次 ffmpeg 重采样）。
    ⚠️ 但**不能只看 Content-Type 判成功**：平台返回的是 RIFF/WAV，头部却谎报
    `audio/mpeg`（302.ai 的下载域也有类似毛病）⇒ 只认字节，不认头。
  - **停顿标记**：`[pause:N]` 已由 backend `split_by_pauses` 切走并转成段间静音，
    正常路径收不到。这里仍做一次兜底替换（→ 全角逗号）：万一有旁路直接调
    `synthesize_segment`，宁可读成一个短停顿，也不要让模型把 "[pause:1]" 念出来
    还照样计费。
  - **错误归因**：参考音频缺失 / 超长 —— 都抛 `EngineInputError`（不是 ValueError），
    这样资源池**不会因为一份坏音色或一次超长输入把整个平台冷却 120s**
    （`base.ENGINE_INPUT_ERRORS` 的约定）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx

from .base import (
    EMO_VECTOR_ORDER,
    EngineCapabilities,
    EngineInputError,
    RetryableResourceError,
    SegmentRequest,
)
# 情绪幅度表复用 302.ai 那份（同一个 IndexTTS-2 底座、同一套 8 维向量语义）。
# 复制一份表迟早两边漂移，所以只留一个来源：改幅度两边同时生效。
from .indextts_302ai import DEFAULT_AMPLITUDE, EMOTION_AMPLITUDE

DEFAULT_BASE_URL = "https://api.modelverse.cn"
MODEL = "IndexTeam/IndexTTS-2"

# 平台硬上限：**600 个 Unicode 字符**（不是 art 那种「1 汉字算 2」的口径）
MAX_INPUT_CHARS = 600
# 直接要 24kHz：与 base.NORM_RATE 一致 ⇒ 池里不做重采样（实测该值被原样支持）
SAMPLE_RATE = 24000
# 句间静音 ms。句间停顿由平台按标点自己断句时使用；段间/行间静音是 backend
# 的 split_by_pauses + gap_ms 干的，跟这个参数不是一回事。
INTERVAL_SILENCE_MS = 200
# 情感向量融合权重：1.0 = 完全按传入向量（幅度已在 EMOTION_AMPLITUDE 里温和化过）
EMO_ALPHA = 1.0

HEALTH_TTL = 300.0
# 单段 300s：600 字实测 ~41s 墙钟（RTF 0.3~0.6）；但语速调到 0.25× 时
# 音频要长 4 倍 ⇒ 留足余量，宁可不卡在超时上（非幂等请求，超时=白花钱）
REQUEST_TIMEOUT = 300.0
# 提交的连接级重试：只重试「连接没建立」类错误（请求确定没到服务器，重试不重复计费）。
# 读超时/响应中断**一律不重试** —— 那时平台可能已经接单并计费。
CONNECT_RETRIES = 3
CONNECT_RETRY_BASE_S = 1.5

# 停顿标记兜底（正常路径已被 split_by_pauses 切走，见模块 docstring）
_PAUSE_RE = re.compile(r"\[pause:[0-9]*\.?[0-9]+\]|<#>")


class ModelverseEngine:
    name = "indextts_modelverse"
    capabilities = EngineCapabilities(
        display_name="Modelverse 托管 IndexTTS-2（Compshare）",
        # 600 字符；实际按 ≈300 汉字/段切，理由见模块 docstring「单次上限」
        max_input_chars=MAX_INPUT_CHARS,
        max_concurrency=None,   # 读 TTS_CONCURRENCY
        # 实测 speed 真生效（1.5 → 0.624× 时长）⇒ 池门面不得再套 atempo
        supports_speed=True,
        supports_emotion=True,  # emo_control_method=2 + 8 维向量
        # 平台对音频做过什么后处理未知 ⇒ 不承诺响度，由池门面统一归一
        normalizes_loudness=False,
    )
    # `GET /v1/models/{model}` 是免费只读端点 ⇒ 池按 TTL 正常探活。
    # （对比 art：它没有可验证的东西，只能 `has_free_probe = False` 看 Token 存在）
    has_free_probe = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ):
        # 显式传值优先（含显式 "" = 明确不要 Key），只有 None 才回退环境变量；
        # 用 `or` 会让显式 "" 被环境变量顶掉 ⇒ 测试/调试里关不掉 Key
        self.api_key = api_key if api_key is not None else os.environ.get("MODELVERSE_API_KEY", "")
        self.base_url = (base_url or os.environ.get("MODELVERSE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0))
        self.timeout = timeout
        # 探活结果缓存（TTL 内不重复打）
        self._health_ok: bool | None = None
        self._health_ts = 0.0
        # 探活失败原因：池只透出这句短话，不含 URL / 凭据 / 响应正文
        self.last_health_note: str | None = None

    # ---------- 探活 ----------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def health(self) -> bool:
        """`GET /v1/models/{model}` —— 免费、能同时验证「连通 + 凭据有效」。

        与 art 的差别：art 没有可验证的东西，只能看 Token 存在（`has_free_probe=False`）；
        这里有一个真能打的只读端点，所以探活是真探活。

        ⚠️ 探活成功**不等于**能合成：余额耗尽时价目照常返回 200，要等真正合成
        才会暴露（403/欠费），那时由池门面对该资源冷却 cooldown_sec。
        """
        if not self.api_key:
            self.last_health_note = "未配置 MODELVERSE_API_KEY"
            return False
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_ts < HEALTH_TTL:
            return self._health_ok
        try:
            resp = await self.client.get(
                f"{self.base_url}/v1/models/{MODEL}", headers=self._headers(), timeout=10.0
            )
        except Exception as exc:  # noqa: BLE001 - 探活失败即不可用，原因留给排障
            self.last_health_note = f"连接失败：{type(exc).__name__}"
            self._health_ok, self._health_ts = False, time.monotonic()
            return False
        ok = resp.status_code == 200
        if not ok:
            self.last_health_note = (
                f"HTTP {resp.status_code}" + ("（Key 无效或过期）" if resp.status_code in (401, 403) else "")
            )
        else:
            self.last_health_note = None
        self._health_ok, self._health_ts = ok, time.monotonic()
        return ok

    # ---------- 合成 ----------

    @staticmethod
    def _emotion_fields(label: str | None) -> dict:
        """8 标签 → emo_control_method=2 + one-hot 向量；neutral/None → 空（跟随音色）。"""
        if not label or label == "neutral" or label not in EMO_VECTOR_ORDER:
            return {}
        amp = EMOTION_AMPLITUDE.get(label, DEFAULT_AMPLITUDE)
        return {
            "emo_control_method": 2,
            "emo_vec": [amp if i == EMO_VECTOR_ORDER.index(label) else 0.0 for i in range(8)],
            "emo_alpha": EMO_ALPHA,
        }

    def _build_payload(self, req: SegmentRequest) -> dict:
        text = req.text
        if len(text) > MAX_INPUT_CHARS:
            # 归因为输入侧错误：不冷却资源、也不该重试（切不动是上层的事）
            raise EngineInputError(
                f"Modelverse 单次输入超长（{len(text)} > {MAX_INPUT_CHARS} 字符），应由 chunker 切片后再提交"
            )
        payload: dict = {
            "input": _PAUSE_RE.sub("，", text),
            "sample_rate": SAMPLE_RATE,
            "interval_silence": INTERVAL_SILENCE_MS,
        }
        try:
            speed = float(req.speed or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        # 平台接受 0.25~4.0；越界的值不在本地夹（会被上层钳过），
        # 真越界就让平台报错 —— 别把用户设置悄悄吞掉
        if abs(speed - 1.0) > 1e-6:
            payload["speed"] = speed
        payload.update(self._emotion_fields(req.emotion_label))
        return payload

    @staticmethod
    def _reference_bytes(req: SegmentRequest) -> tuple[bytes, str]:
        path = req.voice.local_path
        if not path:
            raise EngineInputError(
                f"Modelverse 需要 backend 侧可读的参考音频文件（音色 {req.voice.display_name!r} 没有 local_path）"
            )
        p = Path(path)
        if not p.is_file():
            raise EngineInputError(
                f"参考音频不存在：{path}（音色 {req.voice.display_name!r}）"
            )
        return p.read_bytes(), p.name

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        """从错误响应里抠出能看的那句话。

        平台的错误体有两种形状（都实测见过）：`{"error": {"message": …}}` 与
        `{"error": "invalid api key"}`，所以两种都认；都不像就截原文。
        """
        body = resp.text or ""
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001 - 不是 JSON 就原样截断
            return body[:300]
        if isinstance(data, dict):
            err = data.get("error", data.get("message", data))
            if isinstance(err, dict):
                err = err.get("message") or err.get("msg") or json.dumps(err, ensure_ascii=False)
            return str(err)[:300]
        return body[:300]

    async def _post_infer(self, data: dict, files: dict) -> httpx.Response:
        """带连接级重试的提交（只重试连接没建立的情况，见 CONNECT_RETRIES）。

        两类失败的归因**刻意分开**（2026-10-23，生产事故后修）：

          - 连接没建立（ConnectError / ConnectTimeout）：请求确定没到平台 ⇒ 重试不重复
            计费，所以就地重试 CONNECT_RETRIES 次。
          - 请求已经发出去了，但没在 REQUEST_TIMEOUT 内拿到响应（ReadTimeout 等）
            ⇒ 抛 `RetryableResourceError`，**不在本地重试**（同一台平台只会再等一个
            300s），交给池短冷却 + runner 的「失败段重试一次」换到别的资源。

        事故数据：任务 q_ee0bcd7bf0 共 50 段，48 段 3~10s 正常返回，2 段卡满 300s。
        旧行为把这两段当资源故障（冷却 120s + 禁止重试）⇒ 整单作废。
        """
        last: Exception | None = None
        for attempt in range(CONNECT_RETRIES):
            if attempt:
                await asyncio.sleep(CONNECT_RETRY_BASE_S * attempt)
            try:
                return await self.client.post(
                    f"{self.base_url}/v1/audio/infer",
                    headers=self._headers(), data=data, files=files, timeout=self.timeout,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
            except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
                # 同步接口的固有长尾：平台可能还在推理，也可能已计费 ⇒ 语义上是
                # 「状态未知」，但代价极小（¥0.4/万字，一段 300 字约 ¥0.012），
                # 远小于「整单退掉已成功的 48 段」。所以允许重试换资源而不是判死。
                raise RetryableResourceError(
                    f"Modelverse 本次未拿到音频（{type(exc).__name__}，超过 {self.timeout:.0f}s 未响应"
                    f"或连接中断；平台侧可能仍在推理/已计费）"
                ) from exc
        raise RuntimeError(f"Modelverse 连接失败（已重试 {CONNECT_RETRIES - 1} 次）: {last}")

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        if not self.api_key:
            raise RuntimeError("Modelverse 引擎缺少 API Key（MODELVERSE_API_KEY）")
        payload = self._build_payload(req)
        audio, filename = self._reference_bytes(req)
        resp = await self._post_infer(
            data={"model": MODEL, "payload": json.dumps(payload, ensure_ascii=False)},
            files={"spk_audio_file": (filename, audio, "application/octet-stream")},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Modelverse 合成失败 HTTP {resp.status_code}: {self._error_text(resp)}")
        body = resp.content or b""
        # 200 也可能塞着错误 JSON（有的网关就这么干）⇒ 只认字节、不认 Content-Type
        if body.lstrip()[:1] == b"{":
            raise RuntimeError(f"Modelverse 返回了错误体而不是音频: {self._error_text(resp)}")
        if not body:
            raise RuntimeError("Modelverse 返回空音频")
        return body
