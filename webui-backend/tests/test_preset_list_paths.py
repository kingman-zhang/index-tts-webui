#!/usr/bin/env python3
"""`/api/preset-voices` 的 path 现算单测（2026-10-15，方案 A）。

背景：manifest.json 是在生成机（2026-09-22，compshare pod → 本机 Mac）上一次性
生成的静态文件，`path` 烘焙的是**生成那一刻的绝对路径**，换机器/换容器必死 ——
此前没炸全靠 `engines/voice_sync.locate_voice()` 按 basename 兜底。

本文件锁的行为（改造后）：
  1. manifest 存在 ⇒ 响应里每条 path 都是**本机现算**的活路径（female/male 在根
     目录、emotion 在 `emotions/` 子目录），烘焙的死路径**绝不透出**；
  2. manifest 的元数据字段（description / tones_zh / voice_category / age / size_kb）
     原样保留，一个不丢、一个不改；
  3. manifest 没有 categories 键 / categories 形状不对 ⇒ 原样返回，**不 500**；
  4. manifest 不存在 ⇒ 目录扫描 fallback 行为不变（女- → female、男- → male、
     emotions/ → emotion，path 本来就是现算的活路径）；
  5. `/api/preset-voices/{category}` 子路由同样吃到现算路径。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="wb-preset-list-test-")
os.environ["DATA_DIR"] = TMP
os.environ["TTS_URL"] = "http://127.0.0.1:59999"
os.environ["MEMBER_ENFORCE"] = "0"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import PRESET_VOICES_DIR  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


PRESET_DIR = Path(TMP) / "preset-voices"
EMO_DIR = PRESET_DIR / "emotions"
EMO_DIR.mkdir(parents=True, exist_ok=True)

# 三类各造一个真实音频文件（有真文件，path 才算「活」）
F_NAME, M_NAME, E_NAME = "女-测试甲.mp3", "男-测试乙.mp3", "抖音爆款-情感丙.mp3"
(PRESET_DIR / F_NAME).write_bytes(b"\xff\xfb" + b"\x00" * 512)
(PRESET_DIR / M_NAME).write_bytes(b"\xff\xfb" + b"\x00" * 512)
(EMO_DIR / E_NAME).write_bytes(b"\xff\xfb" + b"\x00" * 512)

# 烘焙的死路径：生成机上的绝对路径（本机/容器里都不存在）
DEAD_F = "/Users/someone-else/old-machine/data/preset-voices/" + F_NAME
DEAD_M = "/Users/someone-else/old-machine/data/preset-voices/" + M_NAME
DEAD_E = "/Users/someone-else/old-machine/data/preset-voices/emotions/" + E_NAME


def write_manifest(payload: dict) -> None:
    (PRESET_DIR / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


try:
    # ── ① manifest 存在：path 全部现算，死路径绝不透出 ────────────────────
    print("① manifest 的烘焙 path 被替换为本机活路径")
    write_manifest({
        "version": "1.0",
        "source": "https://old-pod.example/",
        "count": 3,
        "categories": {
            "female": [{"name": F_NAME, "path": DEAD_F, "size_kb": 1.0,
                        "voice_category": "roleplay", "age": "young",
                        "tones_zh": ["俏皮"], "description": "测试甲"}],
            "male": [{"name": M_NAME, "path": DEAD_M, "size_kb": 2.0}],
            "emotion": [{"name": E_NAME, "path": DEAD_E, "size_kb": 3.0}],
        },
    })
    r = client.get("/api/preset-voices")
    check("① HTTP 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    d = r.json()
    check("① female path = 本机根目录活路径",
          d["categories"]["female"][0]["path"] == str(PRESET_DIR / F_NAME),
          d["categories"]["female"][0]["path"])
    check("① male path = 本机根目录活路径",
          d["categories"]["male"][0]["path"] == str(PRESET_DIR / M_NAME),
          d["categories"]["male"][0]["path"])
    check("① emotion path = emotions/ 子目录活路径",
          d["categories"]["emotion"][0]["path"] == str(EMO_DIR / E_NAME),
          d["categories"]["emotion"][0]["path"])
    check("① 全响应中不再出现烘焙死路径", DEAD_F not in r.text and DEAD_E not in r.text)

    # ── ② 元数据字段原样保留 ─────────────────────────────────────────────
    print("② manifest 元数据字段原样保留")
    f0 = d["categories"]["female"][0]
    check("② description 不丢", f0.get("description") == "测试甲", str(f0))
    check("② tones_zh 不丢", f0.get("tones_zh") == ["俏皮"], str(f0))
    check("② voice_category / age 不丢",
          f0.get("voice_category") == "roleplay" and f0.get("age") == "young", str(f0))
    check("② size_kb 不丢", f0.get("size_kb") == 1.0, str(f0))
    check("② 顶层 version / source / count 不动",
          d["version"] == "1.0" and d["source"] == "https://old-pod.example/"
          and d["count"] == 3, str({k: d.get(k) for k in ("version", "source", "count")}))

    # ── ③ 形状不对的 manifest 不 500 ────────────────────────────────────
    print("③ 异常形状的 manifest 原样返回（不 500）")
    write_manifest({"version": "1.0"})                       # 没有 categories
    r = client.get("/api/preset-voices")
    check("③ 缺 categories → 200 + 原样", r.status_code == 200
          and r.json() == {"version": "1.0"}, r.text[:160])
    write_manifest({"categories": "not-a-dict"})             # categories 不是 dict
    r = client.get("/api/preset-voices")
    check("③ categories 非字典 → 200 + 原样", r.status_code == 200
          and r.json() == {"categories": "not-a-dict"}, r.text[:160])
    write_manifest({"categories": {"female": "not-a-list",   # 条目形状各坏一遍
                                   "male": [None, 42, {"name": M_NAME, "path": DEAD_M}]}})
    r = client.get("/api/preset-voices")
    check("③ 坏条目被跳过、好条目照常现算 → 200",
          r.status_code == 200
          and r.json()["categories"]["male"][2]["path"] == str(PRESET_DIR / M_NAME),
          r.text[:160])

    # ── ④ manifest 不存在：目录扫描 fallback 行为不变 ────────────────────
    print("④ 无 manifest → fallback 扫描行为不变")
    (PRESET_DIR / "manifest.json").unlink()
    r = client.get("/api/preset-voices")
    d = r.json()
    check("④ HTTP 200 且 count=3", r.status_code == 200 and d.get("count") == 3,
          r.text[:200])
    cats = d["categories"]
    check("④ 女- 前缀 → female、男- → male、emotions/ → emotion",
          [i["name"] for i in cats["female"]] == [F_NAME]
          and [i["name"] for i in cats["male"]] == [M_NAME]
          and [i["name"] for i in cats["emotion"]] == [E_NAME], str(cats))
    check("④ fallback 的 path 本来就是活路径",
          cats["female"][0]["path"] == str(PRESET_DIR / F_NAME)
          and cats["emotion"][0]["path"] == str(EMO_DIR / E_NAME), str(cats))

    # ── ⑤ 子路由 /{category} 同样吃到现算路径 ────────────────────────────
    print("⑤ /api/preset-voices/{category} 同样现算")
    write_manifest({
        "count": 3,
        "categories": {"female": [{"name": F_NAME, "path": DEAD_F, "size_kb": 1.0}]},
    })
    r = client.get("/api/preset-voices/female")
    d = r.json()
    check("⑤ 子路由 HTTP 200 且 path 为活路径",
          r.status_code == 200 and d["voices"][0]["path"] == str(PRESET_DIR / F_NAME),
          r.text[:200])
    check("⑤ 不存在的分类 → 404", client.get("/api/preset-voices/nope").status_code == 404)

    # ── ⑥ manifest 条目不带 path 键（2026-10-15 清理后的新常态）─────────
    print("⑥ manifest 无 path 键 → 响应照样补出活路径")
    write_manifest({
        "count": 3,
        "categories": {"female": [{"name": F_NAME, "size_kb": 1.0, "description": "无path"}],
                       "emotion": [{"name": E_NAME, "size_kb": 2.0}]},
    })
    r = client.get("/api/preset-voices")
    d = r.json()
    check("⑥ HTTP 200 且 female/emotion 均有活 path",
          r.status_code == 200
          and d["categories"]["female"][0]["path"] == str(PRESET_DIR / F_NAME)
          and d["categories"]["emotion"][0]["path"] == str(EMO_DIR / E_NAME),
          r.text[:200])
    check("⑥ 元数据字段保留", d["categories"]["female"][0]["description"] == "无path",
          str(d["categories"]["female"][0]))

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
