"""JSON 文件存储原语：读 + 原子写（账本与会话两个 json 后端共用）。

为什么单独一层
--------------
本模块只做「把 JSON 文件读出来 / 原子地写回去」，**不含任何业务语义、不带锁**。
`store_file.py`（账本）与 `session_store_file.py`（会话）都用它 —— 否则
「文件损坏时先备份再返回默认值」这类细节会被抄成两份、各自演化。
锁仍由各后端自己持有（它们的临界区范围不同，不能共用一把）。

⚠️ 只给**同目录的 json 后端**用；业务层一律走 `store.py` / `session_store.py` 门面。
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
