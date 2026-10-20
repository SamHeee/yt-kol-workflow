from copy import deepcopy

import pytest

from workflow import refresh_influencers as refresh_module


class FakeClient:
    def __init__(self, records):
        self.records = records
        self.updates = []

    def get_all_records(self, table_id):
        return deepcopy(self.records)

    def batch_get_records(self, table_id, record_ids):
        return deepcopy([record for record in self.records if record["record_id"] in record_ids])

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
    assert refresh_module.extract_video_id("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk"
    assert refresh_module.extract_video_id("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert refresh_module.extract_video_id("https://www.youtube.com/shorts/abcdefghijk") == "abcdefghijk"


def test_quota_estimate_uses_batched_channel_and_representative_calls():
    records = [_record(f"rec{i}", f"UC{i}", "") for i in range(51)]

    # 2 channels.list batches + 102 per-channel recent-video units +
    # 1 batched representative-title videos.list call (same URL in fixtures).
    assert refresh_module._estimate_quota(records, refresh_module.ALL_REFRESH_FIELDS) == 105


def _mock_name_source(monkeypatch):
    monkeypatch.setattr(
        refresh_module, "fetch_channel_details",
        lambda *args, **kwargs: ([{"channel_id": "UC1", "channel_title": "Sarah Jones", "channel_description": "I'm Sarah."}], ""),
    )


def test_refresh_preserves_name_edited_during_youtube_fetch(monkeypatch):
    class ConcurrentClient(FakeClient):
        def batch_get_records(self, table_id, record_ids):
            latest = super().batch_get_records(table_id, record_ids)
            latest[0]["fields"]["KOL Name"] = "Human confirmed Sarah"
            return latest

    client = ConcurrentClient([_record("rec1", "UC1", "手动确认")])
    _mock_name_source(monkeypatch)
    result = refresh_module.refresh_influencers(api_key="key", client=client, table_id="table", fields="name")
    assert result["actual_updates"] == 0
    assert result["protected_names"] == 1
    assert client.updates == []


@pytest.mark.parametrize("latest_name", ["Human confirmed Sarah", ""])
def test_migration_flag_does_not_replace_concurrently_changed_name(monkeypatch, latest_name):
    class ConcurrentClient(FakeClient):
        def batch_get_records(self, table_id, record_ids):
            latest = super().batch_get_records(table_id, record_ids)
            latest[0]["fields"]["KOL Name"] = latest_name
            return latest

    client = ConcurrentClient([_record("rec1", "UC1", "Old automatic guess")])
    _mock_name_source(monkeypatch)
    result = refresh_module.refresh_influencers(
        api_key="key", client=client, table_id="table", fields="name", replace_kol_names=True,
    )
    assert result["actual_updates"] == 0
    assert result["protected_names"] == 1


@pytest.mark.parametrize("source_field", ["Channel ID", "代表视频URL", "频道描述", "联系邮箱"])
def test_refresh_skips_record_whose_source_changed_before_write(monkeypatch, source_field):
    class ConcurrentClient(FakeClient):
        def batch_get_records(self, table_id, record_ids):
            latest = super().batch_get_records(table_id, record_ids)
            latest[0]["fields"][source_field] = "changed by a person"
            return latest

    client = ConcurrentClient([_record("rec1", "UC1", "手动确认")])
    _mock_name_source(monkeypatch)
    result = refresh_module.refresh_influencers(api_key="key", client=client, table_id="table", fields="name")
    assert result["actual_updates"] == 0
    assert result["concurrent_skips"] == 1


def test_refresh_does_not_write_when_fresh_record_read_fails(monkeypatch):
    client = FakeClient([_record("rec1", "UC1", "手动确认")])
    _mock_name_source(monkeypatch)
    monkeypatch.setattr(client, "batch_get_records", lambda *args: (_ for _ in ()).throw(RuntimeError("read failed")))
    with pytest.raises(RuntimeError, match="read failed"):
        refresh_module.refresh_influencers(api_key="key", client=client, table_id="table", fields="name")
    assert client.updates == []


def test_refresh_skips_record_deleted_before_write(monkeypatch):
    client = FakeClient([_record("rec1", "UC1", "手动确认")])
    _mock_name_source(monkeypatch)
    monkeypatch.setattr(client, "batch_get_records", lambda *args: [])
    result = refresh_module.refresh_influencers(api_key="key", client=client, table_id="table", fields="name")
    assert result["actual_updates"] == 0
    assert result["missing_before_write"] == 1


def test_refresh_does_not_write_old_title_after_representative_url_changes(monkeypatch):
    class ConcurrentClient(FakeClient):
        def batch_get_records(self, table_id, record_ids):
            latest = super().batch_get_records(table_id, record_ids)
            latest[0]["fields"]["代表视频URL"] = "https://youtu.be/bcdefghijkl"
            return latest

    client = ConcurrentClient([_record("rec1", "UC1", "Sarah")])
    monkeypatch.setattr(refresh_module, "fetch_video_details", lambda *args, **kwargs: ([{"video_id": "abcdefghijk", "title": "Old representative"}], [], ""))
    result = refresh_module.refresh_influencers(api_key="key", client=client, table_id="table", fields="rep-title")
    assert result["actual_updates"] == 0
    assert result["concurrent_skips"] == 1
    assert client.updates == []
