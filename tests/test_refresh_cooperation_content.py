from copy import deepcopy
from datetime import datetime, timezone

import pytest

from feishu.cooperation_content import SYSTEM_WRITABLE_FIELDS
from workflow import refresh_cooperation_content as refresh_module


NOW = datetime(2026, 8, 3, 4, 5, 6, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


class FakeClient:
    def __init__(self, records, latest_records=None):
        self.records = deepcopy(records)
        self.latest_records = deepcopy(latest_records) if latest_records is not None else None
        self.updates = []
        self.single_updates = []
        self.batch_get_calls = []

    def get_all_records(self, table_id):
        return deepcopy(self.records)

    def batch_get_records(self, table_id, record_ids):
        self.batch_get_calls.append(list(record_ids))
        source = self.latest_records if self.latest_records is not None else self.records
        wanted = set(record_ids)
        return [deepcopy(record) for record in source if record.get("record_id") in wanted]

    def batch_update_records(self, table_id, updates):
        self.updates.extend(deepcopy(updates))
        return len(updates)

    def update_record(self, table_id, record_id, fields):
        self.single_updates.append({"record_id": record_id, "fields": deepcopy(fields)})
        return {"record_id": record_id, "fields": deepcopy(fields)}


def _record(
    record_id="rec1",
    *,
    content_id="CNT-000001",
    url="https://www.youtube.com/watch?v=abcdefghijk",
    channel_id="UC_EXPECTED",
    cooperation_id="COOP-000001",
    lookup_value=None,
    content_status="待发布",
    video_id="",
    sync_status="待同步",
    data_updated_at=None,
    metrics=None,
):
    fields = {
        "多行文本": "",
        "内容记录ID": content_id,
        "合作自动编号": cooperation_id,
        "合作记录ID": (
            [{"type": "text", "text": cooperation_id}]
            if lookup_value is None
            else lookup_value
        ),
        "Channel ID": [{"type": "text", "text": channel_id}],
        "KOL Name": [{"type": "text", "text": "Sarah"}],
        "样品记录ID": "",
        "内容状态": content_status,
        "视频URL": {"text": url, "link": url},
        "Video ID": video_id,
        "数据同步状态": sync_status,
    }
    if data_updated_at is not None:
        fields["数据更新时间"] = data_updated_at
    if metrics:
        fields.update(metrics)
    return {"record_id": record_id, "fields": fields}


def _video(video_id="abcdefghijk", channel_id="UC_EXPECTED", **overrides):
    video = {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": "Published review",
        "published_at_raw": "2026-08-01T01:02:03Z",
        "view_count": 1000,
        "like_count": 50,
        "comment_count": 10,
        "view_count_available": True,
        "like_count_available": True,
        "comment_count_available": True,
    }
    video.update(overrides)
    return video


def test_success_updates_only_whitelisted_system_fields(monkeypatch):
    client = FakeClient([_record()])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([_video(ids[0])], [], ""),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key",
        client=client,
        table_id="table",
        now=NOW,
    )

    assert result["actual_updates"] == 1
    assert result["status_counts"]["同步成功"] == 1
    fields = client.updates[0]["fields"]
    assert set(fields) <= SYSTEM_WRITABLE_FIELDS
    assert fields["内容状态"] == "已发布"
    assert fields["Video ID"] == "abcdefghijk"
    assert fields["Video Title"] == "Published review"
    assert fields["Views"] == 1000
    assert fields["Likes"] == 50
    assert fields["Comments"] == 10
    assert fields["互动率(%)"] == 6.0
    assert fields["数据更新时间"] == NOW_MS
    assert fields["数据同步状态"] == "同步成功"
    assert fields["同步错误"] == ""
    assert "多行文本" not in fields
    assert "视频URL" not in fields
    assert "Channel ID" not in fields


def test_invalid_url_does_not_call_api_and_preserves_old_video_data(monkeypatch):
    old_metrics = {"Views": 99, "Likes": 8, "Comments": 7, "Video Title": "Old"}
    client = FakeClient([_record(url="https://example.com/watch?v=abcdefghijk", metrics=old_metrics)])

    def fail_if_called(*args, **kwargs):
        raise AssertionError("invalid URL must not call YouTube")

    monkeypatch.setattr(refresh_module, "fetch_video_details", fail_if_called)
    result = refresh_module.refresh_cooperation_content(
        api_key="key",
        client=client,
        table_id="table",
        now=NOW,
    )

    assert result["status_counts"]["URL无效"] == 1
    fields = client.updates[0]["fields"]
    assert set(fields) == {"数据同步状态", "同步错误"}
    assert fields["数据同步状态"] == "URL无效"


def test_duplicate_alias_urls_are_rejected_before_api(monkeypatch):
    records = [
        _record("rec1", content_id="CNT-000001"),
        _record(
            "rec2",
            content_id="CNT-000002",
            cooperation_id="COOP-000002",
            url="https://youtu.be/abcdefghijk?t=10",
        ),
    ]
    client = FakeClient(records)
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([_video(ids[0])], [], ""),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["status_counts"]["同步成功"] == 1
    assert result["status_counts"]["重复视频"] == 1
    assert len(client.updates) == 2
    updates_by_record = {item["record_id"]: item["fields"] for item in client.updates}
    assert updates_by_record["rec1"]["数据同步状态"] == "同步成功"
    assert updates_by_record["rec2"]["数据同步状态"] == "重复视频"
    assert "内容记录ID=CNT-000001" in updates_by_record["rec2"]["同步错误"]
    assert "record_id=rec1" in updates_by_record["rec2"]["同步错误"]


@pytest.mark.parametrize("invalid_status", ["已取消", "已下架", "unmatched"])
def test_invalid_historical_row_cannot_own_duplicate_video(monkeypatch, invalid_status):
    old = _record("rec-old", content_id="CNT-000001")
    if invalid_status == "unmatched":
        old["fields"]["合作自动编号"] = ""
    else:
        old["fields"]["内容状态"] = invalid_status
    client = FakeClient([old, _record("rec-new", content_id="CNT-000002")])
    monkeypatch.setattr(refresh_module, "fetch_video_details", lambda *a, **k: ([_video()], [], ""))
    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW, force=True
    )
    assert result["requested_video_ids"] == 1
    updates = {item["record_id"]: item["fields"] for item in client.updates}
    assert updates["rec-new"]["数据同步状态"] == "同步成功"
    assert result["status_counts"]["重复视频"] == 0


def test_duplicate_without_content_id_uses_stable_record_id_fallback(monkeypatch):
    records = [
        _record("rec_z", content_id=""),
        _record("rec_a", content_id="", url="https://youtu.be/abcdefghijk"),
    ]
    client = FakeClient(records)
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([_video(ids[0])], [], ""),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["status_counts"]["同步成功"] == 1
    assert result["status_counts"]["重复视频"] == 1
    updates_by_record = {item["record_id"]: item["fields"] for item in client.updates}
    assert updates_by_record["rec_a"]["数据同步状态"] == "同步成功"
    assert updates_by_record["rec_z"]["数据同步状态"] == "重复视频"
    assert "主记录标识：record_id=rec_a" in updates_by_record["rec_z"]["同步错误"]


def test_duplicate_malformed_content_ids_fall_back_to_record_id(monkeypatch):
    records = [
        _record("rec_z", content_id="CNT-00001"),
        _record("rec_a", content_id="AAA", url="https://youtu.be/abcdefghijk"),
    ]
    client = FakeClient(records)
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([_video(ids[0])], [], ""),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["status_counts"]["同步成功"] == 1
    assert result["status_counts"]["重复视频"] == 1
    updates_by_record = {item["record_id"]: item["fields"] for item in client.updates}
    assert updates_by_record["rec_a"]["数据同步状态"] == "同步成功"
    assert updates_by_record["rec_z"]["数据同步状态"] == "重复视频"
    assert "主记录标识：record_id=rec_a" in updates_by_record["rec_z"]["同步错误"]


def test_duplicate_valid_content_id_takes_priority_over_fallback_id(monkeypatch):
    records = [
        _record("rec_a", content_id="malformed"),
        _record("rec_z", content_id="CNT-000099", url="https://youtu.be/abcdefghijk"),
    ]
    client = FakeClient(records)
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([_video(ids[0])], [], ""),
    )

    refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    updates_by_record = {item["record_id"]: item["fields"] for item in client.updates}
    assert updates_by_record["rec_z"]["数据同步状态"] == "同步成功"
    assert updates_by_record["rec_a"]["数据同步状态"] == "重复视频"
    assert "内容记录ID=CNT-000099" in updates_by_record["rec_a"]["同步错误"]


def test_channel_mismatch_and_api_missing_preserve_existing_metrics(monkeypatch):
    records = [
        _record("rec1", metrics={"Views": 100}),
        _record(
            "rec2",
            content_id="CNT-000002",
            cooperation_id="COOP-000002",
            url="https://www.youtube.com/watch?v=bbbbbbbbbbb",
            metrics={"Views": 200},
        ),
    ]
    client = FakeClient(records)

    def fake_fetch(api_key, ids, quota_tracker=None):
        videos = [_video("abcdefghijk", channel_id="UC_WRONG")] if "abcdefghijk" in ids else []
        missing = [video_id for video_id in ids if video_id != "abcdefghijk"]
        return videos, missing, ""

    monkeypatch.setattr(refresh_module, "fetch_video_details", fake_fetch)
    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["status_counts"]["频道不匹配"] == 1
    assert result["status_counts"]["视频不可用"] == 1
    for update in client.updates:
        assert set(update["fields"]) == {"数据同步状态", "同步错误"}


def test_partial_metrics_update_available_values_and_mark_status(monkeypatch):
    client = FakeClient([
        _record(
            video_id="abcdefghijk",
            sync_status="API失败",
            metrics={"Views": 900, "Likes": 44, "Comments": 9, "互动率(%)": 5.89},
        )
    ])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([
            _video(
                ids[0],
                view_count=1000,
                like_count_available=False,
                comment_count=12,
            )
        ], [], ""),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["partial_metric_records"] == 1
    fields = client.updates[0]["fields"]
    assert fields["数据同步状态"] == "部分指标不可用"
    assert fields["Views"] == 1000
    assert fields["Comments"] == 12
    assert "Likes" not in fields
    assert "互动率(%)" not in fields
    assert "Likes" in fields["同步错误"]


def test_partial_metrics_for_changed_video_clear_stale_unavailable_values(monkeypatch):
    client = FakeClient([
        _record(
            video_id="oldoldold01",
            metrics={"Views": 900, "Likes": 44, "Comments": 9, "互动率(%)": 5.89},
        )
    ])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([
            _video(ids[0], like_count_available=False)
        ], [], ""),
    )

    refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    fields = client.updates[0]["fields"]
    assert fields["Likes"] is None
    assert fields["互动率(%)"] is None
    assert "已清空" in fields["同步错误"]


@pytest.mark.parametrize(
    ("content_status", "video_overrides"),
    [
        ("待发布", {"live_broadcast_content": "upcoming"}),
        ("", {"published_at_raw": "2026-08-04T01:02:03Z"}),
    ],
)
def test_upcoming_or_future_video_does_not_promote_content_status(
    monkeypatch,
    content_status,
    video_overrides,
):
    client = FakeClient([_record(content_status=content_status)])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: (
            [_video(ids[0], **video_overrides)],
            [],
            "",
        ),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["status_counts"]["同步成功"] == 1
    fields = client.updates[0]["fields"]
    assert "内容状态" not in fields


def test_api_requests_are_split_into_batches_of_50(monkeypatch):
    records = []
    for index in range(51):
        video_id = f"{index:011d}"
        records.append(_record(
            f"rec{index}",
            content_id=f"CNT-{index:06d}",
            cooperation_id=f"COOP-{index:06d}",
            url=f"https://youtu.be/{video_id}",
        ))
    client = FakeClient(records)
    batch_sizes = []

    def fake_fetch(api_key, ids, quota_tracker=None):
        batch_sizes.append(len(ids))
        return ([_video(video_id) for video_id in ids], [], "")

    monkeypatch.setattr(refresh_module, "fetch_video_details", fake_fetch)
    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert batch_sizes == [50, 1]
    assert result["api_batches"] == 2
    assert result["estimated_quota"] == 2
    assert result["actual_updates"] == 51


def test_dry_run_plans_without_batch_get_or_write(monkeypatch):
    client = FakeClient([_record()])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([_video(ids[0])], [], ""),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW, dry_run=True
    )

    assert result["planned_updates"] == 1
    assert result["actual_updates"] == 0
    assert client.batch_get_calls == []
    assert client.updates == []


def test_write_before_read_skips_record_when_operator_changes_url(monkeypatch):
    original = _record()
    changed = _record(url="https://youtu.be/bbbbbbbbbbb")
    client = FakeClient([original], latest_records=[changed])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([_video(ids[0])], [], ""),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["planned_updates"] == 1
    assert result["concurrent_skips"] == 1
    assert result["actual_updates"] == 0
    assert client.updates == []


def test_write_before_read_accepts_equivalent_batch_lookup_envelopes(monkeypatch):
    original = _record()
    latest = deepcopy(original)
    latest_fields = latest["fields"]
    latest_fields["合作自动编号"] = [
        {"type": "text", "text": "COOP-000001"}
    ]
    latest_fields["合作记录ID"] = {
        "type": 1,
        "value": [{"type": "text", "text": "COOP-000001"}],
    }
    latest_fields["Channel ID"] = {
        "type": 1,
        "value": [{"type": "text", "text": "UC_EXPECTED"}],
    }
    client = FakeClient([original], latest_records=[latest])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([_video(ids[0])], [], ""),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["concurrent_skips"] == 0
    assert result["actual_updates"] == 1
    assert len(client.updates) == 1


def test_fresh_success_is_skipped_until_stale(monkeypatch):
    client = FakeClient([
        _record(
            video_id="abcdefghijk",
            sync_status="同步成功",
            data_updated_at=NOW_MS,
            content_status="已发布",
        )
    ])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fresh data must skip API")),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW, stale_hours=24
    )

    assert result["planned_updates"] == 0
    assert result["skipped_counts"]["fresh"] == 1


def test_lookup_must_resolve_exactly_one_cooperation(monkeypatch):
    client = FakeClient([_record(lookup_value=[])])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("invalid lookup must skip API")),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["status_counts"]["合作匹配失败"] == 1
    assert set(client.updates[0]["fields"]) == {"数据同步状态", "同步错误"}


def test_api_error_is_sanitized_and_preserves_old_data(monkeypatch):
    client = FakeClient([_record(metrics={"Views": 321})])
    monkeypatch.setattr(
        refresh_module,
        "fetch_video_details",
        lambda api_key, ids, quota_tracker=None: ([], ids, f"request failed for key={api_key}"),
    )

    result = refresh_module.refresh_cooperation_content(
        api_key="SECRET-KEY", client=client, table_id="table", now=NOW
    )

    assert result["status_counts"]["API失败"] == 1
    assert "SECRET-KEY" not in client.updates[0]["fields"]["同步错误"]
    assert set(client.updates[0]["fields"]) == {"数据同步状态", "同步错误"}


def test_clean_error_redacts_feishu_app_oauth_bearer_and_cli_url_secrets():
    raw = (
        "CLI request failed "
        "app_token=bascnSuperSecret "
        "app_id=cli_SensitiveApp "
        "access_token=oauth-secret "
        "Authorization: Bearer bearer-secret "
        "url=https://tenant.feishu.cn/wiki/wikcnSensitiveNode?code=oauth-code "
        "endpoint=/open-apis/bitable/v1/apps/bascnPathSecret/tables"
    )

    cleaned = refresh_module._clean_error(raw)

    for secret in (
        "bascnSuperSecret",
        "cli_SensitiveApp",
        "oauth-secret",
        "bearer-secret",
        "wikcnSensitiveNode",
        "oauth-code",
        "bascnPathSecret",
    ):
        assert secret not in cleaned
    assert cleaned.startswith("CLI request failed")
    assert "https://tenant.feishu.cn/<redacted>" in cleaned


def test_clean_error_preserves_ordinary_diagnostic_text():
    message = "one row made the batch fail (code=1254001); retry later"

    assert refresh_module._clean_error(message) == message


def test_batch_write_failure_falls_back_to_individual_updates(monkeypatch):
    class FallbackClient(FakeClient):
        def batch_update_records(self, table_id, updates):
            raise RuntimeError("one row made the batch fail")

        def update_record(self, table_id, record_id, fields):
            if record_id == "rec2":
                raise RuntimeError("bad record")
            return super().update_record(table_id, record_id, fields)

    records = [
        _record("rec1", content_id="CNT-000001"),
        _record(
            "rec2",
            content_id="CNT-000002",
            cooperation_id="COOP-000002",
            url="https://youtu.be/bbbbbbbbbbb",
        ),
    ]
    client = FallbackClient(records)

    def fake_fetch(api_key, ids, quota_tracker=None):
        return ([_video(video_id) for video_id in ids], [], "")

    monkeypatch.setattr(refresh_module, "fetch_video_details", fake_fetch)
    result = refresh_module.refresh_cooperation_content(
        api_key="key", client=client, table_id="table", now=NOW
    )

    assert result["write_fallback_used"] is True
    assert result["batch_write_error"] == "one row made the batch fail"
    assert result["actual_updates"] == 1
    assert result["write_failures"] == 1
    assert [item["record_id"] for item in client.single_updates] == ["rec1"]
    assert any(
        item.get("stage") == "feishu_write" and item["record_id"] == "rec2"
        for item in result["errors"]
    )
    assert any(item.get("stage") == "feishu_batch_write" for item in result["errors"])
