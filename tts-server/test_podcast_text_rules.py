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


def test_foreign_name_middle_dot_does_not_create_hyphen_pause():
    from pathlib import Path

    front_source = Path(__file__).parents[2] / "index-tts-main/indextts/utils/front.py"
    source = front_source.read_text(encoding="utf-8")
    assert '"·": "",' in source
    assert '"·": "-",' not in source


def test_podcast_sanitizer_keeps_chinese_middle_dot_for_downstream_name_handling():
    assert _sanitize_text("约翰·阿彻") == "约翰·阿彻"
