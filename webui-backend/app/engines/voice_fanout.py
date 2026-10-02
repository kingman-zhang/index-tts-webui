"""把用户音色**广播**到资源池里所有在跑的 tts-server（2026-10-02，用户需求第 2/5 条）。

## 为什么需要它

`voice_sync.ensure_voice_on_server()` 是**按需**补传：只在「这一段恰好被调度到某台、
而那台上没有这个音色」时才上传。它保证**合成不会失败**，但不保证**上传之后立刻处处可用**：

- 新上传的音色只落到 `TTS_URL` 那一台（旧的单点问题，已在 `e153824` 补了 backend 本地副本）；
- 用户删掉音色后，池内其它机器的副本还在（占磁盘，且老任务路径仍指向它）。

所以上传/删除要**主动广播**一次，把「池内每台 local 的当前状态」拉齐到与 backend 一致。
合成时那条按需补传路径**继续保留**——它负责新上线的机器和广播时临时连不上的机器。

## 边界

- **只推给 `provider=local` 的资源**：云端引擎（302.ai / SiliconFlow / art）不落地文件，
  它们各自按 URL / `speech:` URI / base64 传参考音频，收不到也不需要这份文件。
- **失败只记日志，绝不抛出**：广播是「尽力而为」的一致性优化，不是上传的前置条件。
  一台 GPU 机器正在重启不该让用户的上传失败 —— 那台音色会在首次合成时被按需补传。
- 收到删除请求时，服务器上不存在（404）也算成功：目标状态已经达成。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import httpx

from ..config import logger

# 广播的超时：上传要给音频文件留时间（几十 MB 的 wav 也可能），删除很快
UPLOAD_TIMEOUT = 180.0
DELETE_TIMEOUT = 30.0


async def local_targets() -> list[str]:
    """资源池里所有 `provider=local` 的 tts-server 地址（去重、保序）。

    池的配置源在 `factory.build_registry()`（支持文件热加载）。构建失败时返回空列表
    并打 warning —— 音色上传本身不该因为「资源池配置写错了」而失败。
    """
    from .factory import build_registry

    try:
        registry = build_registry()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[voice-fanout] 取不到资源池，本次不做广播（合成时会按需补传）：%s", exc)
        return []

    urls: list[str] = []
    for engine in getattr(registry, "engines", []) or []:
        for cfg, _impl in getattr(engine, "resources", []) or []:
            if getattr(cfg, "provider", None) != "local":
                continue
            base = (getattr(cfg, "base_url", None) or "").rstrip("/")
            if base and base not in urls:
                urls.append(base)
    return urls


def _forget_cache(tts_url: str) -> None:
    """清掉 voice_sync 对这台机器的音色表缓存。

    不清的话，广播刚上传完、紧接着的合成仍可能读到 30s 内的旧表 ⇒ 又触发一次
    「未命中 → 强制刷新 → 上传」，白跑一轮（服务端 409 兜底不会出错，但浪费）。
    """
    try:
        from .voice_sync import reset_cache
        reset_cache(tts_url)
    except Exception:  # noqa: BLE001 — 缓存清理失败无关紧要
        pass


async def broadcast_upload(
    client: httpx.AsyncClient, src: Path, server_name: str
) -> dict[str, str]:
    """把本地文件以 `server_name` 上传到池内每台 local。

    返回 `{tts_url: 结果}`，结果为 `"ok"` 或一句人话的原因（仅用于日志/接口响应）。
    """
    targets = await local_targets()
    if not targets:
        return {}
    mime = mimetypes.guess_type(server_name)[0] or "application/octet-stream"
    try:
        content = src.read_bytes()
    except OSError as exc:
        logger.error("[voice-fanout] 读取本地音色失败，放弃广播 src=%s error=%s", src, exc)
        return {url: f"读取本地文件失败：{exc}" for url in targets}

    results: dict[str, str] = {}
    for tts_url in targets:
        try:
            resp = await client.post(
                f"{tts_url}/api/voices/upload",
                files={"file": (server_name, content, mime)},
                data={"name": server_name},
                timeout=UPLOAD_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — 单台失败不影响其它台，也不影响上传本身
            results[tts_url] = f"连接失败：{type(exc).__name__}"
            logger.warning(
                "[voice-fanout] 广播上传失败 tts=%s name=%s error=%s（该台将在首次合成时按需补传）",
                tts_url, server_name, exc,
            )
            continue
        if resp.status_code == 200:
            results[tts_url] = "ok"
            logger.info("[voice-fanout] 广播上传成功 tts=%s name=%s", tts_url, server_name)
        elif resp.status_code == 409:
            # 服务器上已有同名（重复广播 / 之前按需补传过）：目标状态已达成
            results[tts_url] = "ok(已存在)"
            logger.info("[voice-fanout] 广播上传跳过（服务器已有同名）tts=%s name=%s", tts_url, server_name)
        else:
            results[tts_url] = f"HTTP {resp.status_code}: {resp.text[:120]}"
            logger.warning(
                "[voice-fanout] 广播上传失败 tts=%s name=%s status=%s body=%s",
                tts_url, server_name, resp.status_code, resp.text[:200],
            )
        _forget_cache(tts_url)
    return results


async def broadcast_delete(client: httpx.AsyncClient, server_name: str) -> dict[str, str]:
    """在池内每台 local 上删除 `server_name`；404 视为成功（本来就没有）。"""
    targets = await local_targets()
    if not targets:
        return {}

    results: dict[str, str] = {}
    for tts_url in targets:
        try:
            resp = await client.delete(
                f"{tts_url}/api/voices/{server_name}", timeout=DELETE_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001
            results[tts_url] = f"连接失败：{type(exc).__name__}"
            logger.warning("[voice-fanout] 广播删除失败 tts=%s name=%s error=%s", tts_url, server_name, exc)
            continue
        if resp.status_code in (200, 404, 405):
            results[tts_url] = "ok"
            logger.info("[voice-fanout] 广播删除完成 tts=%s name=%s status=%s",
                        tts_url, server_name, resp.status_code)
        else:
            results[tts_url] = f"HTTP {resp.status_code}: {resp.text[:120]}"
            logger.warning("[voice-fanout] 广播删除失败 tts=%s name=%s status=%s body=%s",
                           tts_url, server_name, resp.status_code, resp.text[:200])
        _forget_cache(tts_url)
    return results
