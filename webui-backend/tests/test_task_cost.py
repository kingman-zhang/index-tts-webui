#!/usr/bin/env python3
"""积分计费口径单测（纯函数，不启服务、不连 TTS）。

锁定 `service.estimate_task_cost()` 的口径：
    cost = max(MIN_CHARGE, ceil(chars * POINTS_PER_1000_CHARS / 1000))
其中 chars = 剥离行内 [pause:N] 后的字符数。

为什么值得单独锁：旧口径是 `ceil(chars/1000) * 单价`（整千向上取整），
不足 1000 字也按一整千收费、1000→1001 字翻倍。口径改动没有测试兜底时
很容易在重构里被改回去，而它直接决定用户看到的价格。

与前端的一致性由 `webui-frontend/src/types/index.ts` 的 estimatePoints() /
billableChars() 保证——那边是同一公式的 JS 版，改这里必须同步改那边。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="wb-cost-test-")
os.environ["MEMBER_POINTS_PER_1000_CHARS"] = "50"
os.environ["MEMBER_MIN_CHARGE"] = "5"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.membership import service  # noqa: E402

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


def text(n: int) -> list:
    """构造恰好 n 字的一行（用可读汉字填充，避免被当成标记）。"""
    body = ("字" * n)[:n]
    return [{"text": body, "emotion_label": None}]


def cost(n: int) -> int:
    return service.estimate_task_cost(text(n))


def main():
    global PASS, FAIL

    print("── 计费口径基本式（单价 50 / 千字，最低 5）──")
    check("空任务不计费", service.estimate_task_cost([]) == 0)
    check("空行不计费", service.estimate_task_cost([{"text": "   "}]) == 0)
    check("None 不计费", service.estimate_task_cost(None) == 0)

    print("\n── 最低收费地板 ──")
    check("1 字 → 5（地板）", cost(1) == 5, f"got {cost(1)}")
    check("20 字 → 5（地板）", cost(20) == 5, f"got {cost(20)}")
    check("50 字 → 5（地板）", cost(50) == 5, f"got {cost(50)}")
    check("99 字 → 5（线性值 4.95 上取整仍 < 地板）", cost(99) == 5, f"got {cost(99)}")
    check("100 字 → 5（恰在地板线上）", cost(100) == 5, f"got {cost(100)}")
    check("101 字 → 6（越过地板，按线性）", cost(101) == 6, f"got {cost(101)}")

    print("\n── 线性段（与旧口径在大字数额完全一致）──")
    check("300 字 → 15", cost(300) == 15, f"got {cost(300)}")
    check("500 字 → 25", cost(500) == 25, f"got {cost(500)}")
    check("999 字 → 50", cost(999) == 50, f"got {cost(999)}")
    check("1000 字 → 50（与旧口径同价）", cost(1000) == 50, f"got {cost(1000)}")
    check("2000 字 → 100（与旧口径同价）", cost(2000) == 100, f"got {cost(2000)}")
    check("5000 字 → 250（与旧口径同价）", cost(5000) == 250, f"got {cost(5000)}")
    check("10000 字 → 500（与旧口径同价）", cost(10000) == 500, f"got {cost(10000)}")

    print("\n── 旧口径的两个畸形点已消除 ──")
    old_1001 = -(-1001 // 1000) * service.POINTS_PER_1000_CHARS
    check("1001 字不再翻倍（旧 100 → 新 51）", cost(1001) == 51, f"got {cost(1001)}")
    check("1001 字旧口径确实是 100（对照）", old_1001 == 100, f"got {old_1001}")
    check(
        "短文本不再按整千收费（500 字旧 50 → 新 25）",
        cost(500) == 25,
        f"got {cost(500)}",
    )
    check(
        "1000 字以上与旧口径逐档一致",
        all(
            cost(n) == -(-n // 1000) * service.POINTS_PER_1000_CHARS
            for n in (1000, 1001 + 998, 2000, 3000, 5000, 10000)
        ),
    )

    print("\n── [pause:N] 不进计费字数 ──")
    with_pause = [{"text": "你好[pause:0.5]世界[pause: 1]"}]
    check(
        "停顿标记被剥离（4 字正文）",
        service.count_billable_chars(with_pause) == 4,
        f"got {service.count_billable_chars(with_pause)}",
    )
    check(
        "同句带标记与不带标记同价",
        service.estimate_task_cost(with_pause)
        == service.estimate_task_cost([{"text": "你好世界"}]),
    )
    long_pause = [{"text": "字" * 100 + "[pause:9.9]"}]
    check(
        "停顿秒数写长不涨价",
        service.estimate_task_cost(long_pause) == cost(100),
        f"got {service.estimate_task_cost(long_pause)}",
    )

    print("\n── 行形态兼容（dict / str）──")
    check(
        "dict 行与 str 行同价",
        service.estimate_task_cost([{"text": "字" * 300}])
        == service.estimate_task_cost(["字" * 300]),
    )
    check(
        "多个 dict 行累加",
        service.estimate_task_cost([{"text": "字" * 400}, {"text": "字" * 400}]) == 40,
        f"got {service.estimate_task_cost([{'text': '字' * 400}, {'text': '字' * 400}])}",
    )
    check(
        "忽略无 text 的脏行",
        service.estimate_task_cost([{"nope": 1}, {"text": "字" * 100}]) == 5,
    )
    check("行首尾空白不计费", service.estimate_task_cost([{"text": "  " + "字" * 100 + "  "}]) == 5)

    print("\n── 单价 / 地板可配 ──")
    orig_p, orig_m = service.POINTS_PER_1000_CHARS, service.MIN_CHARGE
    try:
        service.POINTS_PER_1000_CHARS = 0
        check("单价 0 = 关闭按量扣费（恒 0）", service.estimate_task_cost(text(5000)) == 0)

        service.POINTS_PER_1000_CHARS = 10
        service.MIN_CHARGE = 1
        check("单价 10 / 地板 1：1000 字 → 10", cost(1000) == 10, f"got {cost(1000)}")
        check("单价 10 / 地板 1：5 字 → 1", cost(5) == 1, f"got {cost(5)}")

        service.POINTS_PER_1000_CHARS = 50
        service.MIN_CHARGE = 0
        check("地板 0 = 不设地板：10 字 → 1", cost(10) == 1, f"got {cost(10)}")
        check("地板 0 时空任务仍为 0", service.estimate_task_cost([]) == 0)
    finally:
        service.POINTS_PER_1000_CHARS, service.MIN_CHARGE = orig_p, orig_m

    print("\n── 与 .env 实际配置自洽 ──")
    check(
        "默认单价为 50（.env 生效时）",
        orig_p == 50,
        f"got {orig_p}（若此处失败，检查 .env 的 MEMBER_POINTS_PER_1000_CHARS）",
    )
    check("默认地板为 5", orig_m == 5, f"got {orig_m}")
    check(
        "签到 1 天（%d 积分）够合成 100 字" % service.CHECKIN_BONUS,
        cost(100) <= service.CHECKIN_BONUS,
        f"签到 {service.CHECKIN_BONUS} 积分，100 字需 {cost(100)} 积分",
    )

    print(f"\n{'=' * 56}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 56}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
