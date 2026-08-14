from feishu.bitable import BitableClient


def test_get_all_records_normalizes_null_items_to_empty_list(monkeypatch):
    client = BitableClient("test-token", auth=None)
    monkeypatch.setattr(
        client,
        "_get",
        lambda *args, **kwargs: {
            "code": 0,
            "data": {"items": None, "has_more": False, "page_token": ""},
        },
    )

    assert client.get_all_records("tbl-test") == []
