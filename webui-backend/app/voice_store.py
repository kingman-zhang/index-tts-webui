"""用户音色库：**每个用户一个目录 + 一份 index.json**（2026-10-02 与用户确认）。

## 为什么要有这一层

旧结构是「`data/voices/` 平铺 + `data/voices_meta.json` 一张大表」，音色的身份
**就是文件名**。于是：

- 重命名 = 换身份 ⇒ 服务器上的文件、老任务里存的路径全部失配；
- 两个用户各有一个「我的音色A.wav」⇒ 要么互相覆盖，要么在服务器上加
  `__{owner}` 后缀，文件名越来越长、还不能改；
- 「显示名」和「存储名」纠缠在一起，前端用哪个当 key 都别扭。

新结构把这两件事拆开：

```
data/voices/<user_id>/          ← 目录名 = 用户 id（如 u_41588d678184）
    index.json                  ← 音色元数据（显示名在这里）
    voc_a1b2c3d4e5f6.wav        ← 文件名 = 音色 id + 原扩展名，**永不改变**
```

- **重命名 = 只改 `index.json` 里的 `name`**，不动任何文件（用户需求第 7 条）。
- 服务器上的文件名由 `{user_id}_{voice_id}{ext}` 现算（见 `engines/voice_sync.py`），
  与显示名无关 ⇒ 改名也不需要碰 tts-server。
- 音色 id 一旦分配终身不变，老任务里存的路径永远有效。

## 与老结构的关系（用户 2026-10-02 选择「不迁移」）

`data/voices/*.wav` 平铺文件 + `voices_meta.json` **原样保留**，仍然可读、可改名、
可删除（由 `routes/voices.py` 的既有分支处理）。本模块**只管新结构**，
两条路径靠「文件在不在用户子目录里」区分，互不干扰。

## 字段

| 字段 | 说明 |
|---|---|
| `id` | `voc_` + 12 位 hex（纯 hex，与 BreezeBlue 的 base36 id 不会撞） |
| `name` | 显示名，可改；同一用户内唯一 |
| `file` | 实际文件名（`{id}{ext}`，派生但落盘，便于人肉排查） |
| `ext` | 扩展名（含点） |
| `created_at` / `updated_at` | ISO 时间 |
| `deleted_at` | 软删标记；非 null 即从列表隐藏 |
| `source` | `"upload"`（为将来的"录制"等留口） |

`size_kb` **不落盘**，列表时按文件真实大小现算 —— 存下来就会和磁盘不一致。
音频时长同样不存（要解析容器格式，收益只是展示，不引入额外依赖）。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DATA_DIR, logger

# 用户音色根目录（与旧结构的 LOCAL_VOICES_DIR 是同一个目录：
# 老的平铺文件在它下面，新的用户目录也是它的子目录）
USER_VOICES_ROOT = DATA_DIR / "voices"

ALLOWED_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".webm")

# 音色 id：`voc_` + 12 位十六进制。刻意用纯 hex —— BreezeBlue 的 id 是 base36
# （如 `voc_534z3zu7d53k`，含非 hex 字符），两者永远不会互相匹配。
_ID_RE = re.compile(r"^voc_[0-9a-f]{12}$")

# 目录名 = 用户 id。白名单式校验，杜绝 `..` 之类的路径穿越。
_OWNER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class VoiceStoreError(RuntimeError):
    """音色库操作失败（id 非法、归属目录不存在、索引损坏等）。"""


class VoiceNameTaken(VoiceStoreError):
    """同一用户下已有同名音色（路由层映射成 409，与「参数不对」区分开）。"""


def new_voice_id() -> str:
    """生成音色 id（`voc_` + 12 位 hex）。"""
    return "voc_" + uuid.uuid4().hex[:12]


def is_voice_id(value: str) -> bool:
    """判断一个字符串是不是本模块分配的音色 id。"""
    return bool(_ID_RE.match(str(value or "")))


def valid_owner_id(owner_id: str) -> bool:
    return bool(_OWNER_RE.match(str(owner_id or "")))


# 每个用户的写锁：增删改都要「读 index → 改 → 写回」，并发下必须串行。
# 与 membership 的 user_lock 同一套思路：按 owner 分锁，不同用户互不阻塞。
# 只保证单进程（后端就是单进程 uvicorn；多 worker 需换文件锁）。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(owner_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(owner_id)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[owner_id] = lock
        return lock


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_name(name: str) -> str:
    """显示名清洗：去首尾空白，禁止路径分隔符与控制字符。"""
    name = (name or "").strip()
    if not name:
        raise VoiceStoreError("音色名称不能为空")
    if re.search(r"[\\/:*?\"<>|\x00-\x1f]", name) or Path(name).name != name:
        raise VoiceStoreError("音色名称不能包含 / \\ : * ? \" < > | 等字符")
    if len(name) > 60:
        raise VoiceStoreError("音色名称过长（最多 60 个字符）")
    return name


class UserVoiceStore:
    """单个用户的音色库（目录 + index.json）。"""

    def __init__(self, owner_id: str):
        if not valid_owner_id(owner_id):
            raise VoiceStoreError(f"非法的用户 id：{owner_id!r}")
        self.owner_id = owner_id
        self.dir = USER_VOICES_ROOT / owner_id
        self.index_path = self.dir / "index.json"

    # ─── 落盘 ──────────────────────────────────────────────

    def _load(self) -> dict:
        if not self.index_path.exists():
            return {"version": 1, "owner_id": self.owner_id, "voices": []}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 索引坏了不能当成「这个用户没有音色」——那会让下一次上传**覆盖**整个索引，
            # 把用户已有的音色记录全丢掉。直接抛错，让人去修。
            raise VoiceStoreError(f"音色索引损坏（{self.index_path}）：{exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
            raise VoiceStoreError(f"音色索引结构异常（{self.index_path}）")
        return data

    def _save(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_name(self.index_path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)   # 原子替换：断电也不会留下半截 JSON

    # ─── 视图 ──────────────────────────────────────────────

    def _decorate(self, item: dict) -> dict:
        """补上派生字段（path / size_kb / preview_name / server_name）。"""
        file_name = item.get("file") or f"{item.get('id', '')}{item.get('ext', '')}"
        path = self.dir / file_name
        try:
            size_kb = round(path.stat().st_size / 1024, 1)
        except OSError:
            size_kb = 0.0
        ext = item.get("ext") or path.suffix
        return {
            "voice_id": item.get("id", ""),
            "name": item.get("name", ""),
            "file": file_name,
            "path": str(path),
            # 试听走 `/api/audio/{文件名}`，所以前端要的是**文件名**而不是显示名
            "preview_name": file_name,
            "size_kb": size_kb,
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "source": item.get("source", "upload"),
            # 服务器上应有的文件名（显示名与它无关 ⇒ 改名不用碰 tts-server）
            "server_name": server_filename(self.owner_id, item.get("id", ""), ext),
        }

    def list(self, *, include_deleted: bool = False) -> list[dict]:
        with _lock_for(self.owner_id):
            data = self._load()
            out = []
            for item in data["voices"]:
                if not isinstance(item, dict):
                    continue
                if item.get("deleted_at") and not include_deleted:
                    continue
                out.append(self._decorate(item))
            out.sort(key=lambda v: (v.get("created_at") or ""), reverse=True)
            return out

    def get(self, voice_id: str, *, include_deleted: bool = False) -> Optional[dict]:
        if not is_voice_id(voice_id):
            return None
        with _lock_for(self.owner_id):
            for item in self._load()["voices"]:
                if isinstance(item, dict) and item.get("id") == voice_id:
                    if item.get("deleted_at") and not include_deleted:
                        return None
                    return self._decorate(item)
        return None

    def _find(self, data: dict, voice_id: str) -> Optional[dict]:
        for item in data["voices"]:
            if isinstance(item, dict) and item.get("id") == voice_id:
                return item
        return None

    def _name_taken(self, data: dict, name: str, *, skip_id: str | None = None) -> bool:
        for item in data["voices"]:
            if not isinstance(item, dict) or item.get("deleted_at"):
                continue
            if item.get("id") == skip_id:
                continue
            if (item.get("name") or "").strip().lower() == name.strip().lower():
                return True
        return False

    # ─── 写操作 ────────────────────────────────────────────

    def add(self, content: bytes, ext: str, name: str) -> dict:
        """落盘一个新音色，返回（已装饰的）记录。重名或重 id 直接抛错。"""
        ext = (ext or "").lower()
        if ext not in ALLOWED_EXTS:
            raise VoiceStoreError(f"仅支持 {'/'.join(ALLOWED_EXTS)} 格式，收到: {ext or '无扩展名'}")
        clean = _clean_name(name)
        with _lock_for(self.owner_id):
            data = self._load()
            if self._name_taken(data, clean):
                raise VoiceNameTaken(f"已有同名音色：{clean}")
            voice_id = new_voice_id()
            while self._find(data, voice_id) is not None:   # 理论不可能，防御一下
                voice_id = new_voice_id()
            file_name = f"{voice_id}{ext}"
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / file_name).write_bytes(content)
            item = {
                "id": voice_id,
                "name": clean,
                "file": file_name,
                "ext": ext,
                "source": "upload",
                "created_at": _now(),
                "updated_at": _now(),
                "deleted_at": None,
            }
            data["voices"].append(item)
            try:
                self._save(data)
            except OSError:
                (self.dir / file_name).unlink(missing_ok=True)   # 索引没写成 ⇒ 不留孤儿文件
                raise
            logger.info(
                "[voice-store] 新增音色 owner=%s id=%s name=%r file=%s size=%d",
                self.owner_id, voice_id, clean, file_name, len(content),
            )
            return self._decorate(item)

    def rename(self, voice_id: str, new_name: str) -> dict:
        """**纯元数据改名**：只动 index.json 的 name，文件与 tts-server 都不碰。"""
        if not is_voice_id(voice_id):
            raise VoiceStoreError(f"非法音色 id：{voice_id!r}")
        clean = _clean_name(new_name)
        with _lock_for(self.owner_id):
            data = self._load()
            item = self._find(data, voice_id)
            if item is None or item.get("deleted_at"):
                raise VoiceStoreError("音色不存在")
            if self._name_taken(data, clean, skip_id=voice_id):
                raise VoiceNameTaken(f"已有同名音色：{clean}")
            old = item.get("name")
            item["name"] = clean
            item["updated_at"] = _now()
            self._save(data)
            logger.info(
                "[voice-store] 改名 owner=%s id=%s %r → %r（文件与 tts-server 均未改动）",
                self.owner_id, voice_id, old, clean,
            )
            return self._decorate(item)

    def delete(self, voice_id: str) -> dict:
        """删除音色：**物理删文件 + 索引里标 deleted_at**（用户需求第 5 条）。

        标记而不是整条移除，是为了将来能回答「这个 id 曾经存在过吗」
        （老任务里可能还存着它的路径）。列表默认过滤掉这些记录。
        """
        if not is_voice_id(voice_id):
            raise VoiceStoreError(f"非法音色 id：{voice_id!r}")
        with _lock_for(self.owner_id):
            data = self._load()
            item = self._find(data, voice_id)
            if item is None or item.get("deleted_at"):
                raise VoiceStoreError("音色不存在")
            record = self._decorate(item)
            file_name = record["file"]
            target = self.dir / file_name
            try:
                target.unlink()
                removed_file = True
            except FileNotFoundError:
                removed_file = False
            except OSError as exc:
                raise VoiceStoreError(f"删除音频文件失败：{exc}") from exc
            item["deleted_at"] = _now()
            item["updated_at"] = _now()
            self._save(data)
            logger.info(
                "[voice-store] 删除音色 owner=%s id=%s name=%r file=%s 文件已删=%s",
                self.owner_id, voice_id, record["name"], file_name, removed_file,
            )
            return record

    # ─── 迁移/清理辅助 ─────────────────────────────────────

    def known_ids(self) -> set[str]:
        """该用户 index.json 里出现过的全部 id（含已删）。"""
        with _lock_for(self.owner_id):
            return {i.get("id") for i in self._load()["voices"] if isinstance(i, dict)}


def server_filename(owner_id: str, voice_id: str, ext: str) -> str:
    """该音色在 tts-server 上的文件名：`{user_id}_{voice_id}{ext}`（用户需求第 2 条）。"""
    return f"{owner_id}_{voice_id}{ext or ''}"


def list_owner_ids() -> list[str]:
    """列出磁盘上所有用户音色目录（用于工具/统计；不含 `.tmp` 之类的噪声）。"""
    if not USER_VOICES_ROOT.exists():
        return []
    return sorted(
        p.name for p in USER_VOICES_ROOT.iterdir()
        if p.is_dir() and valid_owner_id(p.name)
    )
