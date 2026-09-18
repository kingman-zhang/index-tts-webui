#!/usr/bin/env python3
"""build_registry 引擎优先级（TTS_ENGINE_PREFERRED）单测。

纯构造级测试（不发网络请求）；env 通过 monkeypatch 设置。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mono_runner import build_registry  # noqa: E402

PASS = 0
FAIL = 0


def check(cond: bool, desc: str) -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✅' if cond else '❌'} {desc}")


def order() -> list[str]:
    return [e.name for e in build_registry().engines]


def main() -> None:
    print("=== 默认顺序（无任何 Key/Token） ===")
    os.environ.pop("TTS_ENGINE_PREFERRED", None)
    os.environ.pop("INDEXTTS302_API_KEY", None)
    os.environ.pop("SILICONFLOW_API_KEY", None)
    check(order() == ["indextts_local", "indextts_art"], "默认：自建 → autodl.art（无 Key 引擎不注册）")

    print("=== TTS_ENGINE_PREFERRED=indextts_art ===")
    os.environ["TTS_ENGINE_PREFERRED"] = "indextts_art"
    check(order() == ["indextts_art", "indextts_local"], "art 排最前，自建落到其后")

    print("=== TTS_ENGINE_PREFERRED 含未注册名 ===")
    os.environ["TTS_ENGINE_PREFERRED"] = "indextts_art,indextts_302ai,bogus_engine"
    check(order() == ["indextts_art", "indextts_local"], "未注册（缺 Key）与不存在的名字被忽略，不破坏顺序")

    print("=== 完整序列重排（带全部 Key） ===")
    os.environ["INDEXTTS302_API_KEY"] = "fake-key"
    os.environ["SILICONFLOW_API_KEY"] = "fake-key"
    os.environ["TTS_ENGINE_PREFERRED"] = "indextts_art,indextts_local"
    check(
        order()
        == ["indextts_art", "indextts_local", "indextts_302ai", "indextts_siliconflow"],
        "列出者按给定顺序在最前，未列出者保持原相对顺序",
    )

    os.environ.pop("TTS_ENGINE_PREFERRED", None)
    os.environ.pop("INDEXTTS302_API_KEY", None)
    os.environ.pop("SILICONFLOW_API_KEY", None)
    print(f"\n===== {PASS}/{PASS + FAIL} passed =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
