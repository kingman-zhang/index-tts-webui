#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""术语表两层结构（全局库 + 用户库）自测。

在**临时数据目录**中运行，绝不触碰真实 data/。
    cd webui-backend && python tests/test_glossary_layers.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# 必须在 import app.* 之前指定独立数据目录（app.config 在 import 时读取）
_TMP = tempfile.mkdtemp(prefix="glossary_test_")
os.environ["DATA_DIR"] = _TMP
os.environ.pop("MEMBER_ADMIN_TOKEN", None)

from app import stores  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def pairs(terms: list) -> list:
    return [(t["original"], t["replacement"]) for t in terms]


def main() -> None:
    print(f"临时数据目录: {_TMP}\n")

    print("── 1. 未登录：只有全局库 ──")
    stores.save_global_glossary([
        {"original": "说服", "replacement": "说福"},
        {"original": "游说", "replacement": "游睡"},
    ])
    m = stores.load_glossary(None)
    check("未登录返回全局库 2 条", len(m) == 2, str(pairs(m)))

    print("\n── 2. 用户同名覆盖 + 独有条目追加 ──")
    stores.save_user_glossary("u_1", [
        {"original": "说服", "replacement": "说福气"},   # 覆盖内置
        {"original": "锚点", "replacement": "毛点"},     # 用户独有
    ])
    m = stores.load_glossary("u_1")
    check("用户覆盖生效且位置不变（全局骨架顺序）",
          pairs(m) == [("说服", "说福气"), ("游说", "游睡"), ("锚点", "毛点")],
          str(pairs(m)))
    check("用户独有项追加在末尾", m[-1]["original"] == "锚点")

    print("\n── 3. 用户停用内置词（replacement 为空）──")
    stores.save_user_glossary("u_2", [{"original": "游说", "replacement": ""}])
    m = stores.load_glossary("u_2")
    check("被停用的内置词从生效词表剔除",
          [t["original"] for t in m] == ["说服"], str(pairs(m)))

    print("\n── 4. 用户库中 replacement 为空且全局没有该词 → 忽略 ──")
    stores.save_user_glossary("u_3", [{"original": "不存在的词", "replacement": ""}])
    m = stores.load_glossary("u_3")
    check("空替换的未知词不进词表", len(m) == 2, str(pairs(m)))

    print("\n── 5. source 标记 ──")
    v = stores.load_glossary("u_1", with_source=True)
    check("覆盖项 source=user", v[0]["source"] == "user", str(v[0]))
    check("未覆盖项 source=global", v[1]["source"] == "global", str(v[1]))
    check("用户独有项 source=user", v[2]["source"] == "user", str(v[2]))

    print("\n── 6. 全局库空 replacement 沿用旧语义（保留，替换为空串）──")
    stores.save_global_glossary([{"original": "占位词", "replacement": ""}])
    m = stores.load_glossary(None)
    check("全局库空替换保留", pairs(m) == [("占位词", "")], str(pairs(m)))

    print("\n── 7. 去重与脏数据容错 ──")
    stores.save_global_glossary([
        {"original": "A", "replacement": "1"},
        {"original": "A", "replacement": "2"},
        {"original": "", "replacement": "空原词"},
        {"replacement": "无原词"},
    ])
    m = stores.load_glossary(None)
    check("重复 original 只保留一条", len(m) == 1, str(pairs(m)))

    print("\n── 8. 用户库文件路径安全 ──")
    p = stores.glossary_user_path("../../etc/passwd")
    check("路径穿越被过滤", ".." not in str(p) and p.parent == stores.GLOSSARY_USERS_DIR,
          str(p))
    p2 = stores.glossary_user_path("u_1")
    check("正常 user_id 路径正确", p2.name == "u_1.json", str(p2))
    try:
        stores.glossary_user_path("")
        check("空 user_id 抛错", False)
    except ValueError:
        check("空 user_id 抛错", True)

    print("\n── 9. 未登录用户写入无效（不创建文件）──")
    stores.save_user_glossary("u_1", [{"original": "x", "replacement": "y"}])
    check("用户库文件已创建", stores.glossary_user_path("u_1").exists())
    check("其他用户互不干扰", stores.load_user_glossary("u_9") == [])

    print("\n── 10. 合成用副本：替换不回写原文 ──")
    stores.save_global_glossary([
        {"original": "游说", "replacement": "游睡"},
        {"original": "说服", "replacement": "说福"},
    ])
    task_lines = [
        {"speaker": "A", "text": "我们要游说对方，也要说服他", "emotion": {"label": "calm"}},
        {"speaker": "A", "text": "这句没有术语", "silence_after_ms": 300},
    ]
    snapshot = json.dumps(task_lines, ensure_ascii=False, sort_keys=True)
    terms = stores.load_glossary(None)
    synth = stores.apply_glossary(task_lines, terms)
    check("合成文本已替换",
          synth[0]["text"] == "我们要游睡对方，也要说福他", synth[0]["text"])
    check("原文未被改写",
          json.dumps(task_lines, ensure_ascii=False, sort_keys=True) == snapshot,
          task_lines[0]["text"])
    check("返回新列表且元素为新 dict",
          synth is not task_lines and synth[0] is not task_lines[0])
    check("非文本字段沿用（emotion / silence_after_ms）",
          synth[0]["emotion"] == {"label": "calm"} and synth[1]["silence_after_ms"] == 300)
    check("空词表原样返回（不复制）", stores.apply_glossary(task_lines, []) is task_lines)

    print("\n── 11. 端到端：_execute_task 不回写 task['lines'] ──")
    import asyncio

    from app import mono_runner, queue_state as qs
    from app import queue_worker as qw

    tid = "q_glossary_smoke"
    lines = [{"speaker": "A", "text": "我们要游说对方", "emotion": None}]
    task = {
        "id": tid, "kind": "mono", "lines": lines,
        "voices": {"A": "/tmp/nonexistent-ref.wav"}, "silence": {}, "params": {},
        "glossary_enabled": True, "status": qs.QueueTaskStatus.RUNNING,
        "member_id": "u_smoke",
    }
    qs.queue_tasks[tid] = task
    captured: dict = {}

    async def _fake_run_mono(_task, lines=None):
        captured["lines"] = lines

    async def _drive():
        qs.queue_order.clear()
        await qw._execute_task(tid)
        await asyncio.sleep(0)  # 让 finally 里排出的 process_queue 跑完，避免遗留 pending 任务

    orig = mono_runner.run_mono_task
    mono_runner.run_mono_task = _fake_run_mono
    try:
        asyncio.run(_drive())
    finally:
        mono_runner.run_mono_task = orig
    check("传给合成层的是替换后文本",
          captured.get("lines", [{}])[0].get("text") == "我们要游睡对方", str(captured))
    check("task['lines'] 保持用户原文",
          task["lines"][0]["text"] == "我们要游说对方", task["lines"][0]["text"])
    check("落盘的任务 JSON 也是原文",
          json.loads(qs.queue_file(tid).read_text(encoding="utf-8"))["lines"][0]["text"]
          == "我们要游说对方")
    qs.queue_tasks.pop(tid, None)

    print("\n" + "=" * 60)
    print(f"结果：通过 {PASS} / 失败 {FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
