"""确保本地引擎要用到的参考音频在 tts-server 上存在（合成前预检 + 按需上传）。

## 为什么需要这一层

自建 tts-server 的 `/api/synthesize` 只接受**服务器本地路径**（`server.py:357` 是
`os.path.exists(req.voice)`）。于是「backend 选好的音色」与「服务器上有什么文件」
成了两份互不知情的状态，任何一边变动就 400：

- **新上线一台 tts-server**：它上面一个音色都没有 ⇒ 本来能跑的任务突然全失败；
- **陈旧路径**：任务里存的是 2026-09-28 之前产出的相对路径
  （`data/preset-voices/xxx.mp3`），它在 backend 的 cwd 下「可读」⇒
  `mono_runner._resolve_local_voice` 判定无需纠正 ⇒ 原样发给服务器 ⇒ 对方没有。

**修法不是让 tts-server 反向拉**：backend 通常跑在开发机/内网（NAT 后没有公网入口），
GPU 机器连不上它；而反方向本来就通（`TTS_URL` 已配、`POST /api/voices/upload` 已存在），
所以由 backend 在**提交前**补齐即可 —— 不需要 backend 对外、不需要新端点、不需要新配置。

## 命名规则（2026-10-02 修订：用户音色改「id 化」）

| 类别 | 目录 | 归属 | 服务器上的文件名 |
|---|---|---|---|
| 预设音色 | `data/preset-voices/`（含 `emotions/`） | 全员共享 | **原名** |
| BreezeBlue | `data/breezeblue/audio/` | 全员共享（310 条） | **原名** |
| 用户音色·新结构 | `data/voices/<user_id>/<voice_id>.wav` | **用户独有** | **`{user_id}_{voice_id}{后缀}`** |
| 用户音色·老结构 | `data/voices/<名>.wav`（平铺） | **用户独有** | **`{名}__{member_id}{后缀}`**（保持旧规则） |

只有用户音色需要隔离：共享音色「同名即同内容」，而两个用户可能各有一个名字相同、
内容不同的自定义音色 —— 直接用原名上传会互相覆盖。

**两种用户结构并存**（用户 2026-10-02 明确选择「存量不迁移」，见 `app/voice_store.py`）：
判定依据是**文件在不在用户子目录里**（`data/voices/<owner>/x.wav` 相对根目录有两段），
不需要任何额外元数据：

- 新结构 → `{user_id}_{voice_id}{后缀}`。因为本地文件名就是 `{voice_id}`，
  **显示名改了也不影响它** ⇒ 重命名不需要碰 tts-server。
- 老结构 → 保持 `{名}__{member_id}{后缀}` 不变。这条**绝不能改**：老任务里存的是
  服务器绝对路径，一旦改名，那些任务会全部失配。

**未知目录**（既不在上面几处、也不在 `VOICE_FALLBACK_DIRS`）按**用户独有·老结构**处理：
安全优先 —— 宁可多占一点磁盘，也不要串音。

## 与「服务自述能力」的关系

本模块只负责「文件在不在」，不动 `EngineCapabilities`。响度归一那件事走
`/api/health` 自述（见 `indextts_local._apply_capabilities`），两者互不相干。
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import DATA_DIR, LOCAL_VOICES_DIR, PRESET_VOICES_DIR, logger
from .base import VoiceRef

SHARED = "shared"   # 全员共享：同名即同内容，服务器上用原名
USER = "user"       # 用户独有：需要按 owner 隔离命名
UNKNOWN = "unknown"  # 认不出归属：按独有处理（安全优先）


class VoiceUnavailable(RuntimeError):
    """参考音频在 tts-server 与 backend 上都找不到，无法补齐。"""


# 服务器音色表缓存：{tts_url: (monotonic 时间戳, {服务器文件名: 服务器绝对路径})}
_SERVER_VOICES: dict[str, tuple[float, dict[str, str]]] = {}
_TTL = 30.0

# 每台服务器一把上传锁：多段并发时避免同一个音色被重复上传
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(tts_url: str) -> asyncio.Lock:
    lock = _LOCKS.get(tts_url)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[tts_url] = lock
    return lock


def reset_cache(tts_url: str | None = None) -> None:
    """清空音色表缓存与上传锁（供测试与排障使用）。"""
    if tts_url is None:
        _SERVER_VOICES.clear()
        _LOCKS.clear()
    else:
        _SERVER_VOICES.pop(tts_url, None)


def _search_dirs() -> list[tuple[Path, str]]:
    """backend 侧的参考音频搜索路径 → (目录, 类别)，顺序即优先级。"""
    dirs: list[tuple[Path, str]] = [
        (PRESET_VOICES_DIR, SHARED),
        (PRESET_VOICES_DIR / "emotions", SHARED),
        (DATA_DIR / "breezeblue" / "audio", SHARED),
        (LOCAL_VOICES_DIR, USER),
    ]
    for raw in os.environ.get("VOICE_FALLBACK_DIRS", "").split(os.pathsep):
        if raw.strip():
            dirs.append((Path(raw.strip()), SHARED))
    return dirs


@dataclass
class VoiceLocation:
    """音色的本地定位结果。`local_file=None` 表示 backend 侧也没有这个文件。"""

    kind: str
    local_file: Path | None = None


def _kind_of(path: Path) -> str:
    """按文件**实际所在目录**判定类别；不在已知目录里 → UNKNOWN。"""
    try:
        resolved = path.resolve()
    except OSError:
        return UNKNOWN
    for base, kind in _search_dirs():
        try:
            resolved.relative_to(base.resolve())
        except (ValueError, OSError):
            continue
        return kind
    return UNKNOWN


def locate_voice(voice_path: str) -> VoiceLocation:
    """定位音色的真实本地文件并判定类别。

    先看 `voice_path` 本身是否可读（此时按它所在目录定类别，这解决了
    「拿到的就是 backend 绝对路径」的正常情形）；读不到再按 basename 在
    backend 的音色目录里找同名替身（这解决了陈旧相对路径与跨机路径）。

    用户音色有**两种落点**：老结构平铺在 `data/voices/` 下，新结构在
    `data/voices/<user_id>/` 下 —— 后者要再下潜一层才找得到。
    """
    p = Path(voice_path)
    if p.is_file():
        return VoiceLocation(_kind_of(p), p)
    for base, kind in _search_dirs():
        cand = base / p.name
        if cand.is_file():
            return VoiceLocation(kind, cand)
        for sub in _user_subdir_matches(base, p.name):
            return VoiceLocation(USER, sub)
    return VoiceLocation(UNKNOWN, None)


def _user_subdir_matches(base: Path, file_name: str):
    """在 `data/voices/<owner>/` 里找同名文件（新结构音色）。"""
    if base != LOCAL_VOICES_DIR:
        return
    try:
        for sub in sorted(base.glob(f"*/{file_name}")):
            if sub.is_file():
                yield sub
    except OSError:
        return


def _is_id_layout(path: Path | None) -> bool:
    """该文件是不是「新结构用户音色」（`data/voices/<owner>/<file>`）。

    判据 = 相对 `data/voices/` 有两段路径。老结构平铺文件只有一段。
    """
    if path is None:
        return False
    try:
        rel = path.resolve().relative_to(LOCAL_VOICES_DIR.resolve())
    except (ValueError, OSError):
        return False
    return len(rel.parts) == 2


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def target_server_name(
    local_name: str, kind: str, owner_id: str | None, *, id_layout: bool = False
) -> str:
    """算出该音色在 tts-server 上**应该**叫什么。

    - 共享类（预设 / BreezeBlue），或拿不到 owner 信息 → 原名（与旧行为一致）；
    - 用户音色·新结构（`id_layout=True`）→ `{owner}_{voice_id}{后缀}`；
    - 用户音色·老结构 / 未知目录 → `{名}__{owner}{后缀}`（**保持旧规则**，
      老任务里存的服务器路径才不会失配）。

    `id_layout` 由**文件实际落点**判定（见 `_is_id_layout`），不依赖任何元数据。

    已经是目标形态时不再叠加前缀/后缀 —— 否则二次运行（任务里存的已是服务器路径）
    会得到 `x__u_1__u_1.wav` / `u_1_u_1_x.wav` 这种永远匹配不上的名字。
    """
    if kind == SHARED or not owner_id:
        return local_name
    p = Path(local_name)
    owner = _UNSAFE.sub("_", owner_id).strip("_") or "anon"
    if id_layout:
        if p.stem.startswith(f"{owner}_"):
            return local_name
        return f"{owner}_{p.stem}{p.suffix}"
    if p.stem.endswith(f"__{owner}"):
        return local_name
    return f"{p.stem}__{owner}{p.suffix}"


async def _load_server_voices(
    client: httpx.AsyncClient, tts_url: str, *, force: bool = False
) -> dict[str, str]:
    """取服务器音色表 `{文件名: 服务器绝对路径}`（带 TTL 缓存）。"""
    hit = _SERVER_VOICES.get(tts_url)
    if hit and not force and (time.monotonic() - hit[0]) < _TTL:
        return hit[1]
    resp = await client.get(f"{tts_url}/api/voices", timeout=10.0)
    resp.raise_for_status()
    table: dict[str, str] = {}
    for item in resp.json().get("voices") or []:
        if isinstance(item, dict):
            name, path = item.get("name"), item.get("path")
            if isinstance(name, str) and isinstance(path, str):
                table[name] = path
    _SERVER_VOICES[tts_url] = (time.monotonic(), table)
    return table


async def _upload_voice(
    client: httpx.AsyncClient, tts_url: str, src: Path, target: str
) -> str:
    """把本地文件以 `target` 为名上传到服务器，返回服务器侧路径。"""
    mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
    logger.info(
        "[voice-sync] 上传音色 %s → tts-server %s（%.1f KB）",
        src.name, target, src.stat().st_size / 1024,
    )
    with src.open("rb") as fh:
        resp = await client.post(
            f"{tts_url}/api/voices/upload",
            files={"file": (target, fh, mime)},
            data={"name": target},   # custom_name：tts-server 侧存成这个名字
            timeout=180.0,
        )
    if resp.status_code == 409:
        # 竞态：别的请求刚好同名传上去了（或本请求的重试）→ 以服务器现有为准
        table = await _load_server_voices(client, tts_url, force=True)
        resolved = table.get(target)
        if resolved:
            logger.info("[voice-sync] 上传冲突但服务器已有该音色，直接复用 %s", target)
            return resolved
        raise VoiceUnavailable(f"音色 {target!r} 上传冲突，且服务器音色表里查不到它")
    if resp.status_code != 200:
        raise VoiceUnavailable(
            f"音色 {target!r} 上传到 {tts_url} 失败 HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    resolved = body.get("path") if isinstance(body, dict) else None
    if not resolved:
        table = await _load_server_voices(client, tts_url, force=True)
        resolved = table.get(target)
    if not resolved:
        raise VoiceUnavailable(f"音色 {target!r} 上传成功但拿不到服务器路径")
    # 立刻写回缓存，让同一任务的后续段直接命中（省掉一次列表查询）
    hit = _SERVER_VOICES.get(tts_url)
    merged = dict(hit[1]) if hit else {}
    merged[target] = resolved
    _SERVER_VOICES[tts_url] = (time.monotonic(), merged)
    logger.info("[voice-sync] 音色就绪 %s", resolved)
    return resolved


async def ensure_voice_on_server(
    client: httpx.AsyncClient, tts_url: str, voice: VoiceRef
) -> str:
    """确保 voice 在 tts-server 上存在，返回可直接提交的服务器路径。

    返回的路径一定来自服务器（`/api/voices` 或刚上传的返回值），因此调用方不再
    依赖 backend 的 cwd 与路径形态 —— 陈旧相对路径、本机绝对路径、服务器路径
    三种形态在这一层被统一。

    **拿不到服务器音色表时降级**：原样返回 `tts_path or local_path`，把判断交回
    合成环节。探片刻失败不该在 backend 侧直接把任务判死（服务器可能其实有那个
    文件），这与「响度归一探测失败就保持保守值」是同一套思路。
    """
    raw = voice.tts_path or voice.local_path
    if not raw:
        raise VoiceUnavailable(f"音色 {voice.display_name!r} 没有可用的参考音频路径")

    name = Path(raw).name
    location = locate_voice(raw)
    # `id_layout` 决定用户音色用哪套服务器命名（新 `{owner}_{id}` / 老 `{名}__{owner}`）。
    # 判据是**文件实际落点**，所以「任务里存的是服务器路径、backend 也有副本」这种
    # 常见情形也能正确判定 —— 那时 locate_voice 会在 data/voices/ 下找到替身。
    target = target_server_name(
        name, location.kind, voice.owner_id, id_layout=_is_id_layout(location.local_file)
    )

    try:
        table = await _load_server_voices(client, tts_url)
    except Exception as exc:  # noqa: BLE001 — 探测失败不升级为任务失败
        logger.warning(
            "[voice-sync] 取不到 %s 的音色表，按原路径提交 name=%s error=%s",
            tts_url, name, exc,
        )
        return raw

    if target in table:
        return table[target]

    # 未命中：先用**强制刷新**排除「缓存过期但服务器其实已有」的误判
    # （命中路径不刷新，所以正常情况零额外请求；只有 miss 才多打一次列表）
    try:
        table = await _load_server_voices(client, tts_url, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[voice-sync] 刷新 %s 的音色表失败 name=%s error=%s", tts_url, name, exc)
        return raw
    if target in table:
        return table[target]

    if location.local_file is None:
        # backend 也没有本地文件 ⇒ 没法按新规则补传。
        # 若服务器上已有**原名**版本（历史遗留，或共享文件被误判成独有），
        # 就复用它而不是报错 —— 避免「服务器本来跑得通、却因为命名规则变化
        # 而回归失败」。连原名版本都没有，才算真的缺失。
        if target != name and name in table:
            logger.info("[voice-sync] 未找到隔离名 %s，回退复用服务器现有的 %s", target, name)
            return table[name]
        raise VoiceUnavailable(
            f"参考音频 {name!r} 在 tts-server({tts_url}) 上不存在，"
            f"backend 本地也找不到同名文件（原路径 {raw!r}）"
        )

    async with _lock_for(tts_url):
        # 等锁期间，同任务的其他段可能已经把它传上去了
        table = await _load_server_voices(client, tts_url, force=True)
        if target in table:
            return table[target]
        return await _upload_voice(client, tts_url, location.local_file, target)
