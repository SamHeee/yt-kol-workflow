import pytest

from youtube.videos import _parse_video_item, extract_video_id


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abcdefghijk", "abcdefghijk"),
        ("https://www.youtube.com/watch?v=abcdefghijk", "abcdefghijk"),
        ("https://m.youtube.com/watch?v=abcdefghijk&t=3", "abcdefghijk"),
        ("https://youtu.be/abcdefghijk?si=tracking", "abcdefghijk"),
        ("https://www.youtube.com/shorts/abcdefghijk", "abcdefghijk"),
        ("https://www.youtube.com/live/abcdefghijk?feature=share", "abcdefghijk"),
        ("https://www.youtube.com/embed/abcdefghijk", "abcdefghijk"),
        ("https://www.youtube-nocookie.com/embed/abcdefghijk", "abcdefghijk"),
    ],
)
def test_extract_video_id_supports_strict_common_forms(value, expected):
    assert extract_video_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "too-short",
        "https://example.com/watch?v=abcdefghijk",
        "https://notyoutube.com/watch?v=abcdefghijk",
        "https://youtube.com.evil.test/watch?v=abcdefghijk",
        "https://www.youtube.com/playlist?list=abcdefghijk",
        "https://www.youtube.com/watch?v=short",
        "javascript:https://youtu.be/abcdefghijk",
        "https://www.youtube.com/shorts/abcdefghijk/extra",
        "https://youtu.be/abcdefghijk/extra",
    ],
)
def test_extract_video_id_rejects_invalid_values_and_lookalike_hosts(value):
    assert extract_video_id(value) == ""


def test_parse_video_item_keeps_raw_timestamp_and_broadcast_state():
    item = {
        "id": "abc123",
        "snippet": {
            "publishedAt": "2026-07-10T12:34:56Z",
            "liveBroadcastContent": "upcoming",
            "title": "Scheduled stream",
        },
        "statistics": {},
        "contentDetails": {},
    }

    parsed = _parse_video_item(item)

    assert parsed["published_at_raw"] == "2026-07-10T12:34:56Z"
    assert parsed["published_at"] == "2026-07-10 12:34:56"
    assert parsed["live_broadcast_content"] == "upcoming"


def test_parse_video_item_defaults_broadcast_state_to_none():
    parsed = _parse_video_item({"id": "abc123", "snippet": {}})

    assert parsed["published_at_raw"] == ""
    assert parsed["published_at"] == ""
    assert parsed["live_broadcast_content"] == "none"
    assert parsed["view_count_available"] is False
    assert parsed["like_count_available"] is False
    assert parsed["comment_count_available"] is False


def test_parse_video_item_marks_individually_available_statistics():
    parsed = _parse_video_item({
        "id": "abcdefghijk",
        "snippet": {},
        "statistics": {"viewCount": "123", "commentCount": "4"},
    })

    assert parsed["view_count"] == 123
    assert parsed["view_count_available"] is True
    assert parsed["like_count_available"] is False
    assert parsed["comment_count_available"] is True
