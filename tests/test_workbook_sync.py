import pytest

from feishu import workbook_sync


class FakeClient:
    def __init__(self):
        self.records = [{"record_id": "blank-record", "fields": {}}]

    def get_all_records(self, table_id):
        return self.records


def _plan_for(monkeypatch, table_key, source_row):
    config = workbook_sync.TABLE_CONFIGS[table_key]
    field_meta = {
        field_name: {"type": 1, "ui_type": "Text"}
        for field_name in config["field_map"].values()
    }
    monkeypatch.setattr(
        workbook_sync,
        "list_field_meta",
        lambda client, table_id: field_meta,
    )
    monkeypatch.setattr(
        workbook_sync,
        "load_source_rows",
        lambda workbook_path, sheet, limit=None: [source_row],
    )

    return workbook_sync.plan_table(
        FakeClient(),
        workbook_path=None,
        table_key=table_key,
        table_id="table-id",
    )


def test_workbook_sync_covers_all_four_schema_tables():
    assert list(workbook_sync.TABLE_CONFIGS) == [
        "search_tasks",
        "search_videos",
        "influencers",
        "influencer_videos",
    ]
    assert workbook_sync.TABLE_CONFIGS["search_tasks"]["key_field"] == "唯一键"


@pytest.mark.parametrize(
    ("table_key", "source_row"),
    [
        ("search_videos", {"Video ID": "video-1"}),
        ("influencers", {"频道ID": "channel-1"}),
    ],
)
def test_new_keys_do_not_reuse_blank_records_for_record_dated_tables(
    monkeypatch,
    table_key,
    source_row,
):
    stats, updates, creates = _plan_for(monkeypatch, table_key, source_row)

    assert updates == []
    assert len(creates) == 1
    assert stats["planned_updates"] == 0
    assert stats["planned_creates"] == 1
    assert stats["blank_records_available"] == 1
    assert stats["blank_records_reused"] == 0
    assert stats["reuse_blank_records"] is False
    assert stats["blank_records_left_unreused"] == 1


def test_other_tables_keep_reusing_blank_records(monkeypatch):
    stats, updates, creates = _plan_for(
        monkeypatch,
        "influencer_videos",
        {"Video ID": "video-1"},
    )

    assert creates == []
    assert updates == [
        {
            "record_id": "blank-record",
            "fields": {"唯一键": "video-1", "Video ID": "video-1"},
        }
    ]
    assert stats["planned_updates"] == 1
    assert stats["planned_creates"] == 0
    assert stats["blank_records_reused"] == 1
    assert stats["reuse_blank_records"] is True
    assert stats["blank_records_left_unreused"] == 0


def test_post_batch_uses_public_client_methods():
    class PublicClient:
        def batch_create_records(self, table_id, records):
            assert table_id == "tbl1"
            return len(records)

        def batch_update_records(self, table_id, records):
            assert table_id == "tbl1"
            return len(records)

    client = PublicClient()
    records = [{"fields": {"Name": "A"}}]

    assert workbook_sync._post_batch(client, "tbl1", "batch_create", records) == 1
    assert workbook_sync._post_batch(client, "tbl1", "batch_update", records) == 1


def test_post_batch_falls_back_to_single_record_update(monkeypatch):
    class FailingBatchClient:
        def batch_update_records(self, table_id, records):
            raise RuntimeError("batch unavailable")

    monkeypatch.setattr(
        workbook_sync,
        "_put_records_one_by_one",
        lambda client, table_id, records: len(records),
    )

    assert workbook_sync._post_batch(
        FailingBatchClient(),
        "tbl1",
        "batch_update",
        [{"record_id": "rec1", "fields": {"Name": "A"}}],
    ) == 1


def test_legacy_headers_are_normalized_and_composite_key_is_rebuilt():
    row = workbook_sync.normalize_source_row(
        "search_videos",
        {
            "搜索关键词": "camping review",
            "Video ID": "video-1",
            "频道ID": "channel-1",
            "视频标题": "Legacy title",
        },
    )

    assert row["唯一键"] == "camping review|video-1"
    assert row["Channel ID"] == "channel-1"
    assert row["Video Title"] == "Legacy title"
    assert "频道ID" not in row
    assert "视频标题" not in row


def test_primary_and_created_time_columns_are_read_only():
    config = workbook_sync.TABLE_CONFIGS["search_videos"]
    field_meta = {
        field_name: {"type": 1, "ui_type": "Text"}
        for field_name in config["field_map"].values()
    }
    fields = workbook_sync.build_fields(
        {
            "多行文本": "must not write",
            "唯一键": "keyword|video-1",
            "视频记录日期": 1783699200000,
            "Video ID": "video-1",
        },
        config,
        field_meta,
    )

    assert fields == {"唯一键": "keyword|video-1", "Video ID": "video-1"}
