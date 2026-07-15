from workflow import phase_d_detail


def _channel():
    return {
        "channel_id": "UC1",
        "channel_title": "Sarah Jones",
        "channel_url": "https://www.youtube.com/channel/UC1",
        "channel_description": "I'm Sarah. Camping gear reviews and tutorials.",
        "subscriber_count": 1000,
        "total_video_count": 10,
        "total_view_count": 10000,
        "channel_created_at": "2020-01-01 00:00:00",
        "country": "US",
        "uploads_playlist_id": "UU1",
    }


def _new_channel():
    return {
        "channel_id": "UC1",
        "channel_title": "Sarah Jones",
        "source_keyword": "camping gear",
        "representative_video": {
            "video_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "title": "Best Camping Chair",
            "view_count": 50000,
            "engagement_rate": 4.2,
        },
    }


def test_phase_d_populates_all_enrichment_fields(monkeypatch):
    monkeypatch.setattr(phase_d_detail, "fetch_channel_details", lambda **kwargs: ([_channel()], ""))
    monkeypatch.setattr(phase_d_detail, "get_channel_uploads", lambda **kwargs: (["recent00001"], ""))
    monkeypatch.setattr(
        phase_d_detail,
        "fetch_video_details",
        lambda **kwargs: ([{
            "video_id": "recent00001",
            "channel_id": "UC1",
            "title": "Camping Chair Review Tutorial",
            "tags": "camping,review,tutorial",
            "published_at": "2026-07-01 00:00:00",
            "published_at_raw": "2026-07-01T00:00:00Z",
            "live_broadcast_content": "none",
        }], [], ""),
    )

    details, videos, error = phase_d_detail.run_phase_d("key", [_new_channel()])

    assert error == ""
    assert len(details) == 1
    assert details[0]["kol_name"] == "Sarah"
    assert details[0]["rep_video_title"] == "Best Camping Chair"
    assert details[0]["activity_status"] in {"持续更新", "有断更风险"}
    assert details[0]["latest_published_at"] == "2026-07-01 00:00:00"
    assert details[0]["channel_initial_assessment"].startswith("领域=户外/露营")
    assert videos[0]["channel_title"] == "Sarah Jones"


def test_phase_d_marks_activity_pending_on_recent_video_error(monkeypatch):
    monkeypatch.setattr(phase_d_detail, "fetch_channel_details", lambda **kwargs: ([_channel()], ""))
    monkeypatch.setattr(phase_d_detail, "get_channel_uploads", lambda **kwargs: ([], "temporary failure"))

    details, _, error = phase_d_detail.run_phase_d("key", [_new_channel()])

    assert error == ""
    assert details[0]["activity_status"] == "待确认"
    assert details[0]["latest_published_at"] == ""

