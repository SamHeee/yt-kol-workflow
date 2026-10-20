from types import SimpleNamespace

import pytest

from feishu import preflight
from feishu.bitable import BitableClient


def test_preflight_fails_when_test_record_deletion_is_not_confirmed(monkeypatch):
    client = BitableClient("offline", auth=None)
    monkeypatch.setattr(preflight, "_build_test_records", lambda marker: {
        "test-table": [{"fields": {"key": marker}}],
    })
    remaining = set()

    def post(path, data):
        if path.endswith("/batch_create"):
            remaining.add("test-record")
            return {"code": 0, "data": {"records": [{"record_id": "test-record"}]}}
        assert path.endswith("/batch_delete")
        return {"code": 0, "data": {"records": [{
            "record_id": "test-record", "deleted": False,
        }]}}

    monkeypatch.setattr(client, "_post", post)
    schema = SimpleNamespace(get_table_id=lambda name: "tbl")
    with pytest.raises(RuntimeError, match="测试数据清理失败"):
        preflight._write_and_cleanup_test_records(client, schema)
    assert remaining == {"test-record"}
