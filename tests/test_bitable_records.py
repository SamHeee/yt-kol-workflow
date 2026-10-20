import json
from types import SimpleNamespace

import pytest

from feishu.bitable import BitableClient
from feishu.lark_cli_bitable import LarkCliBitableClient


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


@pytest.mark.parametrize("method,args", [
    ("list_tables", ()), ("list_fields", ("tbl",)), ("get_all_records", ("tbl",)),
])
def test_lists_accept_explicit_first_page_empty_response_without_items(method, args, monkeypatch):
    client = BitableClient("offline", auth=None)
    monkeypatch.setattr(client, "_get", lambda *args: {
        "code": 0, "data": {"has_more": False, "total": 0},
    })
    assert getattr(client, method)(*args) == []


def test_cli_upsert_creates_into_confirmed_empty_table_with_omitted_items():
    calls = []
    responses = [
        {"ok": True, "identity": "user", "data": {"has_more": False, "total": 0}},
        {"ok": True, "identity": "user", "data": {"records": [{"record_id": "created"}]}},
    ]

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(responses.pop(0)), stderr="")

    client = LarkCliBitableClient("offline", profile="test", runner=runner)
    result = client.upsert_records("tbl", [{"fields": {"key": "new"}}], key_field="key")
    assert result == {"updated": 0, "created": 1}
    assert [call[0][4] for call in calls] == ["GET", "POST"]
    assert calls[1][0][5].endswith("/records/batch_create")


@pytest.mark.parametrize("data", [
    {}, {"total": 0}, {"has_more": False},
    {"total": 1, "has_more": False},
    {"total": "0", "has_more": False},
    {"total": 0.0, "has_more": False},
    {"total": False, "has_more": False},
    {"total": None, "has_more": False},
    {"total": 0, "has_more": True},
    {"total": 0, "has_more": 0},
    {"total": 0, "has_more": None},
    {"total": 0, "has_more": "false"},
])
def test_omitted_items_requires_integer_zero_and_explicit_false(data, monkeypatch):
    client = BitableClient("offline", auth=None)
    writes = []
    monkeypatch.setattr(client, "_get", lambda *args: {"code": 0, "data": data})
    monkeypatch.setattr(client, "_post", lambda *args: writes.append(args))
    with pytest.raises(RuntimeError):
        client.upsert_records("tbl", [{"fields": {"key": "new"}}], key_field="key")
    assert writes == []


@pytest.mark.parametrize("first", [
    {"items": [{"record_id": "old", "fields": {"key": "old"}}],
     "total": 2, "has_more": True, "page_token": "next"},
    {"items": [], "has_more": True, "page_token": "next"},
])
def test_omitted_items_zero_total_on_later_page_is_not_empty_inventory(first, monkeypatch):
    client = BitableClient("offline", auth=None)
    responses = [
        {"code": 0, "data": first},
        {"code": 0, "data": {"total": 0, "has_more": False}},
    ]
    writes = []
    monkeypatch.setattr(client, "_get", lambda *args: responses.pop(0))
    monkeypatch.setattr(client, "_post", lambda *args: writes.append(args))
    with pytest.raises(RuntimeError):
        client.upsert_records("tbl", [{"fields": {"key": "new"}}], key_field="key")
    assert responses == []
    assert writes == []


def test_error_response_with_omitted_empty_items_never_allows_upsert(monkeypatch):
    client = BitableClient("offline", auth=None)
    writes = []
    monkeypatch.setattr(client, "_get", lambda *args: {
        "code": 99991672, "data": {"total": 0, "has_more": False},
    })
    monkeypatch.setattr(client, "_post", lambda *args: writes.append(args))
    with pytest.raises(RuntimeError, match="99991672"):
        client.upsert_records("tbl", [{"fields": {"key": "new"}}], key_field="key")
    assert writes == []


@pytest.mark.parametrize("method,args", [
    ("list_tables", ()), ("list_fields", ("tbl",)), ("get_all_records", ("tbl",)),
])
def test_lists_read_all_pages(method, args, monkeypatch):
    client = BitableClient("offline", auth=None)
    calls = []

    def get(path, params):
        calls.append((path, dict(params)))
        if params.get("page_token") == "next":
            return {"code": 0, "data": {"items": [{"id": "second"}], "has_more": False}}
        return {"code": 0, "data": {
            "items": [{"id": "first"}], "has_more": True, "page_token": "next",
        }}

    monkeypatch.setattr(client, "_get", get)
    assert getattr(client, method)(*args) == [{"id": "first"}, {"id": "second"}]
    assert len(calls) == 2
    assert calls[1][1]["page_token"] == "next"


@pytest.mark.parametrize("failed_page", [1, 2])
def test_upsert_never_writes_after_incomplete_record_inventory(failed_page, monkeypatch):
    client = BitableClient("offline", auth=None)
    calls = []
    writes = []

    def get(path, params):
        calls.append(params)
        if len(calls) == failed_page:
            return {"code": 99991672, "msg": "private-value-must-not-leak"}
        return {"code": 0, "data": {
            "items": [{"record_id": "old", "fields": {"key": "first"}}],
            "has_more": True, "page_token": "next",
        }}

    monkeypatch.setattr(client, "_get", get)
    monkeypatch.setattr(client, "_post", lambda *args: writes.append(args))
    with pytest.raises(RuntimeError, match="99991672") as exc:
        client.upsert_records("tbl", [{"fields": {"key": "already-existing"}}], key_field="key")
    assert "private-value" not in str(exc.value)
    assert writes == []


@pytest.mark.parametrize("method,args", [("list_tables", ()), ("list_fields", ("tbl",))])
def test_metadata_list_errors_do_not_become_empty_inventory(method, args, monkeypatch):
    client = BitableClient("offline", auth=None)
    monkeypatch.setattr(client, "_get", lambda *args: {"code": 1254290, "msg": "rate limited"})
    with pytest.raises(RuntimeError, match="1254290"):
        getattr(client, method)(*args)


@pytest.mark.parametrize("next_token", [None, "", " ", 123, "same"])
def test_invalid_or_repeated_page_cursor_fails_closed(next_token, monkeypatch):
    client = BitableClient("offline", auth=None)
    calls = []

    def get(path, params):
        calls.append(params)
        token = "same" if len(calls) == 1 else next_token
        return {"code": 0, "data": {"items": [], "has_more": True, "page_token": token}}

    monkeypatch.setattr(client, "_get", get)
    with pytest.raises(RuntimeError, match="分页游标"):
        client.get_all_records("tbl")
    assert len(calls) == 2


@pytest.mark.parametrize("data", [
    {}, {"items": "invalid"}, {"items": [None]},
    {"items": None, "total": 1, "has_more": False},
    {"items": [], "has_more": "true"},
    {"items": [], "total": 1, "has_more": False},
    {"items": [], "total": 0, "has_more": True, "page_token": "next"},
    {"items": [], "total": False, "has_more": False},
])
def test_unknown_list_shapes_do_not_become_empty_inventory(data, monkeypatch):
    client = BitableClient("offline", auth=None)
    monkeypatch.setattr(client, "_get", lambda *args: {"code": 0, "data": data})
    with pytest.raises(RuntimeError):
        client.list_tables()


def test_ensure_fields_finds_existing_field_on_later_page(monkeypatch):
    client = BitableClient("offline", auth=None)
    writes = []

    def get(path, params):
        if params.get("page_token") == "next":
            return {"code": 0, "data": {
                "items": [{"field_name": "existing", "field_id": "fld2"}], "has_more": False,
            }}
        return {"code": 0, "data": {
            "items": [{"field_name": "first", "field_id": "fld1"}],
            "has_more": True, "page_token": "next",
        }}

    monkeypatch.setattr(client, "_get", get)
    monkeypatch.setattr(client, "_post", lambda *args: writes.append(args))
    assert client.ensure_fields("tbl", [("existing", 1)])["existing"] == "fld2"
    assert writes == []
