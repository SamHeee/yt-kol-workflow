import pytest

from feishu.bitable import BitableClient


def test_delete_counts_only_confirmed_distinct_records(monkeypatch):
    client = BitableClient("offline", auth=None)
    calls = []

    def post(path, data):
        calls.append(data)
        return {"code": 0, "data": {"records": [
            {"record_id": rid, "deleted": True} for rid in reversed(data["records"])
        ]}}

    monkeypatch.setattr(client, "_post", post)
    assert client.batch_delete_records("tbl", ["rec1", "rec2", "rec1", ""]) == 2
    assert calls == [{"records": ["rec1", "rec2"]}]


@pytest.mark.parametrize("records", [
    None, [],
    [{"record_id": "rec1", "deleted": False}],
    [{"record_id": "rec1", "deleted": 1}],
    [{"record_id": "rec1", "deleted": True}],
    [{"record_id": "rec1", "deleted": True}, {"record_id": "rec2", "deleted": False}],
    [{"record_id": "rec1", "deleted": True}, {"record_id": "rec1", "deleted": True}],
    [{"record_id": "unexpected", "deleted": True}],
])
def test_delete_rejects_missing_failed_or_ambiguous_confirmations(records, monkeypatch):
    client = BitableClient("offline", auth=None)
    monkeypatch.setattr(client, "_post", lambda *args: {"code": 0, "data": {"records": records}})
    with pytest.raises(RuntimeError, match="删除"):
        client.batch_delete_records("tbl", ["rec1", "rec2"])
