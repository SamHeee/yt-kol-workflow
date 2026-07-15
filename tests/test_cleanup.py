from feishu.cleanup import AUTOMATIC_FIELD_TYPES, cleanup_empty_records, is_record_empty


def test_is_record_empty_ignores_all_automatic_field_types():
    automatic_fields = {f"auto_{field_type}" for field_type in AUTOMATIC_FIELD_TYPES}
    record = {
        "fields": {
            **{name: 1783697075000 for name in automatic_fields},
            "Channel ID": "",
            "备注": None,
        }
    }

    assert is_record_empty(record, automatic_fields)

    record["fields"]["Channel ID"] = "UC123"
    assert not is_record_empty(record, automatic_fields)


def test_cleanup_empty_records_resolves_automatic_fields_from_metadata():
    class FakeClient:
        def __init__(self):
            self.records = [
                {
                    "record_id": "rec-empty",
                    "fields": {
                        "视频记录日期": 1783697075000,
                        "最后更新时间": 1783697075000,
                        "创建人": [{"name": "workflow"}],
                        "修改人": [{"name": "workflow"}],
                        "自动编号": "0001",
                        "Video ID": "",
                    },
                },
                {
                    "record_id": "rec-data",
                    "fields": {
                        "视频记录日期": 1783697075000,
                        "Video ID": "video123",
                    },
                },
            ]
            self.deleted = []

        def list_tables(self):
            return [{"name": "视频数据表", "table_id": "tbl-video"}]

        def list_fields(self, table_id):
            assert table_id == "tbl-video"
            return [
                {"field_name": "视频记录日期", "type": 1001},
                {"field_name": "最后更新时间", "type": 1002},
                {"field_name": "创建人", "type": 1003},
                {"field_name": "修改人", "type": 1004},
                {"field_name": "自动编号", "type": 1005},
                {"field_name": "Video ID", "type": 1},
            ]

        def get_all_records(self, table_id):
            assert table_id == "tbl-video"
            return list(self.records)

        def batch_delete_records(self, table_id, record_ids):
            assert table_id == "tbl-video"
            self.deleted.extend(record_ids)
            self.records = [
                record
                for record in self.records
                if record["record_id"] not in record_ids
            ]
            return len(record_ids)

    client = FakeClient()

    results = cleanup_empty_records(client, ["视频数据表"])

    assert client.deleted == ["rec-empty"]
    assert [record["record_id"] for record in client.records] == ["rec-data"]
    assert results[0]["table_name"] == "视频数据表"
    assert results[0]["table_id"] == "tbl-video"
    assert results[0]["records_before"] == 2
    assert results[0]["empty_rows_before"] == 1
    assert results[0]["deleted_empty_rows"] == 1
    assert results[0]["records_after"] == 1
    assert results[0]["empty_rows_after"] == 0
    assert set(results[0]["automatic_fields_ignored"]) == {
        "视频记录日期",
        "最后更新时间",
        "创建人",
        "修改人",
        "自动编号",
    }
    assert results[0]["dry_run"] is False
