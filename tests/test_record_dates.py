import pytest

from feishu.bitable import BitableClient
from feishu.preflight import _validate_created_time_field
from feishu.schema import INFLUENCERS_TABLE, SEARCH_VIDEOS_TABLE
from main import _populate_created_times


def test_batch_get_records_requests_automatic_fields():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def _post(self, path, data):
            self.calls.append((path, data))
            return {
                "code": 0,
                "data": {
                    "records": [
                        {
                            "record_id": "rec1",
                            "created_time": 1783699200000,
                            "fields": {"视频记录日期": 1783699200000},
                        }
                    ]
                },
            }

    client = FakeClient()
    records = BitableClient.batch_get_records(
        client,
        "tbl1",
        ["rec1"],
        automatic_fields=True,
    )

    assert records[0]["created_time"] == 1783699200000
    assert client.calls == [
        (
            "/tables/tbl1/records/batch_get",
            {"record_ids": ["rec1"], "automatic_fields": True},
        )
    ]


def test_get_records_by_field_values_resolves_ids_and_reads_automatic_fields():
    class FakeClient:
        def __init__(self):
            self.batch_call = None

        def get_record_id_map(self, table_id, field_name):
            assert table_id == "tbl1"
            assert field_name == "唯一键"
            return {"keyword|video-1": "rec1"}

        def batch_get_records(self, table_id, record_ids, *, automatic_fields=False):
            self.batch_call = (table_id, record_ids, automatic_fields)
            return [{
                "record_id": "rec1",
                "created_time": 1783699200000,
                "fields": {"视频记录日期": 1783699200000},
            }]

    client = FakeClient()
    records = BitableClient.get_records_by_field_values(
        client,
        "tbl1",
        "唯一键",
        ["keyword|video-1", "keyword|video-1", "missing"],
        automatic_fields=True,
    )

    assert records["keyword|video-1"]["record_id"] == "rec1"
    assert client.batch_call == ("tbl1", ["rec1"], True)


def test_populate_created_times_uses_named_system_field_and_leaves_missing_blank():
    class FakeClient:
        def get_records_by_field_values(
            self,
            table_id,
            field_name,
            values,
            *,
            automatic_fields=False,
        ):
            assert (table_id, field_name, automatic_fields) == ("tbl1", "唯一键", True)
            assert values == ["keyword|video-1", "keyword|video-2"]
            return {
                "keyword|video-1": {
                    "fields": {"视频记录日期": 1783699200000},
                    "created_time": 111,
                }
            }

    items = [{"video_id": "video-1"}, {"video_id": "video-2"}]
    _populate_created_times(
        FakeClient(),
        "tbl1",
        items,
        source_key=lambda item: f"keyword|{item['video_id']}",
        feishu_key_field="唯一键",
        created_time_field="视频记录日期",
        output_key="video_record_date",
    )

    assert items[0]["video_record_date"] == 1783699200000
    assert items[1]["video_record_date"] == ""


def test_preflight_created_time_validation_skips_undeployed_field():
    class FakeClient:
        def list_fields(self, table_id):
            return []

        def batch_get_records(self, *args, **kwargs):
            raise AssertionError("undeployed fields must not trigger record readback")

    _validate_created_time_field(
        FakeClient(),
        SEARCH_VIDEOS_TABLE,
        "tbl1",
        ["rec1"],
    )


@pytest.mark.parametrize(
    ("table_name", "field_name"),
    [
        (SEARCH_VIDEOS_TABLE, "视频记录日期"),
        (INFLUENCERS_TABLE, "网红记录日期"),
    ],
)
def test_preflight_created_time_validation_reads_nonblank_system_field(
    table_name,
    field_name,
):
    class FakeClient:
        def list_fields(self, table_id):
            return [{"field_name": field_name, "type": 1001}]

        def batch_get_records(self, table_id, record_ids, *, automatic_fields=False):
            assert automatic_fields is True
            return [
                {
                    "record_id": record_ids[0],
                    "created_time": 1783699200000,
                    "fields": {field_name: 1783699200000},
                }
            ]

    _validate_created_time_field(
        FakeClient(),
        table_name,
        "tbl1",
        ["rec1"],
    )


def test_preflight_created_time_validation_rejects_blank_system_field():
    class FakeClient:
        def list_fields(self, table_id):
            return [{"field_name": "视频记录日期", "type": 1001}]

        def batch_get_records(self, table_id, record_ids, *, automatic_fields=False):
            return [
                {
                    "record_id": record_ids[0],
                    "created_time": 1783699200000,
                    "fields": {"视频记录日期": None},
                }
            ]

    with pytest.raises(RuntimeError, match="飞书创建时间字段为空"):
        _validate_created_time_field(
            FakeClient(),
            SEARCH_VIDEOS_TABLE,
            "tbl1",
            ["rec1"],
        )
