"""积分套餐规格（积分商城的商品清单）。

## 定价锚点

与计费口径一致（见 `service.estimate_task_cost`）：

    1 积分 = ¥0.01 ；合成单价 50 积分/千字 ⇒ ¥5/万字
    ⇒ 1000 积分 约可合成 2 万字（1 元 ≈ 2000 字）

## 字段约定（**入账只认 `points`，别的地方都不许再算一遍**）

- `price_fen`  售价，单位**分**（¥10 = 1000）。用整数分，避免浮点。
- `points`     到账积分，**已含赠送量**；入账只用它。
- `bonus_points` 其中赠送的部分，**仅供前端展示「多送 N 积分」**，不参与计算
  —— 两处口径各算一次，迟早会漂移（改了 points 忘改 bonus 就会出现
  页面写「多送 750」实际到账却不是）。
- `order`      展示顺序。

## 改价格

只改这一张表。**不要**把「约可合成多少字」写死在这里：那取决于
`MEMBER_POINTS_PER_1000_CHARS`，由 service 按当前配置现算返回
（`est_chars`），否则调价/改单价后商城文案就会过期。
"""

from __future__ import annotations

from typing import Optional

# 方案 B「阶梯赠送」：小额无赠送、大额递增（2026-10-05 定稿）。
PACKS: list[dict] = [
    {
        "id": "p1000",          # ¥10
        "name": "体验包",
        "price_fen": 1000,
        "points": 1000,
        "bonus_points": 0,
        "order": 1,
    },
    {
        "id": "p3200",          # ¥30（多送 6.7%）
        "name": "标准包",
        "price_fen": 3000,
        "points": 3200,
        "bonus_points": 200,
        "order": 2,
    },
    {
        "id": "p5750",          # ¥50（多送 15%）
        "name": "超值包",
        "price_fen": 5000,
        "points": 5750,
        "bonus_points": 750,
        "order": 3,
    },
    {
        "id": "p12500",         # ¥100（多送 25%）
        "name": "尊享包",
        "price_fen": 10000,
        "points": 12500,
        "bonus_points": 2500,
        "order": 4,
    },
]

PACK_BY_ID: dict[str, dict] = {p["id"]: p for p in PACKS}


def get_pack(pack_id: str) -> Optional[dict]:
    """按 id 取套餐；不存在返回 None（由调用方决定报错文案）。"""
    return PACK_BY_ID.get((pack_id or "").strip())


def sorted_packs() -> list[dict]:
    return sorted(PACKS, key=lambda p: p["order"])
