"""单音色配音的**分段音频缓存**：流式落盘的中间产物，同时是断点续传凭据。

为什么要有它（2026-10-10，G4）
──────────────────────────────────────────────────────────────
mono_runner 原先把所有分段的音频字节攒在内存里（`results` 列表），最后一次性
交给 `_concat_wavs` 拼成整章 wav。峰值内存是音频体积的**数倍**：results 列表
一份、`_concat_wavs` 里的 frames 列表一份、输出 BytesIO 一份、`getvalue()`
返回拷贝又一份。而且它随章节大小线性增长 —— 单章 2 万字约 200 MB 音频
（22.05 kHz/16bit 单声道），峰值 600~800 MB。**当初「单章上限只能定在 1 万字」
就是这么来的**（见 `book_split.DEFAULT_CHAPTER_MAX_CHARS`）：不是产品判断，
是被内存逼的。

改成「每段合成完立即落盘」之后，同一个动作带来两条收益：

  ① **流式落盘**：合流时按段落顺序一个个「读 → 写 → 丢」，峰值内存 = 单段，
     与章节总长彻底无关。章可以想多大就多大。
  ② **断点续传**：段文件本身就是续传凭据。任务失败/中断/服务重启后，用户点
     「重新提交」是**复用同一个 task_id**（见 `routes/queue.py:retry_queue_task`），
     重跑时已完成的段直接复用、不再调用平台 —— 云端引擎按次计费，跳过即省钱，
     也省掉重跑的时间。

两条收益来自同一个动作，所以合在一处做。

失效规则（**正确性关键**）
──────────────────────────────────────────────────────────────
「命中缓存」的前提是这次要合成的东西与上次**逐段一致**。而段划分依赖引擎声明的
单次上限（`capabilities.max_input_chars`）—— 换引擎（典型场景：某台熔断后从
art 切到 302.ai，上限 2048 → 2000）会改变切法，于是**同一个下标 i 对应的文本
变了**，复用旧文件就会拼出**错位的音频**（听感上就是「句子串行」）。

因此缓存目录里记一份**合成指纹**：引擎名 + 单次上限 + 语速 + 音色路径 +
全部段的（文本, 情绪, 段后静音）。指纹不符即整目录作废重建。这样「命中」永远
等价于「这段的输入与产出确实对得上」。

原子性
──────────────────────────────────────────────────────────────
段文件先写 `*.tmp` 再 `os.replace` 改名。进程在写一半时被杀，磁盘上只会留下
`.tmp`（不会被当成有效段），不会出现「半截 wav 被当作完整段拼进成品」。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Iterable

from .config import DATA_DIR, logger

_META_NAME = "meta.json"


def default_root() -> Path:
    """生产路径下的缓存根（= `DATA_DIR/outputs/seg_cache`，与 mono 成品同区）。

    ⚠️ mono_runner 走的是自己的 `_seg_cache_root()`（= `OUTPUTS_DIR/seg_cache`）。
    两者在生产下**指向同一处**；分开写只是因为测试会 patch `mono_runner.OUTPUTS_DIR`
    把产物重定向到临时目录，写死常量会绕过它。
    """
    return DATA_DIR / "outputs" / "seg_cache"


def _safe(task_id: str) -> str:
    """任务 id → 目录名（与 queue_state.queue_file 同口径，防路径穿越）。"""
    return task_id.replace("/", "_").replace("\\", "_")


def cache_dir(root: Path, task_id: str) -> Path:
    """某个任务的分段缓存目录。"""
    return Path(root) / _safe(task_id)


def _meta_path(root: Path, task_id: str) -> Path:
    return cache_dir(root, task_id) / _META_NAME


def fingerprint(
    *,
    engine: str,
    max_input_chars: int | None,
    speed: float,
    voice: str,
    entries: list[dict],
) -> str:
    """合成指纹：其中任何一项变化都意味着旧缓存不能再复用。

    刻意把**全部段的文本**纳进来（而不只是引擎与参数）：这样「用户改了稿子再
    重新生成」在指纹层面就被识别出来，不必依赖 task_id 一定换新。
    """
    payload = {
        "engine": engine,
        "max_input_chars": max_input_chars,
        "speed": round(float(speed), 6),
        "voice": voice,
        "segments": [
            [
                e.get("text", ""),
                e.get("emotion"),
                int(e.get("gap_ms") or 0),
            ]
            for e in entries
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_meta(root: Path, task_id: str) -> dict | None:
    """读缓存元数据；缺失或损坏返回 None（损坏按「无缓存」处理，不抛）。"""
    p = _meta_path(root, task_id)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("[seg-cache] 元数据读取失败 task=%s error=%s", task_id, e)
        return None


def prepare(root: Path, task_id: str, fp: str, count: int) -> bool:
    """准备缓存目录。返回 True = 指纹一致、已有段可复用；False = 已清空重建。

    元数据必须在**第一段写盘之前**落地：否则中途崩溃后重跑时读不到指纹，
    已完成的段就无法被识别为「可复用」，续传失效。
    """
    meta = load_meta(root, task_id)
    if meta and meta.get("fingerprint") == fp and int(meta.get("count") or -1) == count:
        return True
    clear(root, task_id)
    d = cache_dir(root, task_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{_META_NAME}.tmp"
    tmp.write_text(
        json.dumps({"fingerprint": fp, "count": count}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, _meta_path(root, task_id))
    return False


def segment_path(root: Path, task_id: str, index: int) -> Path:
    """第 index 段的缓存文件路径（0 起）。"""
    return cache_dir(root, task_id) / f"{index:06d}.wav"


def has_segment(root: Path, task_id: str, index: int) -> bool:
    """该段是否已有缓存。

    只判存在性、不解析文件：写入是原子的（见 `write_segment`），所以「文件在」
    就等价于「内容完整」。逐段解析反而会让命中检查的开销等于重读一遍音频。
    """
    return segment_path(root, task_id, index).is_file()


def read_segment(root: Path, task_id: str, index: int) -> bytes | None:
    """读回一段音频；缺失或读取失败返回 None。"""
    p = segment_path(root, task_id, index)
    if not p.is_file():
        return None
    try:
        return p.read_bytes()
    except Exception as e:
        logger.warning("[seg-cache] 读取分段失败 task=%s index=%d error=%s", task_id, index, e)
        return None


def write_segment(root: Path, task_id: str, index: int, data: bytes) -> None:
    """原子落盘一段音频（先写 .tmp 再改名）。"""
    p = segment_path(root, task_id, index)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def clear(root: Path, task_id: str) -> None:
    """删除该任务的缓存目录（成功收尾与指纹失配时调用）。异常不外抛。"""
    d = cache_dir(root, task_id)
    try:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        logger.warning("[seg-cache] 清理失败 task=%s error=%s", task_id, e)


def prune(root: Path, valid_task_ids: Iterable[str]) -> int:
    """删除**不属于任何现存任务**的缓存目录，返回删除数量。

    给启动时调用：失败/中断的任务会刻意保留缓存（供续传），但用户一旦把这个
    任务从队列里删掉，缓存就再也不会被用到 —— 没有这一步，磁盘会只增不减。
    """
    root = Path(root)
    if not root.is_dir():
        return 0
    valid = {_safe(t) for t in valid_task_ids}
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for d in entries:
        if not d.is_dir() or d.name in valid:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed
