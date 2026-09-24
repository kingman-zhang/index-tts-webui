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
  以 autodl_body.json 模板为准（随包位于 app/engines/，仓库根 tools/ 有同步副本）——
  字段名随工作流版本可能变化）。

情绪控制结论（2026-09-18 深夜实测修正，覆盖当日早间"平台不支持情绪"的误判）：
  **"使用情感向量控制"档是可用的**，前提：
  - emo_surprised 必须传字符串 "0"（schema type=enum，写 int 报
    "enum 参数值必须是字符串"；官方 input_example 自己写 int 0 也是错的）；
  - 早间误判根因：探测请求体里 {{AUDIO}}/{{TEXT}} 占位符未替换，
    prompt_simple 是 audio 类型字段，占位符非法导致所有模式一律
    "参数值非法"，与情绪档位无关（教训：探测必须用生产级合法请求体）。
  策略：有情绪标签（非 neutral）→ emo_control_method="使用情感向量控制"
  + 置对应滑杆；无标签/neutral → 保持默认档"与音色参考音频相同"。
  另：emo_ref_audio（情感参考音频）字段存在，"使用情感参考音频"档可作备选。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
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
        self.body_template = body_template or json.loads(self._load_body_template().read_text(encoding="utf-8"))
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

    @staticmethod
    def _load_body_template() -> Path:
        """定位请求体模板：优先包内副本（Docker 镜像只拷贝 app/，仓库根的
        tools/ 不在镜像里），回退仓库根 tools/（本地开发布局）。
        两处内容需保持一致——修改模板时同步 app/engines/autodl_body.json。"""
        pkg_local = Path(__file__).resolve().parent / "autodl_body.json"
        if pkg_local.exists():
            return pkg_local
        repo_tools = Path(__file__).resolve().parents[3] / "tools" / "autodl_body.json"
        if repo_tools.exists():
            return repo_tools
        raise FileNotFoundError(
            "autodl_body.json 模板缺失：应随包位于 app/engines/autodl_body.json"
        )

    def _audio_data_uri(self, path: str) -> str:
        """带缓存的 data URI 编码（避免每段重复 base64 整个参考音频）。

        平台工作流（indextts2-v1）只接受 mp3（audio/mpeg）参考音频，wav 会被
        拒绝"参数值非法"（2026-09-21 实测二分定位：同内容 wav 拒、mp3 收）。
        非 mp3 一律先转 mp3 再编码，转换结果落盘缓存（键含 size+mtime，文件
        变化自动失效）。
        """
        p = Path(path)
        st = p.stat()
        key = (st.st_size, st.st_mtime_ns)
        hit = self._audio_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        if p.suffix.lower() in (".mp3", ".mpeg"):
            uri = audio_data_uri(path)
        else:
            uri = audio_data_uri(self._to_mp3(p, st))
        self._audio_cache[path] = (key, uri)
        return uri

    def _to_mp3(self, p: Path, st: os.stat_result) -> Path:
        """wav/其它格式 → mp3（ffmpeg 转码，按内容哈希缓存到临时目录）。"""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
                if Path(cand).is_file():
                    ffmpeg = cand
                    break
        if not ffmpeg:
            # 无 ffmpeg：原样提交，让平台侧给出明确错误（与跳过归一的兜底策略一致）
            return p
        digest = hashlib.sha1(f"{p.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]
        out = Path(tempfile.gettempdir()) / "art_voice_mp3" / f"{digest}.mp3"
        if not out.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp_out = out.with_suffix(".tmp.mp3")
            result = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(p), "-b:a", "128k", str(tmp_out)],
                capture_output=True,
            )
            if result.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
                raise RuntimeError(f"参考音频转 mp3 失败: {result.stderr.decode('utf-8', 'ignore')[-200:]}")
            tmp_out.rename(out)
        return out

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
        # 统一 8 标签 → 平台 8 滑杆：
        # - 有标签（非 neutral）→ 切"使用情感向量控制"档 + 置滑杆（该档下平台
        #   真正读滑杆；默认档下滑杆被静默忽略——2026-09-18 实测）
        # - emo_surprised 平台锁死单选项枚举 "0"，必须字符串（int 报
        #   "enum 参数值必须是字符串"）→ surprised 无法表达，降级跟随音色
        # - 无标签/neutral → 保持模板默认档"与音色参考音频相同"，滑杆不碰
        field_name = _LABEL_TO_FIELD.get(req.emotion_label or "") if req.emotion_label else None
        if field_name:
            body["emo_control_method"] = "使用情感向量控制"
            for label, f in _LABEL_TO_FIELD.items():
                if f and f in body:
                    selected = f == field_name
                    # 数值滑杆保持数值类型（schema type=number）；emo_surprised
                    # 不在循环内赋值（锁死 "0"，见 _LABEL_TO_FIELD 映射 None）
                    body[f] = 1.0 if selected else 0.0
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
