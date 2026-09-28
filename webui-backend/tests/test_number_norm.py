#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数字读法归一化（app/number_norm.py）自测 + 与 IndexTTS 文本前端 TN 的对照。

用法（在 webui-backend 下）：
    python tests/test_number_norm.py

装了 wetext（mac 上的 WeTextProcessing 替身）时会额外打印 TN 原始输出对照；
没装则跳过对照，只跑断言。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须在 import app.* 之前：独立数据目录 + 开启本模块
_TMP = tempfile.mkdtemp(prefix="number_norm_test_")
os.environ["DATA_DIR"] = _TMP
os.environ["NUM_NORMALIZE"] = "1"

from app.number_norm import ENABLED, normalize_numbers, read_digits  # noqa: E402

PASS = FAIL = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    ok = actual == expected
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         expect: {expected!r}\n         actual: {actual!r}")


# (原文, 期望输出, 说明)
CASES: list[tuple[str, str, str]] = [
    # —— 号码语境：应逐位读 ——
    ("快递单号1234567890", "快递单号幺二三四五六七八九零", "长号码"),
    ("订单号是1234567890123", "订单号是幺二三四五六七八九零幺二三", "13 位订单号"),
    ("身份证110101199001011234", "身份证幺幺零幺零幺幺九九零零幺零幺幺二三四", "18 位身份证"),
    ("账号6222021234567890123", "账号六二二二零二幺二三四五六七八九零幺二三", "19 位卡号"),
    ("他的手机是13812345678", "他的手机是幺三八幺二三四五六七八", "11 位手机号（形态命中）"),
    ("13812345678", "幺三八幺二三四五六七八", "裸手机号（形态命中）"),
    ("座机号码是01088886666", "座机号码是零幺零八八八八六六六六", "带区号座机"),
    ("400-123-4567", "四零零幺二三四五六七", "分隔符号码"),
    ("135-4567-8900", "幺三五四五六七八九零零", "分隔符手机号"),
    ("分机8021", "分机八零二幺", "4 位分机号"),
    ("客服热线10086", "客服热线幺零零八六", "与 TN 结果一致"),
    ("021-88886666", "零二幺八八八八六六六六", "两段但总位数 ≥10 → 号码"),
    ("123-456-789", "幺二三四五六七八九", "三段 → 号码"),
    # —— 数值语境：一律不动，交回 TN ——
    ("这件商品110元", "这件商品110元", "后跟单位 → 否决"),
    ("110元", "110元", "后跟单位 → 否决"),
    ("110万", "110万", "后跟单位 → 否决"),
    ("考了110分", "考了110分", "后跟单位 → 否决"),
    ("第110位", "第110位", "前有「第」→ 否决"),
    ("110个", "110个", "后跟量词 → 否决"),
    ("110公里", "110公里", "后跟单位 → 否决"),
    ("我在110号房间", "我在110号房间", "后跟「号」→ 否决"),
    ("订单金额110", "订单金额110", "「金额」否决优先于「订单」命中"),
    ("股价110", "股价110", "前有「股价」→ 否决"),
    ("发布于2026", "发布于2026", "4 位无语境 → 不猜"),
    ("2026年", "2026年", "后跟「年」→ 否决"),
    ("2020-2024", "2020-2024", "年份区间，非号码"),
    ("2026-09-28发布", "2026-09-28发布", "日期形态 → 否决"),
    ("1999-2001年", "1999-2001年", "年份区间 + 单位 → 否决"),
    # —— 交给 TN（本模块刻意不碰）——
    ("拨打110报警", "拨打110报警", "3 位短号，TN 已读对"),
    ("12306", "12306", "5 位无语境、非手机形态 → 不猜"),
    ("110", "110", "裸短号，TN 默认号码读法"),
    # —— 不跨句借语境 ——
    ("1234567890是我的编号", "幺二三四五六七八九零是我的编号", "后置语境词命中"),
    ("客服。1234567890", "客服。1234567890", "跨句号不借语境 → 自身不命中 → 不改"),
]


def main() -> None:
    print(f"NUM_NORMALIZE 生效状态: ENABLED={ENABLED}\n")

    print("── 1. 逐位读法 ──")
    check("read_digits('110')", read_digits("110"), "幺幺零")
    check("read_digits('2026')", read_digits("2026"), "二零二六")
    check("read_digits 丢弃分隔符", read_digits("400-123"), "四零零幺二三")

    print("\n── 2. 判定用例 ──")
    for src, expected, why in CASES:
        check(f"{src}  （{why}）", normalize_numbers(src), expected)

    print("\n── 3. 与 TN 的对照（wetext 替身，可选）──")
    try:
        import wetext

        tn = wetext.Normalizer(remove_erhua=False, lang="zh", operator="tn")
    except Exception as e:  # noqa: BLE001
        print(f"  跳过：{e}")
    else:
        print(f"  {'原文':26} {'TN 原本读作':34} 本模块输出")
        for src, _, _ in CASES:
            print(f"  {src:26} {tn.normalize(src):34} {normalize_numbers(src)}")

    print("\n── 4. 端到端：合成副本里生效、任务原文不动 ──")
    import asyncio

    from app import mono_runner, queue_state as qs
    from app import queue_worker as qw

    tid = "q_number_smoke"
    lines = [{"speaker": "A", "text": "快递单号1234567890，有问题拨打110报警", "emotion": None}]
    task = {
        "id": tid, "kind": "mono", "lines": lines,
        "voices": {"A": "/tmp/nonexistent-ref.wav"}, "silence": {}, "params": {},
        "glossary_enabled": True, "status": qs.QueueTaskStatus.RUNNING,
    }
    qs.queue_tasks[tid] = task
    captured: dict = {}

    async def _fake_run_mono(_task, lines=None):
        captured["lines"] = lines

    async def _drive():
        qs.queue_order.clear()
        await qw._execute_task(tid)
        await asyncio.sleep(0)

    orig = mono_runner.run_mono_task
    mono_runner.run_mono_task = _fake_run_mono
    try:
        asyncio.run(_drive())
    finally:
        mono_runner.run_mono_task = orig
    synth_text = (captured.get("lines") or [{}])[0].get("text", "")
    check("合成层拿到转换后的号码",
          synth_text, "快递单号幺二三四五六七八九零，有问题拨打110报警")
    check("3 位短号 110 不动（交回 TN）", "拨打110报警" in synth_text, True)
    check("task['lines'] 仍是原文", task["lines"][0]["text"], lines[0]["text"])
    qs.queue_tasks.pop(tid, None)

    print("\n" + "=" * 60)
    print(f"结果：通过 {PASS} / 失败 {FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
