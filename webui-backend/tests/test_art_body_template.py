#!/usr/bin/env python3
"""autodl.art 请求体模板的离线回归锁（2026-10-15，生产故障 q_a1edf18f99）。

故障事实：平台把 `emo_surprised` 从 enum 字符串 `"0"` 悄悄改成 **number 滑杆**
（0–1.4，与其余 8 个滑杆同口径）。我们的模板还写字符串 `"0"` ⇒ 平台一律回
`参数值非法`（**响应里不点字段名**），于是**每一个 art 提交都失败**。
用最小请求体二分后确认：传 int `0` 成功、传 `"0"` 失败、传 `1.5` 时平台才明说
「emo_surprised 的值: 1.5 大于最大值 1.4」。

本文件锁的行为（全部离线，不发任何网络请求）：
  1. 两处模板副本（包内 `app/engines/` 与仓库根 `tools/`）内容**一致**
     —— 这是 indextts_art 文件头写明的约定，此前没有任何测试保证；
  2. 8 个情绪滑杆一律 **number**（int/float，非 bool、非 str）：字符串会被平台拒；
  3. `emo_surprised` 尤其不能是字符串 —— 就是这次挂掉的那一格；
  4. `emo_random` 是 bool，`emo_control_method` 只取文档里的三档之一；
  5. 两个必填字段保持 `{{AUDIO}}` / `{{TEXT}}` 占位符；
  6. `_build_body` 产出：无标签走默认档、有标签走向量档且滑杆全为数字、
     surprised 标签**已启用**（2026-10-15 起与其余标签同路径：向量档 + 滑杆 1.0）；
  7. 除 `neutral` 外，**每个标签都映射到 8 个滑杆之一** —— 防止再出现
     「某标签被静默降级为 None」这种缺口（surprised 曾是）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="wb-art-body-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["TTS_URL"] = "http://127.0.0.1:59999"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.engines.indextts_art import _LABEL_TO_FIELD, IndexttsArtEngine  # noqa: E402

PKG_TEMPLATE = BACKEND_ROOT / "app" / "engines" / "autodl_body.json"
TOOLS_TEMPLATE = BACKEND_ROOT.parent / "tools" / "autodl_body.json"

# 平台 8 个情绪滑杆（emo_surprised 在里面 —— 它曾经是 enum 字符串，现为 number）
SLIDER_FIELDS = (
    "emo_happy", "emo_angry", "emo_sad", "emo_afraid",
    "emo_disgusted", "emo_melancholic", "emo_surprised", "emo_calm",
)
CONTROL_METHODS = {"与音色参考音频相同", "使用情感参考音频", "使用情感向量控制"}

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")
    return cond


def _is_number(value) -> bool:
    """number 口径：int/float 且**不是** bool（JSON 里 true/false 与数字互不通用）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _engine() -> IndexttsArtEngine:
    return IndexttsArtEngine(token="offline-test")


def _fake_voice(tmp: Path) -> str:
    """造一个 .mp3 后缀的文件：走 audio_data_uri 的直通分支，不触发 ffmpeg 转码。"""
    p = tmp / "ref.mp3"
    p.write_bytes(b"ID3\x03\x00\x00\x00offline")
    return str(p)


def test_templates_in_sync() -> bool:
    print("\n[1] 两处模板副本一致")
    ok_a = check("包内模板存在", PKG_TEMPLATE.is_file(), str(PKG_TEMPLATE))
    ok_b = check("tools/ 同步副本存在", TOOLS_TEMPLATE.is_file(), str(TOOLS_TEMPLATE))
    if not (ok_a and ok_b):
        return False
    pkg = json.loads(PKG_TEMPLATE.read_text(encoding="utf-8"))
    tools = json.loads(TOOLS_TEMPLATE.read_text(encoding="utf-8"))
    return check("内容逐字段相同（改模板必须两处一起改）", pkg == tools,
                 f"包内={pkg} tools={tools}")


def test_sliders_are_numbers() -> bool:
    print("\n[2] 8 个情绪滑杆都是 number")
    body = json.loads(PKG_TEMPLATE.read_text(encoding="utf-8"))
    ok = True
    for field in SLIDER_FIELDS:
        if field not in body:
            ok = check(f"{field} 存在", False, "缺字段") and ok
            continue
        ok = check(f"{field} 是 number（实为 {type(body[field]).__name__}: {body[field]!r}）",
                   _is_number(body[field])) and ok
    return ok


def test_surprised_is_not_string() -> bool:
    """本轮故障的直接回归锁：字符串会让平台回「参数值非法」且不点字段名。"""
    print("\n[3] emo_surprised 不是字符串（q_a1edf18f99 的根因）")
    body = json.loads(PKG_TEMPLATE.read_text(encoding="utf-8"))
    value = body.get("emo_surprised")
    ok = check("emo_surprised 存在", "emo_surprised" in body)
    ok = check(f"不是 str（实为 {type(value).__name__}: {value!r}）",
               not isinstance(value, str)) and ok
    ok = check("是 number", _is_number(value)) and ok
    return ok


def test_other_fields_shape() -> bool:
    print("\n[4] 其它字段形状")
    body = json.loads(PKG_TEMPLATE.read_text(encoding="utf-8"))
    ok = check("emo_control_method 取三档之一",
               body.get("emo_control_method") in CONTROL_METHODS,
               repr(body.get("emo_control_method")))
    ok = check("emo_random 是 bool", isinstance(body.get("emo_random"), bool)) and ok
    ok = check("prompt_simple 是 {{AUDIO}} 占位符", body.get("prompt_simple") == "{{AUDIO}}") and ok
    ok = check("prompt_text 是 {{TEXT}} 占位符", body.get("prompt_text") == "{{TEXT}}") and ok
    return ok


def test_build_body_default_branch() -> bool:
    print("\n[5] 无情绪标签 → 默认档，滑杆保持数字")
    with tempfile.TemporaryDirectory() as tmp:
        engine = _engine()
        req = _req(_fake_voice(Path(tmp)), text="你好，这是一次离线测试。")
        body = engine._build_body(req)
    ok = check("emo_control_method 未被改动（与音色参考音频相同）",
               body["emo_control_method"] == "与音色参考音频相同")
    ok = check("滑杆全为 number", all(_is_number(body[f]) for f in SLIDER_FIELDS)) and ok
    ok = check("emo_surprised 仍是 number（默认档下也必须是）",
               _is_number(body["emo_surprised"])) and ok
    ok = check("prompt_text 已替换、无残留占位符",
               body["prompt_text"] == "你好，这是一次离线测试。") and ok
    ok = check("prompt_simple 已替换为 data URI",
               str(body["prompt_simple"]).startswith("data:audio/mpeg;base64,")) and ok
    return ok


def test_build_body_vector_branch() -> bool:
    print("\n[6] 有情绪标签（happy）→ 向量档 + 滑杆数字")
    with tempfile.TemporaryDirectory() as tmp:
        engine = _engine()
        req = _req(_fake_voice(Path(tmp)), text="测试", emotion="happy")
        body = engine._build_body(req)
    ok = check("切到「使用情感向量控制」", body["emo_control_method"] == "使用情感向量控制")
    ok = check("emo_happy == 1.0", body["emo_happy"] == 1.0)
    ok = check("其余滑杆为 0（数字）",
               all(body[f] == 0 and _is_number(body[f]) for f in SLIDER_FIELDS if f != "emo_happy"))
    ok = check("emo_surprised 仍为 number 0（不许退回字符串）",
               _is_number(body["emo_surprised"]) and body["emo_surprised"] == 0) and ok
    return ok


def test_surprised_label_enabled() -> bool:
    print("\n[7] surprised 标签已启用（2026-10-15 起与其余标签同路径）")
    with tempfile.TemporaryDirectory() as tmp:
        engine = _engine()
        req = _req(_fake_voice(Path(tmp)), text="测试", emotion="surprised")
        body = engine._build_body(req)
    ok = check("映射为 emo_surprised",
               _LABEL_TO_FIELD.get("surprised") == "emo_surprised")
    ok = check("切到「使用情感向量控制」",
               body["emo_control_method"] == "使用情感向量控制") and ok
    ok = check("emo_surprised == 1.0 且为 number",
               body["emo_surprised"] == 1.0 and _is_number(body["emo_surprised"])) and ok
    ok = check("其余滑杆为 0（数字）",
               all(body[f] == 0 and _is_number(body[f])
                   for f in SLIDER_FIELDS if f != "emo_surprised")) and ok
    return ok


def test_all_labels_mapped() -> bool:
    """防「静默降级」缺口：除 neutral 外每个标签都必须落到一个真实滑杆。"""
    print("\n[8] 除 neutral 外，所有标签都映射到合法滑杆")
    ok = True
    for label, field in _LABEL_TO_FIELD.items():
        if label == "neutral":
            ok = check("neutral 映射为 None（保持全零滑杆）", field is None) and ok
            continue
        ok = check(f"{label} → {field!r}（须是 8 滑杆之一）",
                   field in SLIDER_FIELDS) and ok
    return ok


def _req(audio: str, text: str, emotion: str | None = None):
    from app.engines.base import SegmentRequest, VoiceRef

    return SegmentRequest(text=text,
                          voice=VoiceRef(local_path=audio, display_name="离线测试音色"),
                          emotion_label=emotion)


def main() -> int:
    print(f"DATA_DIR = {_TMP}")
    results = [
        test_templates_in_sync(),
        test_sliders_are_numbers(),
        test_surprised_is_not_string(),
        test_other_fields_shape(),
        test_build_body_default_branch(),
        test_build_body_vector_branch(),
        test_surprised_label_enabled(),
        test_all_labels_mapped(),
    ]
    print(f"\n===== {PASS}/{PASS + FAIL} passed =====")
    return 0 if all(results) and FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
