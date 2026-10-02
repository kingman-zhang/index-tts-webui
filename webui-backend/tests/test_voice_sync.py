#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色按需同步（app/engines/voice_sync.py）自测。

用法（在 webui-backend 下）：
    python tests/test_voice_sync.py

五部分：
  1. 分类：按文件**实际所在目录**判定共享/独有（预设、BreezeBlue、自上传、未知）
  2. 命名：共享用原名；独有加 `__owner` 后缀；已带后缀不叠加；owner 非法字符净化
  3. 缓存：TTL 内不重复打 `/api/voices`；force 强制刷新；不同 tts_url 各自一份
  4. 合成前预检的分支：命中 / 本地有则上传 / 本地无则报错 / 探不到表则降级 /
     409 竞态 / 隔离名缺失时回退原名
  5. 仓库真实音色自检（文件存在才跑）

全程用假 httpx client，不触网、不依赖 tts-server、不读真实音色表。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines import voice_sync  # noqa: E402
from app.engines.base import VoiceRef  # noqa: E402
from app.engines.voice_sync import (  # noqa: E402
    SHARED,
    UNKNOWN,
    USER,
    VoiceUnavailable,
)

PASS = FAIL = SKIP = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         expect: {expected!r}\n         actual: {actual!r}")


def skipped(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  [skip] {name} — {why}")


# ─── 假 httpx client ───────────────────────────────────────────


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (
            json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        )

    def json(self):
        if self._payload is None:
            raise ValueError("响应不是 JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """只实现本模块用到的 get/post，签名与 httpx 的关键字调用一致。"""

    def __init__(self, voices=None, *, upload_status: int = 200, fail_list: bool = False):
        self.voices = [dict(v) for v in (voices or [])]
        self.upload_status = upload_status
        self.fail_list = fail_list
        self.list_calls = 0
        self.uploads: list[str] = []

    async def get(self, url, timeout=None):
        self.list_calls += 1
        if self.fail_list:
            raise RuntimeError("连接失败（假）")
        return FakeResponse(200, {"voices": self.voices, "count": len(self.voices)})

    async def post(self, url, files=None, data=None, timeout=None):
        target = (data or {}).get("name")
        self.uploads.append(target)
        if self.upload_status == 409:
            return FakeResponse(409, {"detail": "音色名称已存在"})
        if self.upload_status != 200:
            return FakeResponse(self.upload_status, None, text="上传失败")
        path = f"/server/voices/{target}"
        self.voices.append({"name": target, "path": path, "size_kb": 1.0})
        return FakeResponse(200, {"name": target, "path": path, "size_kb": 1.0})


class RaceClient(FakeClient):
    """模拟「提交上传的那一刻，别人刚好同名传完了」：POST 回 409，但服务器上确实有。"""

    async def post(self, url, files=None, data=None, timeout=None):
        target = (data or {}).get("name")
        self.uploads.append(target)
        self.voices.append(
            {"name": target, "path": f"/server/voices/{target}", "size_kb": 1.0}
        )
        return FakeResponse(409, {"detail": "音色名称已存在"})


# ─── 临时音色目录 ──────────────────────────────────────────────


@contextlib.contextmanager
def temp_voice_tree():
    """把模块用的音色目录换成临时目录，避免碰真实 data/。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        preset = root / "preset-voices"
        (preset / "emotions").mkdir(parents=True)
        breeze = root / "breezeblue" / "audio"
        breeze.mkdir(parents=True)
        voices = root / "voices"
        voices.mkdir(parents=True)
        saved = (
            voice_sync.PRESET_VOICES_DIR,
            voice_sync.LOCAL_VOICES_DIR,
            voice_sync.DATA_DIR,
        )
        voice_sync.PRESET_VOICES_DIR = preset
        voice_sync.LOCAL_VOICES_DIR = voices
        voice_sync.DATA_DIR = root
        try:
            yield {"root": root, "preset": preset, "breeze": breeze, "voices": voices}
        finally:
            (
                voice_sync.PRESET_VOICES_DIR,
                voice_sync.LOCAL_VOICES_DIR,
                voice_sync.DATA_DIR,
            ) = saved


URL = "http://fake-tts:8000"


# ─── 1. 分类 ──────────────────────────────────────────────────


def part1_locate() -> None:
    print("\n-- 1. 分类（按文件实际所在目录）--")
    with temp_voice_tree() as t:
        cases = [
            (t["preset"], "p.mp3", SHARED, "预设目录"),
            (t["preset"] / "emotions", "e.wav", SHARED, "预设 emotions 子目录"),
            (t["breeze"], "voc_x.wav", SHARED, "BreezeBlue 目录"),
            (t["voices"], "mine.wav", USER, "自上传目录"),
        ]
        for base, fname, kind, label in cases:
            f = base / fname
            f.write_bytes(b"RIFF")
            check(f"{label} → {kind}", voice_sync.locate_voice(str(f)).kind, kind)

        loose = t["root"] / "loose.wav"
        loose.write_bytes(b"RIFF")
        check("未知目录 → unknown", voice_sync.locate_voice(str(loose)).kind, UNKNOWN)

        loc = voice_sync.locate_voice("data/preset-voices/p.mp3")
        check("不可读路径 → 按 basename 找到替身并判 shared", loc.kind, SHARED)
        check("替身就是那个文件", loc.local_file, t["preset"] / "p.mp3")

        loc = voice_sync.locate_voice("data/voices/根本没有这个.wav")
        check("哪都没有 → unknown + 无本地文件", (loc.kind, loc.local_file), (UNKNOWN, None))

        # ── 新结构用户音色：data/voices/<user_id>/<voice_id>.wav ──
        sub = t["voices"] / "u_42"
        sub.mkdir()
        new_style = sub / "voc_0123456789ab.wav"
        new_style.write_bytes(b"RIFF")
        check("新结构子目录 → 仍是 user", voice_sync.locate_voice(str(new_style)).kind, USER)
        stale = voice_sync.locate_voice("data/voices/voc_0123456789ab.wav")  # 陈旧路径只留文件名
        check("只给文件名也能下潜找到（新结构）", stale.local_file, new_style)
        check("下潜找到的仍判 user", stale.kind, USER)
        check("_is_id_layout 认得子目录", voice_sync._is_id_layout(new_style), True)
        check("_is_id_layout 不认老结构平铺", voice_sync._is_id_layout(t["voices"] / "mine.wav"), False)
        check("_is_id_layout 对非用户目录为假", voice_sync._is_id_layout(t["preset"] / "p.mp3"), False)


# ─── 2. 命名 ──────────────────────────────────────────────────


def part2_naming() -> None:
    print("\n-- 2. 服务器上的文件名 --")
    check("共享类 → 原名", voice_sync.target_server_name("p.mp3", SHARED, "u_1"), "p.mp3")
    check("独有类 → 加 owner", voice_sync.target_server_name("mine.wav", USER, "u_1"), "mine__u_1.wav")
    check("未知类 → 按独有处理", voice_sync.target_server_name("x.wav", UNKNOWN, "u_1"), "x__u_1.wav")
    check("拿不到 owner → 原名", voice_sync.target_server_name("mine.wav", USER, None), "mine.wav")
    check("共享类即使有 owner 也不改名", voice_sync.target_server_name("p.mp3", SHARED, "u_1"), "p.mp3")
    check(
        "已带本 owner 后缀 → 不叠加（防二次运行）",
        voice_sync.target_server_name("mine__u_1.wav", USER, "u_1"),
        "mine__u_1.wav",
    )
    check(
        "带的是别人后缀 → 仍按自己加",
        voice_sync.target_server_name("mine__u_9.wav", USER, "u_1"),
        "mine__u_9__u_1.wav",
    )
    check(
        "owner 含非法字符 → 净化",
        voice_sync.target_server_name("mine.wav", USER, "a/b c"),
        "mine__a_b_c.wav",
    )
    check("无扩展名也能处理", voice_sync.target_server_name("mine", USER, "u_1"), "mine__u_1")
    # ── 新结构（id 化）用户音色：2026-10-02 起 data/voices/<user_id>/<voice_id>.wav ──
    check(
        "新结构 → {user_id}_{voice_id}",
        voice_sync.target_server_name("voc_0123456789ab.wav", USER, "u_42", id_layout=True),
        "u_42_voc_0123456789ab.wav",
    )
    check(
        "新结构：已带本 owner 前缀 → 不叠加",
        voice_sync.target_server_name("u_42_voc_0123456789ab.wav", USER, "u_42", id_layout=True),
        "u_42_voc_0123456789ab.wav",
    )
    check(
        "新结构：带的是别人前缀 → 仍按自己加（宁可重复也不串音）",
        voice_sync.target_server_name("u_9_voc_01.wav", USER, "u_42", id_layout=True),
        "u_42_u_9_voc_01.wav",
    )
    check(
        "id_layout 对共享类无影响",
        voice_sync.target_server_name("p.mp3", SHARED, "u_42", id_layout=True),
        "p.mp3",
    )
    check(
        "id_layout 但拿不到 owner → 原名",
        voice_sync.target_server_name("voc_01.wav", USER, None, id_layout=True),
        "voc_01.wav",
    )
    check(
        "老结构与新结构不会互相误判（同一 owner）",
        (
            voice_sync.target_server_name("mine.wav", USER, "u_42"),
            voice_sync.target_server_name("mine.wav", USER, "u_42", id_layout=True),
        ),
        ("mine__u_42.wav", "u_42_mine.wav"),
    )


# ─── 3. 缓存 ──────────────────────────────────────────────────


def part3_cache() -> None:
    print("\n-- 3. 服务器音色表缓存 --")
    voice_sync.reset_cache()
    c = FakeClient([{"name": "p.mp3", "path": "/s/voices/p.mp3"}])

    first = asyncio.run(voice_sync._load_server_voices(c, URL))
    second = asyncio.run(voice_sync._load_server_voices(c, URL))
    check("TTL 内只打一次列表", c.list_calls, 1)
    check("两次结果一致", first, second)
    check("解析成 {文件名: 路径}", first, {"p.mp3": "/s/voices/p.mp3"})

    asyncio.run(voice_sync._load_server_voices(c, URL, force=True))
    check("force 会再打一次", c.list_calls, 2)

    asyncio.run(voice_sync._load_server_voices(c, "http://other:8000"))
    check("不同 tts_url 各自一份缓存", c.list_calls, 3)

    voice_sync.reset_cache(URL)
    asyncio.run(voice_sync._load_server_voices(c, "http://other:8000"))
    check("按 url 清缓存后其他 url 仍命中", c.list_calls, 3)


# ─── 4. 合成前预检 ────────────────────────────────────────────


def part4_ensure() -> None:
    print("\n-- 4. 合成前预检 --")

    # 4.1 共享类命中：服务器已有，不上传、不额外刷新
    voice_sync.reset_cache()
    with temp_voice_tree() as t:
        (t["preset"] / "p.mp3").write_bytes(b"RIFF")
        c = FakeClient([{"name": "p.mp3", "path": "/s/voices/p.mp3"}])
        v = VoiceRef(tts_path="/s/voices/p.mp3", display_name="p.mp3", owner_id="u_1")
        got = asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
    check("命中：返回服务器路径", got, "/s/voices/p.mp3")
    check("命中：不上传", c.uploads, [])
    check("命中：只打一次列表（不 force）", c.list_calls, 1)

    # 4.2 独有类、本地有文件 → 按 owner 命名上传
    voice_sync.reset_cache()
    with temp_voice_tree() as t:
        local = t["voices"] / "mine.wav"
        local.write_bytes(b"RIFF")
        c = FakeClient([])
        v = VoiceRef(tts_path=str(local), local_path=str(local),
                     display_name="mine.wav", owner_id="u_42")
        got = asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
    check("独有类：上传后返回服务器路径", got, "/server/voices/mine__u_42.wav")
    check("独有类：上传名带 owner", c.uploads, ["mine__u_42.wav"])

    # 4.3 同一音色第二次 → 命中缓存，不再上传、不再查表
    voice_sync.reset_cache()
    with temp_voice_tree() as t:
        local = t["voices"] / "mine.wav"
        local.write_bytes(b"RIFF")
        c = FakeClient([])
        v = VoiceRef(tts_path=str(local), local_path=str(local), owner_id="u_42")
        a = asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
        snapshot = (len(c.uploads), c.list_calls)
        b = asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
    check("第二次：不再上传/查表", (len(c.uploads), c.list_calls), snapshot)
    check("第二次：返回同一路径", a, b)

    # 4.3b 新结构用户音色（data/voices/<user_id>/<voice_id>.wav）→ {user_id}_{voice_id}
    voice_sync.reset_cache()
    with temp_voice_tree() as t:
        sub = t["voices"] / "u_42"
        sub.mkdir()
        local = sub / "voc_0123456789ab.wav"
        local.write_bytes(b"RIFF")
        c = FakeClient([])
        v = VoiceRef(tts_path=str(local), local_path=str(local),
                     display_name="我的音色A", owner_id="u_42")
        got = asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
    check("新结构：上传后返回服务器路径", got, "/server/voices/u_42_voc_0123456789ab.wav")
    check("新结构：上传名 = {user_id}_{voice_id}", c.uploads, ["u_42_voc_0123456789ab.wav"])

    # 4.4 本地与服务器都没有 → 明确报错（而不是含糊的 400）
    voice_sync.reset_cache()
    c = FakeClient([])
    v = VoiceRef(tts_path="/nowhere/missing__u_9.wav", display_name="missing.wav", owner_id="u_9")
    try:
        asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
        check("两边都缺 → 抛 VoiceUnavailable", "没有抛异常", "VoiceUnavailable")
    except VoiceUnavailable as exc:
        check("两边都缺 → 抛 VoiceUnavailable", "missing__u_9.wav" in str(exc), True)

    # 4.5 探不到音色表 → 降级为原路径（保持旧行为，不把任务判死）
    voice_sync.reset_cache()
    c = FakeClient([], fail_list=True)
    v = VoiceRef(tts_path="data/voices/mine.wav", display_name="mine.wav", owner_id="u_1")
    got = asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
    check("探不到表 → 原路径降级", got, "data/voices/mine.wav")
    check("探不到表 → 不上传", c.uploads, [])

    # 4.6 409 竞态 → 复用服务器现有路径
    voice_sync.reset_cache()
    with temp_voice_tree() as t:
        local = t["voices"] / "mine.wav"
        local.write_bytes(b"RIFF")
        c = RaceClient([])
        v = VoiceRef(tts_path=str(local), local_path=str(local), owner_id="u_7")
        got = asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
    check("409 竞态 → 复用服务器已有", got, "/server/voices/mine__u_7.wav")

    # 4.7 隔离名不在、但服务器有原名版本，且本地没有文件 → 回退复用原名
    voice_sync.reset_cache()
    with temp_voice_tree():
        c = FakeClient([{"name": "mine.wav", "path": "/s/voices/mine.wav"}])
        v = VoiceRef(tts_path="/old/machine/mine.wav", display_name="mine.wav", owner_id="u_5")
        got = asyncio.run(voice_sync.ensure_voice_on_server(c, URL, v))
    check("隔离名缺失 → 回退复用原名版本", got, "/s/voices/mine.wav")
    check("回退时不上传", c.uploads, [])

    # 4.8 没有可用路径 → 报错
    voice_sync.reset_cache()
    c = FakeClient([])
    try:
        asyncio.run(voice_sync.ensure_voice_on_server(c, URL, VoiceRef(display_name="空")))
        check("无路径 → 抛 VoiceUnavailable", "没有抛异常", "VoiceUnavailable")
    except VoiceUnavailable:
        check("无路径 → 抛 VoiceUnavailable", True, True)


# ─── 5. 真实数据自检 ──────────────────────────────────────────


def part5_real() -> None:
    print("\n-- 5. 仓库真实音色（文件存在才跑）--")
    from app.config import LOCAL_VOICES_DIR, PRESET_VOICES_DIR

    preset_files = sorted(PRESET_VOICES_DIR.glob("*.mp3")) + sorted(PRESET_VOICES_DIR.glob("*.wav"))
    if preset_files:
        check(
            f"真实预设音色 {preset_files[0].name} → shared",
            voice_sync.locate_voice(str(preset_files[0])).kind,
            SHARED,
        )
    else:
        skipped("真实预设音色", "目录为空")

    breeze_dir = voice_sync.DATA_DIR / "breezeblue" / "audio"
    breeze_files = sorted(breeze_dir.glob("*.wav"))
    if breeze_files:
        check(
            f"真实 BreezeBlue {breeze_files[0].name} → shared",
            voice_sync.locate_voice(str(breeze_files[0])).kind,
            SHARED,
        )
    else:
        skipped("真实 BreezeBlue", "目录为空")

    local_files = [f for f in sorted(LOCAL_VOICES_DIR.glob("*")) if f.is_file()]
    if local_files:
        check(
            f"真实自上传 {local_files[0].name} → user",
            voice_sync.locate_voice(str(local_files[0])).kind,
            USER,
        )
    else:
        skipped("真实自上传音色", "目录为空")

    # 命名规则对真实文件的落点（用预设音色，共享类应保持原名）
    if preset_files:
        p = preset_files[0]
        check(
            "真实共享音色保持原名（含中文）",
            voice_sync.target_server_name(p.name, SHARED, "u_41588d678184"),
            p.name,
        )


def main() -> int:
    os.environ.pop("VOICE_FALLBACK_DIRS", None)
    print("=" * 62)
    print("音色按需同步（voice_sync）自测")
    print("=" * 62)
    part1_locate()
    part2_naming()
    part3_cache()
    part4_ensure()
    part5_real()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项，跳过 {SKIP} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
