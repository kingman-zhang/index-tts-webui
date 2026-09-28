import pytest

from podcast_engine import _normalize_reading_text, _sanitize_text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("在1550～1850年间", "在一五五零～一八五零年间"),
        ("1550-1850年", "一五五零-一八五零年"),
        ("1550年至1850年", "一五五零年至一八五零年"),
        ("1550年到1850年", "一五五零年到一八五零年"),
        ("1550—1850年间", "一五五零—一八五零年间"),
        ("公元1550年，至公元1850年", "公元一五五零年，至公元一八五零年"),
    ],
)
def test_year_ranges_are_read_digit_by_digit(text, expected):
    assert _normalize_reading_text(text) == expected


def test_year_range_keeps_existing_year_rule():
    assert _normalize_reading_text("从1550年开始，到1850年结束") == "从一五五零年开始，到一八五零年结束"


def test_foreign_name_separators_are_normalized_upstream():
    """人名分隔号在 backend 合成前统一归一化（webui-backend/app/name_punct.py）。

    原先断言的是「给上游 front.py 打补丁把 "·" 换成空串」，但 index-tts-main
    不受本仓库版本管理（同步上游会丢补丁），且改 front.py 只覆盖本地引擎、
    管不到 autodl.art / 302.ai 云端。现在落到引擎无关的 backend 侧：
    `・`U+30FB 等变体原本会被分词成 <unk> → 模型吐怪音。
    """
    from pathlib import Path

    root = Path(__file__).parents[1]
    source = (root / "webui-backend/app/name_punct.py").read_text(encoding="utf-8")
    for codepoint in ("\\u30fb", "\\u00b7", "\\u2022", "\\u2027", "\\uff65"):
        assert codepoint in source, f"name_punct 未覆盖分隔符 {codepoint}"

    queue_worker = (root / "webui-backend/app/queue_worker.py").read_text(encoding="utf-8")
    assert "name_punct.apply_name_separator_rules" in queue_worker, (
        "归一化模块存在但没接进合成前处理（queue_worker）"
    )


def test_podcast_sanitizer_keeps_chinese_middle_dot_for_downstream_name_handling():
    assert _sanitize_text("约翰·阿彻") == "约翰·阿彻"
