"""autodl.art 托管工作流适配器（indextts2-v1，IndexTTS2 底座）。

接口事实（2026-09-16 用户实测确认）：
  POST https://autodl.art/api/v1/comfyui/comfyui_workflow/indextts2-v1
      → {"data": {"task_id": ...}}
  GET  https://autodl.art/api/v1/comfyui/comfyui_workflow/result/{task_id}
      → {"data": {"status": "SUCCESS|FAILED|...", "results": [{"url": ...}|url, ...]}}
  计费：0.001 元/s（2026-09-18 平台调价，原为按次计费）；单次提交 ≤2048 字符
      （分片控制见 chunker.py，分片次数多会增加计费时长边界的冗余）。
  请求体字段：8 个情绪滑杆 + emo_control_method + prompt_simple(base64 data URI)
  + prompt_text（参考音频转写；合成文本经在线调用页面对应字段传入，
  以 tools/autodl_body.json 模板为准——字段名随工作流版本可能变化）。

情绪控制结论（2026-09-18 探测锤实，勿再重复排查）：
  **本工作流当前实际不支持任何情绪控制，唯一可用模式 = 跟随参考音频。**
  - 表单 schema（GET /api/v1/comfyui/workflows/indextts2-v1，存档
    tools/autodl_indextts2-v1_def.json）声明 emo_control_method 有三档：
    与音色参考音频相同 / 使用情感参考音频 / 使用情感向量控制，滑杆 0-1.4；
  - 但提交端对后两档一律拒绝（"参数值非法"），与滑杆类型（int/float/str）、
    emo_random、是否带 emo_ref_audio 等组合无关（10+ 变体全试）；
  - 默认档下滑杆被接受但**静默忽略**——这是 q_718181b5f6 全程无情绪变化的
    根因（此前探针只验了 ASR 文本，没验情绪表达，漏检）。
  - 因此 _build_body 保留滑杆赋值（平台修好后即生效），但调用方
    （mono_runner）会在 art 首选 + 任务含情绪标签时打警告。
  另：emo_ref_audio（情感参考音频 URL）字段存在且 required=false，
  "使用情感参考音频"模式可作为平台修复后的备选路径。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from .base import SegmentRequest, audio_data_uri

DEFAULT_SUBMIT = "https://autodl.art/api/v1/comfyui/comfyui_workflow/indextts2-v1"
DEFAULT_RESULT = "https://autodl.art/api/v1/comfyui/comfyui_workflow/result/{task_id}"

# autodl.art 单次提交的字符上限（平台按次计费的硬限制）
MAX_CHARS_PER_SUBMIT = 2048

# 情绪标签 → autodl.art 滑杆字段
# 注意：平台字段含 emo_melancholic、无 neutral（全零滑杆即中性）
# 实测（2026-09-18）：工作流 indextts2-v1 的 emo_surprised 是单选项枚举
# （options 仅 ["0"]，int 会报"enum 参数值必须是字符串"，其它值不在 options），
# 平台侧锁死无法表达惊喜 → surprised 映射 None，降级为跟随参考音频
_LABEL_TO_FIELD = {
    "happy": "emo_happy",
    "sad": "emo_sad",
    "angry": "emo_angry",
    "afraid": "emo_afraid",
    "disgusted": "emo_disgusted",
    "melancholic": "emo_melancholic",
    "surprised": None,  # 平台 v1 锁死为 "0"，无法表达；降级跟随音色
    "calm": "emo_calm",
    "neutral": None,  # 中性：保持全零滑杆
}


class IndexttsArtEngine:
    name = "indextts_art"

    def __init__(
        self,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        body_template: dict | None = None,
        submit_url: str = DEFAULT_SUBMIT,
        result_url: str = DEFAULT_RESULT,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ):
        self.token = token or os.environ.get("AUTODL_API_TOKEN", "")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        self.body_template = body_template or json.loads(
            (Path(__file__).resolve().parents[3] / "tools" / "autodl_body.json").read_text(encoding="utf-8")
        )
        self.submit_url = submit_url
        self.result_url = result_url
        self.poll_interval = poll_interval
        self.timeout = timeout
        # 参考音频 data URI 缓存（键=path，值=((size, mtime_ns), uri)）：
        # 同一音色每段 base64 编码一次即可，文件变化自动失效
        self._audio_cache: dict[str, tuple[tuple[int, int], str]] = {}

    async def health(self) -> bool:
        """有 Token 即视为可调度（平台侧调度，无实例概念）；真正可用性在合成时验证。"""
        return bool(self.token)

    def _audio_data_uri(self, path: str) -> str:
        """带缓存的 data URI 编码（避免每段重复 base64 整个参考音频）。"""
        p = Path(path)
        st = p.stat()
        key = (st.st_size, st.st_mtime_ns)
        hit = self._audio_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        uri = audio_data_uri(path)
        self._audio_cache[path] = (key, uri)
        return uri

    def _build_body(self, req: SegmentRequest) -> dict:
        import copy

        body = copy.deepcopy(self.body_template)
        if not req.voice.local_path:
            raise ValueError(f"autodl.art 引擎需要本地参考音频文件: {req.voice.display_name!r}")
        body = self._replace(body, "{{AUDIO}}", self._audio_data_uri(req.voice.local_path))
        # 停顿标记：autodl.art 工作流不支持，降级为逗号（自然短停顿）
        import re as _re

        art_text = _re.sub(r"\[pause:[0-9]*\.?[0-9]+\]", "，", req.text).replace("<#>", "，")
        body = self._replace(body, "{{TEXT}}", art_text)
        # 统一 8 标签 → 平台 8 滑杆（未选标签时保持模板原值=跟随参考音频）。
        # 赋值必须保持各字段在模板中的原类型：emo_surprised 等枚举字段平台要求
        # 字符串（"0"/"1"），写成 int 会被拒（"enum 参数值必须是字符串"）
        if req.emotion_label and _LABEL_TO_FIELD.get(req.emotion_label):
            field_name = _LABEL_TO_FIELD[req.emotion_label]
            for label, f in _LABEL_TO_FIELD.items():
                if f and f in body:
                    selected = f == field_name
                    body[f] = ("1" if selected else "0") if isinstance(body[f], str) else (1 if selected else 0)
        return body

    def _replace(self, value, placeholder: str, content: str):
        if isinstance(value, str):
            return value.replace(placeholder, content)
        if isinstance(value, list):
            return [self._replace(x, placeholder, content) for x in value]
        if isinstance(value, dict):
            return {k: self._replace(v, placeholder, content) for k, v in value.items()}
        return value

    async def synthesize_segment(self, req: SegmentRequest) -> bytes:
        body = self._build_body(req)
        headers = {"Authorization": self.token, "Content-Type": "application/json"}

        resp = await self.client.post(self.submit_url, json=body, headers=headers, timeout=60.0)
        if resp.status_code != 200:
            raise RuntimeError(f"autodl.art 提交失败 HTTP {resp.status_code}: {resp.text[:500]}")
        task_id = (resp.json().get("data") or {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"autodl.art 未返回 task_id: {resp.text[:300]}")

        # 提交后立即首查一次（平台侧有时秒回），未完成再按 poll_interval 轮询
        deadline = asyncio.get_event_loop().time() + self.timeout
        while True:
            r = await self.client.get(self.result_url.format(task_id=task_id), headers=headers, timeout=60.0)
            if r.status_code != 200:
                raise RuntimeError(f"autodl.art 状态查询失败 HTTP {r.status_code}")
            data = r.json().get("data") or r.json()
            status = str(data.get("status", "")).upper()
            if status == "SUCCESS":
                return await self._download(data)
            if status == "FAILED":
                raise RuntimeError(f"autodl.art 任务失败: {json.dumps(data, ensure_ascii=False)[:500]}")
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f"autodl.art 任务超时 task_id={task_id}")
            await asyncio.sleep(self.poll_interval)

    async def _download(self, data: dict) -> bytes:
        candidates = data.get("results", [])
        if isinstance(candidates, dict):
            candidates = candidates.get("results", [])
        for item in candidates:
            url = item if isinstance(item, str) else (item.get("url") if isinstance(item, dict) else None)
            if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                audio = await self.client.get(url, timeout=120.0)
                audio.raise_for_status()
                return audio.content
        raise RuntimeError(f"autodl.art 任务成功但无可下载音频 URL: {json.dumps(data, ensure_ascii=False)[:300]}")
