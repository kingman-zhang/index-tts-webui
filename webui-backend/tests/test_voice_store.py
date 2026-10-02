#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户音色库（app/voice_store.py）自测。

用法（在 webui-backend 下）：
    python tests/test_voice_store.py

## 为什么锁这些行为（2026-10-02 与用户确认的改造）

「我的音色」从「文件名即身份」改成「id 即身份」后，有三条性质**必须**成立，
否则线上会出难以排查的问题：

1. **改名不动文件**（用户需求第 7 条）—— server 上的文件名是 `{user_id}_{voice_id}{ext}`，
   与显示名无关；改名只改 `index.json`。测试直接断言「改名前后文件 mtime 与名字都不变」。
2. **删文件 + 索引标 deleted_at**（第 5 条）—— 标记而不是整条删，是为了将来能回答
   「这个 id 曾经存在过吗」（老任务里可能还存着它的路径）。
3. **索损坏不能静默当空**—— 一旦当成空，下一次上传会**覆盖整个索引**，
   把用户已有的音色记录全部丢掉。这条是「宁可炸也不要静默丢数据」。

另外覆盖：路径安全（owner_id 白名单）、同用户重名拒绝、不同用户可同名、
音色 id 与 BreezeBlue 的 base36 id 不混淆。

全程写临时目录，绝不碰仓库真实的 data/voices/。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ⚠️ 必须在 import app.* 之前决定 DATA_DIR：USER_VOICES_ROOT 在 config 导入时就定下来了
if not os.environ.get("DATA_DIR"):
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="test-voice-store-")

from app import voice_store  # noqa: E402
from app.voice_store import (  # noqa: E402
    UserVoiceStore,
    VoiceNameTaken,
    VoiceStoreError,
    is_voice_id,
    new_voice_id,
    server_filename,
)

PASS = FAIL = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         expect: {expected!r}\n         actual: {actual!r}")


def check_true(name: str, cond: bool) -> None:
    check(name, bool(cond), True)


OWNER = "u_41588d678184"
OTHER = "u_aabbccddeeff"
WAV = b"RIFF" + b"\x00" * 64


def main() -> int:
    print("── 1. 音色 id ──")
    vid = new_voice_id()
    check_true("id 形态 voc_+12hex", is_voice_id(vid))
    check("长度", len(vid), len("voc_") + 12)
    check("BreezeBlue 的 base36 id 不算音色 id", is_voice_id("voc_534z3zu7d53k"), False)
    check("空串不算", is_voice_id(""), False)
    check("server 文件名拼接", server_filename("u_ab", "voc_0123456789ab", ".wav"),
          "u_ab_voc_0123456789ab.wav")

    print("── 2. 新增 ──")
    store = UserVoiceStore(OWNER)
    rec = store.add(WAV, ".wav", "我的音色A")
    check_true("落在用户目录下", rec["path"].startswith(str(Path(os.environ["DATA_DIR"]) / "voices" / OWNER)))
    check("文件名 = id + 扩展名", rec["file"], f"{rec['voice_id']}.wav")
    check("显示名保留", rec["name"], "我的音色A")
    check("server 文件名", rec["server_name"], f"{OWNER}_{rec['voice_id']}.wav")
    check("preview_name 是文件名（试听要用它）", rec["preview_name"], rec["file"])
    check_true("磁盘上真的有文件", Path(rec["path"]).is_file())
    check("size_kb 现算（按磁盘真实大小）", rec["size_kb"], round(len(WAV) / 1024, 1))
    check_true("size_kb 是数字", isinstance(rec["size_kb"], float))

    index = json.loads((Path(os.environ["DATA_DIR"]) / "voices" / OWNER / "index.json").read_text(encoding="utf-8"))
    check("索引里 name 落盘", index["voices"][0]["name"], "我的音色A")
    check("索引里没存 size_kb（避免与磁盘不一致）", "size_kb" in index["voices"][0], False)
    check("deleted_at 初始为 null", index["voices"][0]["deleted_at"], None)

    print("── 3. 校验 ──")
    try:
        store.add(WAV, ".wav", "我的音色A")
        check("重名应当抛错", "no-raise", "VoiceNameTaken")
    except VoiceNameTaken:
        check("重名抛 VoiceNameTaken", "VoiceNameTaken", "VoiceNameTaken")
    for bad in ("a/b", "a\\b", "  ", "x" * 61):
        try:
            store.add(WAV, ".wav", bad)
            check(f"非法名 {bad[:6]!r} 应当抛错", "no-raise", "VoiceStoreError")
        except VoiceStoreError:
            check(f"非法名 {bad[:6]!r} 被拒", "VoiceStoreError", "VoiceStoreError")
    try:
        store.add(WAV, ".txt", "格式不对")
        check("非法扩展名应当抛错", "no-raise", "VoiceStoreError")
    except VoiceStoreError:
        check("非法扩展名被拒", "VoiceStoreError", "VoiceStoreError")
    try:
        UserVoiceStore("../../etc")
        check("非法 owner_id 应当抛错", "no-raise", "VoiceStoreError")
    except VoiceStoreError:
        check("非法 owner_id 被拒（路径穿越）", "VoiceStoreError", "VoiceStoreError")

    print("── 4. 列表与隔离 ──")
    check("自己看得到自己", len(store.list()), 1)
    check("别人看不到", len(UserVoiceStore(OTHER).list()), 0)
    other_rec = UserVoiceStore(OTHER).add(WAV, ".mp3", "我的音色A")   # 同名，属于别人
    check_true("不同用户可以同名", other_rec["voice_id"] != rec["voice_id"])
    check("各自仍只有 1 条", (len(store.list()), len(UserVoiceStore(OTHER).list())), (1, 1))

    print("── 5. 改名 = 纯元数据 ──")
    before = Path(rec["path"])
    mtime_before = before.stat().st_mtime_ns
    time.sleep(0.01)
    renamed = store.rename(rec["voice_id"], "改过的名字")
    check("显示名已改", renamed["name"], "改过的名字")
    check("文件名不变", renamed["file"], rec["file"])
    check("路径不变", renamed["path"], rec["path"])
    check("server 文件名**不变**（这就是需求 7 的依据）", renamed["server_name"], rec["server_name"])
    check("文件 mtime 未变（真的没碰文件）", before.stat().st_mtime_ns, mtime_before)
    check("旧名字已释放，可以再被使用", store.rename(rec["voice_id"], "我的音色A")["name"], "我的音色A")
    try:
        UserVoiceStore(OTHER).rename(rec["voice_id"], "x")   # 不是他的 id
        check("改别人的音色应当抛错", "no-raise", "VoiceStoreError")
    except VoiceStoreError:
        check("改别人的音色被拒", "VoiceStoreError", "VoiceStoreError")

    print("── 6. 删除 ──")
    victim = store.add(WAV, ".wav", "待删除")
    victim_path = Path(victim["path"])
    check_true("删除前文件在", victim_path.is_file())
    deleted = store.delete(victim["voice_id"])
    check("返回被删记录", deleted["voice_id"], victim["voice_id"])
    check("文件已删", victim_path.exists(), False)
    check("列表里消失", [v["voice_id"] for v in store.list()].count(victim["voice_id"]), 0)
    check("include_deleted 能看到（留痕）",
          [v["voice_id"] for v in store.list(include_deleted=True)].count(victim["voice_id"]), 1)
    try:
        store.delete(victim["voice_id"])
        check("重复删除应当抛错", "no-raise", "VoiceStoreError")
    except VoiceStoreError:
        check("重复删除被拒", "VoiceStoreError", "VoiceStoreError")

    print("── 7. 索引损坏不静默 ──")
    broken_owner = "u_000000000001"
    broken = UserVoiceStore(broken_owner)
    broken.dir.mkdir(parents=True, exist_ok=True)
    broken.index_path.write_text("{ 这不是 JSON", encoding="utf-8")
    try:
        broken.list()
        check("坏索引应当抛错（不能当空）", "no-raise", "VoiceStoreError")
    except VoiceStoreError:
        check("坏索引抛错而非静默丢数据", "VoiceStoreError", "VoiceStoreError")

    print("── 8. list_owner_ids ──")
    owners = voice_store.list_owner_ids()
    check_true("能列出两个用户目录", OWNER in owners and OTHER in owners)
    check_true("只列合法 owner 名", all(o.startswith("u_") for o in owners))

    print(f"\n{'=' * 46}\nTOTAL {PASS + FAIL}  PASS {PASS}  FAIL {FAIL}\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
