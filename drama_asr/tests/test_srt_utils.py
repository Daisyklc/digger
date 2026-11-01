from drama_asr.server.srt_utils import Segment, build_srt, format_timestamp


def test_format_timestamp_basic():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(1.5) == "00:00:01,500"
    assert format_timestamp(61.25) == "00:01:01,250"
    assert format_timestamp(3661.001) == "01:01:01,001"


def test_format_timestamp_clamps_negative():
    assert format_timestamp(-5) == "00:00:00,000"


def _segs():
    return [
        Segment(start=0.0, end=2.5, ja="こんにちは", zh="你好"),
        Segment(start=3.0, end=5.0, ja="またね", zh="再见"),
    ]


def test_build_srt_bilingual():
    out = build_srt(_segs(), "bilingual")
    assert "1\n00:00:00,000 --> 00:00:02,500\nこんにちは\n你好\n" in out
    assert "2\n00:00:03,000 --> 00:00:05,000\nまたね\n再见\n" in out


def test_build_srt_ja_only():
    out = build_srt(_segs(), "ja")
    assert "你好" not in out
    assert "こんにちは" in out


def test_build_srt_zh_only():
    out = build_srt(_segs(), "zh")
    assert "こんにちは" not in out
    assert "你好" in out


def test_build_srt_skips_empty_text():
    segs = [Segment(start=0.0, end=1.0, ja="  ", zh=None)]
    assert build_srt(segs, "ja") == ""


def test_build_srt_unknown_mode():
    import pytest

    with pytest.raises(ValueError):
        build_srt(_segs(), "nope")
