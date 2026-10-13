"""JSON 文件存储原语：读 + 原子写（账本 / 会话 / 内容三个 json 后端共用）。

为什么单独一层
--------------
本模块只做「把 JSON 文件读出来 / 原子地写回去」，**不含任何业务语义、不带锁**。
`membership/store_file.py`（账本）、`membership/session_store_file.py`（会话）与
`stores_file.py`（项目/术语表/收藏）都用它 —— 否则「文件损坏时先备份再返回默认值」
这类细节会被抄成三份、各自演化。
锁仍由各后端自己持有（它们的临界区范围不同，不能共用一把）。

位置：放 `app/` 顶层而不是 `app/membership/` 内 —— 它是**跨域通用原语**，
被内容层（`stores_file.py`）复用时不该顺带把整个 membership 包初始化一遍。

⚠️ 只给**各 `*_file.py` 后端**用；业务层一律走对应门面（`store.py` /
`session_store.py` / `stores.py`）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    """读 JSON；文件不存在或内容损坏时返回 `default`。

    损坏时把原文件改名成 `*.corrupt` 留证（`OSError` 忽略）—— 直接覆盖会让
    「谁把文件写坏了」永远查不出来。
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 损坏时备份原文件并返回默认值，避免整站不可用
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        return default


def write_json(path: Path, data: Any) -> None:
    """原子写：先写 `*.tmp` 再 `os.replace`（同目录 rename 是原子的）。

    直接 `open(path, "w")` 在写入过程中崩溃会留下**半截文件**，
    下次读就是损坏 → 被 `read_json` 备份掉，等于丢数据。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
