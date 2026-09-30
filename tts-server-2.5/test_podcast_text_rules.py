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


# ── 时间读法：必须与 backend app/time_norm.py 给出同一结果（2026-09-30）──
#
# 为什么 tts-server 侧还要有这套规则：音色**试听**直连本服务（/api/synthesize），
# **不走** backend 的前处理链（试听是即时的，不建队列任务）。所以两边必须各有一套、
# 且必须等价 —— 一旦分叉，用户会听到「试听对、正式合成不对」（或反之）。
#
# 下面这张表与 webui-backend/tests/test_time_norm.py 的 CASES **逐条对应**，
# 改动必须同步改两处 + 两边测试。刻意**不 import backend**：tts-server 要能独立
# 部署在 GPU 机上（backend 代码未必在同一台机器）。
TIME_CASES = [
    # 该改的
    ("12:30", "十二点三十分"),
    ("会议定在12:30开始", "会议定在十二点三十分开始"),
    ("12 : 30", "十二点三十分"),          # TN 会读成「十二比三十」，本层救回来
    ("0:30", "零点三十分"),               # TN 会读成「零比三十」
    ("0:00", "零点整"),
    ("9:05", "九点零五分"),               # 分钟 <10 补零
    ("8:00", "八点整"),
    ("23:59", "二十三点五十九分"),
    ("15:45", "十五点四十五分"),          # 不是「一十五」
    ("20:20", "二十点二十分"),            # 不是「两十」
    ("3:16", "三点十六分"),               # 章节引用会被这样读，TN 也如此（已知取舍）
    # 不该动的
    ("16:9", "16:9"),                     # 比例（分钟一位）
    ("3:2", "3:2"),                       # 比分
    ("1:2", "1:2"),                       # 版本号
    ("12:30:45", "12:30:45"),             # 时:分:秒 整串不碰，交回 TN 读秒
    ("1:12:30", "1:12:30"),
    ("99:99", "99:99"),                   # 时/分越界
    ("24:00", "24:00"),                   # 「二十四点」合法，交回 TN
    ("123:45", "123:45"),                 # 更长的数字串
    ("12:345", "12:345"),
]


@pytest.mark.parametrize("text, expected", TIME_CASES)
def test_time_reading_matches_backend(text, expected):
    assert _normalize_reading_text(text) == expected


def test_backend_time_norm_module_exists_and_is_wired():
    """确认 backend 侧确有对应的独立模块且已接进前处理链。

    两处规则是**各自独立**的实现（tts-server 要能独立部署），所以「有没有人忘了
    同步」只能靠测试各自覆盖 + 这里确认 backend 侧模块还在。若哪天 backend 改名/
    删模块，本断言会提醒去核对两边。
    """
    from pathlib import Path

    root = Path(__file__).parents[1]
    src = (root / "webui-backend/app/time_norm.py").read_text(encoding="utf-8")
    assert "_TIME_PATTERN" in src and "MAX_PARTS" in src
    queue_worker = (root / "webui-backend/app/queue_worker.py").read_text(encoding="utf-8")
    assert "time_norm.apply_time_rules" in queue_worker, (
        "时间规则模块存在但没接进合成前处理（queue_worker）"
    )


# 说明：2.0 侧的 test_podcast_text_rules.py 另有一组「变速」用例（ATEMPO_CASES），
# 校验 `_atempo_chain` 与 backend 的 `atempo_filters` 等价。**2.5 这里刻意不复制** ——
# 2.5 的壳没有 `_apply_speed`/atempo 链，语速是折算成模型侧 `duration_factor` 表达的
# （见 podcast_engine.GenerationParams.to_infer_kwargs），本侧没有可对照的实现。
# 若哪天 2.5 也补上 ffmpeg 后处理，把 2.0 的那组用例一并搬过来。
