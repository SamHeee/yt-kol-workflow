from datetime import datetime, timezone

import pytest

from export.excel_exporter import (
    INFLUENCER_COLUMNS,
    INFLUENCER_VIDEO_COLUMNS,
    SEARCH_TASK_COLUMNS,
    SEARCH_VIDEO_COLUMNS,
)
from feishu.preflight import _build_test_records
from feishu.value_utils import timestamp_ms
from feishu.schema import (
    INFLUENCERS_FIELDS,
    INFLUENCERS_FIELD_OPTIONS,
    INFLUENCERS_TABLE,
    INFLUENCER_VIDEOS_FIELDS,
    PRIMARY_FIELD_NAME,
    SEARCH_TASKS_FIELDS,
    SEARCH_VIDEOS_FIELDS,
    SEARCH_VIDEOS_TABLE,
    _ts,
    ensure_influencer_field_options,
    format_influencer_record,
    format_search_video_record,
)
from feishu.workbook_sync import TABLE_CONFIGS, build_fields


NEW_FIELD_NAMES = [
    "KOL Name",
    "最新发布日期",
    "断更评估",
    "频道初步判断",
    "代表视频标题",
]


@pytest.mark.parametrize(
    ("field_specs", "columns", "expected_count"),
    [
        (SEARCH_TASKS_FIELDS, SEARCH_TASK_COLUMNS, 13),
        (SEARCH_VIDEOS_FIELDS, SEARCH_VIDEO_COLUMNS, 20),
        (INFLUENCERS_FIELDS, INFLUENCER_COLUMNS, 25),
        (INFLUENCER_VIDEOS_FIELDS, INFLUENCER_VIDEO_COLUMNS, 16),
    ],
)
def test_excel_headers_are_derived_from_feishu_schema(
    field_specs,
    columns,
    expected_count,
):
    schema_names = [name for name, _field_type in field_specs]
    assert len(schema_names) == expected_count
    assert schema_names[0] == PRIMARY_FIELD_NAME
    assert list(columns.values()) == schema_names


def test_influencer_schema_and_excel_column_order():
    schema_names = [name for name, _field_type in INFLUENCERS_FIELDS]
    assert len(schema_names) == 25
    assert schema_names[:8] == [
        "多行文本",
        "Channel ID",
        "Channel Name",
        "KOL Name",
        "网红记录日期",
        "最新发布日期",
        "断更评估",
        "频道URL",
    ]
    assert "唯一键" not in schema_names
    assert "频道均播" not in schema_names
    assert schema_names.index("频道初步判断") == schema_names.index("频道描述") + 1
    assert schema_names.index("代表视频标题") == schema_names.index("代表视频URL") + 1
    assert dict(INFLUENCERS_FIELDS)["最新发布日期"] == 5
    assert dict(INFLUENCERS_FIELDS)["断更评估"] == 3
    assert dict(INFLUENCERS_FIELDS)["网红记录日期"] == 1001

    excel_headers = list(INFLUENCER_COLUMNS.values())
    assert len(excel_headers) == 25
    assert excel_headers[:8] == [
        "多行文本",
        "Channel ID",
        "Channel Name",
        "KOL Name",
        "网红记录日期",
        "最新发布日期",
        "断更评估",
        "频道URL",
    ]
    assert excel_headers == schema_names
    assert "唯一键" not in excel_headers
    assert "频道均播" not in excel_headers
    assert excel_headers[-2:] == ["开发负责人", "备注"]
    assert excel_headers.index("频道初步判断") == excel_headers.index("频道描述") + 1
    assert excel_headers.index("代表视频标题") == excel_headers.index("代表视频URL") + 1


def test_influencer_formatter_writes_new_fields_and_omits_empty_date():
    channel = {
        "channel_id": "UC123",
        "channel_title": "Sarah Outdoors",
        "kol_name": "Sarah",
        "latest_published_at": "2026-07-10T12:30:00Z",
        "activity_status": "持续更新",
        "channel_initial_assessment": (
            "领域=户外/露营; 内容=产品测评,教程; "
            "主体=个人创作者; 自有品牌=疑似"
        ),
        "rep_video_title": "Current creator title",
    }
    representative_video = {
        "video_url": "https://www.youtube.com/watch?v=abc",
        "title": "Fallback title",
    }

    fields = format_influencer_record(channel, representative_video)["fields"]
    assert fields["KOL Name"] == "Sarah"
    assert fields["最新发布日期"] == int(
        datetime(2026, 7, 10, 12, 30, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert fields["断更评估"] == "持续更新"
    assert fields["频道初步判断"].startswith("领域=户外/露营;")
    assert fields["代表视频标题"] == "Current creator title"
    assert "唯一键" not in fields
    assert "频道均播" not in fields
    assert "网红记录日期" not in fields
    assert "多行文本" not in fields

    channel["latest_published_at"] = ""
    channel["rep_video_title"] = ""
    fields = format_influencer_record(channel, representative_video)["fields"]
    assert "最新发布日期" not in fields
    assert "频道创建日期" not in fields
    assert fields["代表视频标题"] == "Fallback title"


def test_timestamp_conversion_treats_legacy_and_rfc3339_values_as_utc():
    expected = int(datetime(2026, 7, 10, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert _ts("2026-07-10T12:30:00Z") == expected
    assert _ts("2026-07-10T12:30:00+00:00") == expected
    assert _ts("2026-07-10 12:30:00") == expected
    assert _ts("2026-07-10T20:30:00+08:00") == expected
    assert _ts(1234567890000) == 1234567890000
    assert _ts("") == 0
    assert timestamp_ms("2026-07-10 12:30:00") == expected
    assert timestamp_ms("2026-07-10T20:30:00+08:00") == expected


def test_workbook_mapping_has_all_new_fields_without_removed_fields():
    mapping = TABLE_CONFIGS["influencers"]["field_map"]
    for field_name in NEW_FIELD_NAMES:
        assert mapping[field_name] == field_name
    assert "唯一键" not in mapping
    assert "频道均播" not in mapping
    assert mapping["多行文本"] == "多行文本"
    assert {"多行文本", "网红记录日期"} <= TABLE_CONFIGS["influencers"]["read_only_fields"]


def test_workbook_sync_preserves_confirmed_kol_name_but_replaces_placeholder():
    config = {
        "field_map": {"KOL Name": "KOL Name"},
        "preserve_existing_nonblank": set(),
    }
    field_meta = {"KOL Name": {"type": 1, "ui_type": "Text"}}

    assert build_fields(
        {"KOL Name": "New automatic guess"},
        config,
        field_meta,
        {"KOL Name": "Sarah"},
    ) == {}
    assert build_fields(
        {"KOL Name": "Sarah"},
        config,
        field_meta,
        {"KOL Name": "手动确认"},
    ) == {"KOL Name": "Sarah"}
    assert build_fields(
        {"KOL Name": "手动确认"},
        config,
        field_meta,
        {"KOL Name": ""},
    ) == {"KOL Name": "手动确认"}


def test_preflight_record_exercises_all_new_fields():
    records = _build_test_records("marker")
    fields = records[INFLUENCERS_TABLE][0]["fields"]
    assert fields["KOL Name"] == "Sarah"
    assert fields["最新发布日期"] > 0
    assert fields["断更评估"] == "持续更新"
    assert fields["频道初步判断"]
    assert fields["代表视频标题"] == "KOL Preflight Test Video"
    assert "网红记录日期" not in fields
    assert "视频记录日期" not in records[SEARCH_VIDEOS_TABLE][0]["fields"]


def test_video_created_time_schema_and_formatter_are_system_managed():
    assert dict(SEARCH_VIDEOS_FIELDS)["视频记录日期"] == 1001
    assert [name for name, _field_type in SEARCH_VIDEOS_FIELDS][:5] == [
        "多行文本",
        "搜索关键词",
        "视频URL",
        "视频记录日期",
        "Video ID",
    ]
    assert SEARCH_VIDEOS_FIELDS[-1] == ("唯一键", 1)

    fields = format_search_video_record(
        {
            "video_id": "abc123",
            "video_url": "https://www.youtube.com/watch?v=abc123",
            "channel_id": "UC123",
            "channel_title": "Test Channel",
            "title": "Test Video",
            "published_at": "2026-07-10T12:30:00Z",
        },
        "test keyword",
    )["fields"]

    assert "视频记录日期" not in fields
    assert "多行文本" not in fields


def test_schema_ensures_activity_single_select_options():
    class FakeClient:
        def __init__(self):
            self.updated = None

        def list_fields(self, table_id):
            return [{
                "field_id": "fld1",
                "field_name": "断更评估",
                "type": 3,
                "property": {"options": []},
            }]

        def update_field(self, table_id, field_id, name, field_type, property):
            self.updated = property

    client = FakeClient()
    ensure_influencer_field_options(client, "tbl1")

    assert [item["name"] for item in client.updated["options"]] == INFLUENCERS_FIELD_OPTIONS["断更评估"]
