from workflow import refresh_influencers as refresh_module


class FakeClient:
    def __init__(self, records):
        self.records = records
        self.updates = []

    def get_all_records(self, table_id):
        return self.records

    def batch_update_records(self, table_id, updates):
        self.updates.extend(updates)
        return len(updates)


def _record(record_id, channel_id, kol_name):
    return {
        "record_id": record_id,
        "fields": {
            "Channel ID": channel_id,
            "Channel Name": "Sarah Jones",
            "频道描述": "I'm Sarah and I review camping gear.",
            "联系邮箱": "sarah.jones@example.com",
            "KOL Name": kol_name,
            "代表视频URL": "https://www.youtube.com/watch?v=abcdefghijk",
            "来源关键词": "camping gear",
        },
    }


def test_refresh_updates_only_enrichment_and_preserves_confirmed_name(monkeypatch):
    client = FakeClient([_record("rec1", "UC1", "S. Jones")])
    monkeypatch.setattr(
        refresh_module,
        "fetch_channel_details",
        lambda *args, **kwargs: ([{
            "channel_id": "UC1",
            "channel_title": "Sarah Jones",
            "channel_description": "I'm Sarah and I review camping gear.",
            "uploads_playlist_id": "UU1",
        }], ""),
    )
    monkeypatch.setattr(refresh_module, "get_channel_uploads", lambda **kwargs: (["recent00001"], ""))
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([{
            "video_id": ids[0],
            "title": "Camping Gear Review",
            "tags": "camping,review",
            "published_at_raw": "2026-07-01T00:00:00Z",
            "live_broadcast_content": "none",
        }], [], ""),
    )
    monkeypatch.setattr(
        refresh_module,
        "evaluate_channel_activity",
        lambda videos, fetch_error="": {
            "latest_published_at": "2026-07-01 00:00:00",
            "activity_status": "持续更新",
            "error": "",
        },
    )

    result = refresh_module.refresh_influencers(
        api_key="key",
        client=client,
        table_id="table",
    )

    assert result["actual_updates"] == 1
    fields = client.updates[0]["fields"]
    assert "KOL Name" not in fields
    assert fields["断更评估"] == "持续更新"
    assert fields["代表视频标题"] == "Camping Gear Review"
    assert "开发状态" not in fields


def test_refresh_can_upgrade_manual_confirmation(monkeypatch):
    client = FakeClient([_record("rec1", "UC1", "手动确认")])
    monkeypatch.setattr(
        refresh_module,
        "fetch_channel_details",
        lambda *args, **kwargs: ([{
            "channel_id": "UC1",
            "channel_title": "Sarah Jones",
            "channel_description": "I'm Sarah.",
        }], ""),
    )

    result = refresh_module.refresh_influencers(
        api_key="key",
        client=client,
        table_id="table",
        fields="name",
    )

    assert result["actual_updates"] == 1
    assert client.updates[0]["fields"]["KOL Name"] == "Sarah"


def test_explicit_migration_flag_can_replace_program_generated_name(monkeypatch):
    client = FakeClient([_record("rec1", "UC1", "Bad Automatic Guess")])
    monkeypatch.setattr(
        refresh_module,
        "fetch_channel_details",
        lambda *args, **kwargs: ([{
            "channel_id": "UC1",
            "channel_title": "Sarah Jones",
            "channel_description": "I'm Sarah.",
        }], ""),
    )

    refresh_module.refresh_influencers(
        api_key="key",
        client=client,
        table_id="table",
        fields="name",
        replace_kol_names=True,
    )

    assert client.updates[0]["fields"]["KOL Name"] == "Sarah"


def test_video_id_parser_supports_common_youtube_urls():
    assert refresh_module._video_id_from_url("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk"
    assert refresh_module._video_id_from_url("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert refresh_module._video_id_from_url("https://www.youtube.com/shorts/abcdefghijk") == "abcdefghijk"


def test_quota_estimate_uses_batched_channel_and_representative_calls():
    records = [_record(f"rec{i}", f"UC{i}", "") for i in range(51)]

    # 2 channels.list batches + 102 per-channel recent-video units +
    # 1 batched representative-title videos.list call (same URL in fixtures).
    assert refresh_module._estimate_quota(records, refresh_module.ALL_REFRESH_FIELDS) == 105
