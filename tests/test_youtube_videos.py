from youtube.videos import _parse_video_item


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
