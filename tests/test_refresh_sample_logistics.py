from copy import deepcopy
from datetime import datetime, timedelta, timezone

from logistics.base import (
    TrackingEvent,
    TrackingResult,
    TrackingStatus,
    TrackingSyncStatus,
)
from workflow import refresh_sample_logistics as refresh_module
from feishu.sample_logistics import PROVIDER_DELIVERED_AT_FIELD, DELIVERED_DATE_REVIEW_FIELD, LAST_QUERY_SHIPMENT_KEY_FIELD


NOW = datetime(2026, 8, 4, 1, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


class FakeClient:
    def __init__(self, records, latest_records=None):
        self.records = deepcopy(records)
        self.latest_records = (
            deepcopy(latest_records) if latest_records is not None else None
        )
        self.batch_get_calls = []
        self.updates = []
        self.single_updates = []

    def get_all_records(self, table_id):
        return deepcopy(self.records)

    def batch_get_records(self, table_id, record_ids):
        self.batch_get_calls.append(list(record_ids))
        source = self.latest_records if self.latest_records is not None else self.records
        wanted = set(record_ids)
        return [
            deepcopy(record)
            for record in source
            if record.get("record_id") in wanted
        ]

    def batch_update_records(self, table_id, updates):
        self.updates.extend(deepcopy(updates))
        return len(updates)

    def update_record(self, table_id, record_id, fields):
        self.single_updates.append({"record_id": record_id, "fields": deepcopy(fields)})
        return {"record_id": record_id, "fields": deepcopy(fields)}


class FakeProvider:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = []

    def refresh(self, requests, *, register_first=False):
        self.calls.append({
            "requests": list(requests),
            "register_first": register_first,
        })
        if self.error:
            raise self.error
        wanted = {request.number for request in requests}
        return [result for result in self.results if result.number in wanted]


def _record(
    record_id="rec1",
    *,
    sample_id="SMP-000001",
    sample_status="已寄出",
    tracking_number="TRACK-001",
    carrier_code="ups",
    synced=False,
    shipped_at=None,
    delivered_at=None,
    last_queried_at=None,
    latest_status="",
    latest_progress="",
    latest_event_at=None,
    stagnation_started_at=None,
    source_synced_at=None,
    reminder=None,
):
    fields = {
        "多行文本": "",
        "样品记录ID": sample_id,
        "合作自动编号": "COOP-000001",
        "合作记录ID": "CP-000001",
        "联系邮箱": "kol@example.com",
        "KOL Name": "Sarah",
        "样品明细": "Sample",
        "收件信息": "lookup value",
        "样品状态": sample_status,
        "物流商": "UPS",
        "物流单号": tracking_number,
        "发货日期": shipped_at,
        "签收日期": delivered_at,
        "备注": "",
        "物流商编码": carrier_code,
        "已同步运单键": (
            refresh_module._shipment_key("17track", carrier_code, tracking_number)
            if synced and tracking_number
            else ""
        ),
        "物流状态": latest_status,
        "最新物流进度": latest_progress,
        "最新物流时间": latest_event_at,
        "物流停滞起算时间": stagnation_started_at,
        "物流源同步时间": source_synced_at,
        "上次查询时间": last_queried_at,
        "物流查询错误": "",
    }
    if reminder:
        fields.update(reminder)
    return {"record_id": record_id, "fields": fields}


def _result(
    number="TRACK-001",
    *,
    carrier="ups",
    status=TrackingStatus.IN_TRANSIT,
    sync_status=TrackingSyncStatus.SUCCESS,
    event_time=None,
    description="Departed facility",
    location="Shanghai",
    track_time=None,
    error_code="",
    error_message="",
):
    event = None
    if event_time is not None:
        event = TrackingEvent(
            description=description,
            event_time=event_time,
            location=location,
        )
    return TrackingResult(
        number=number,
        carrier=carrier,
        latest_status=status,
        latest_event=event,
        track_time=track_time or NOW,
        sync_status=sync_status,
        error_code=error_code,
        error_message=error_message,
    )


def test_candidates_are_filtered_and_identical_shipments_are_queried_once():
    records = [
        _record("rec1", sample_id="SMP-000001", carrier_code=""),
        _record("rec2", sample_id="SMP-000002", carrier_code=""),
        _record("rec3", sample_id="SMP-000003", sample_status="待寄出"),
        _record("rec4", sample_id="SMP-000004", tracking_number=""),
    ]
    provider = FakeProvider([
        _result(carrier="100003", event_time=NOW - timedelta(hours=2))
    ])
    client = FakeClient(records)

    outcome = refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
    )

    assert outcome["candidate_records"] == 2
    assert outcome["unique_shipments"] == 1
    assert outcome["actual_updates"] == 2
    assert outcome["skipped_counts"] == {
        "empty_tracking_number": 1,
        "ineligible_status": 1,
    }
    assert len(provider.calls) == 1
    assert provider.calls[0]["register_first"] is True
    assert len(provider.calls[0]["requests"]) == 1
    assert len(client.updates) == 2
    for update in client.updates:
        fields = update["fields"]
        assert fields["物流商编码"] == "100003"
        assert fields["物流状态"] == "运输中"
        assert fields["最新物流进度"] == "Shanghai｜Departed facility"
        assert fields["已同步运单键"] == refresh_module._shipment_key(
            "17track", "100003", "TRACK-001"
        )


def test_dry_run_never_calls_provider_registers_or_writes_feishu():
    provider = FakeProvider([_result(event_time=NOW)])
    client = FakeClient([_record(synced=False)])

    outcome = refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
        dry_run=True,
    )

    assert outcome["planned_updates"] == 1
    assert outcome["actual_updates"] == 0
    assert outcome["registration_shipments"] == 1
    assert outcome["provider_requests"] == 0
    assert provider.calls == []
    assert client.batch_get_calls == []
    assert client.updates == []
    assert client.single_updates == []


def test_known_synced_shipment_queries_without_registration():
    provider = FakeProvider([_result(event_time=NOW - timedelta(hours=1))])
    client = FakeClient([
        _record(synced=True, last_queried_at=NOW_MS - 25 * 3600 * 1000)
    ])

    refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
    )

    assert len(provider.calls) == 1
    assert provider.calls[0]["register_first"] is False


def test_fresh_known_shipment_is_skipped_unless_forced():
    provider = FakeProvider([_result(event_time=NOW)])
    record = _record(synced=True, last_queried_at=NOW_MS - 2 * 3600 * 1000)
    client = FakeClient([record])

    outcome = refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
        stale_hours=20,
    )

    assert outcome["candidate_records"] == 0
    assert outcome["skipped_counts"]["fresh"] == 1
    assert provider.calls == []


def test_delivered_only_promotes_eligible_status_and_preserves_manual_date():
    delivered_time = NOW - timedelta(hours=3)
    existing_date = NOW_MS - 5 * 24 * 3600 * 1000
    records = [
        _record("rec1", sample_id="SMP-000001", synced=True),
        _record(
            "rec2",
            sample_id="SMP-000002",
            synced=True,
            delivered_at=existing_date,
        ),
    ]
    provider = FakeProvider([
        _result(status=TrackingStatus.DELIVERED, event_time=delivered_time)
    ])
    client = FakeClient(records)

    outcome = refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
        force=True,
    )

    updates = {item["record_id"]: item["fields"] for item in client.updates}
    assert updates["rec1"]["样品状态"] == "已签收"
    assert updates["rec1"]["签收日期"] == int(delivered_time.timestamp() * 1000)
    assert updates["rec2"]["样品状态"] == "已签收"
    assert "签收日期" not in updates["rec2"]
    assert outcome["delivery_events"] == ["SMP-000001", "SMP-000002"]


def test_delivered_from_non_success_source_does_not_change_sample_status():
    provider = FakeProvider([
        _result(
            status=TrackingStatus.DELIVERED,
            sync_status=TrackingSyncStatus.SOURCE_FAILED,
            event_time=NOW - timedelta(hours=2),
            error_message="carrier timeout",
        )
    ])
    client = FakeClient([_record(synced=True)])

    refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
        force=True,
    )

    fields = client.updates[0]["fields"]
    assert "样品状态" not in fields
    assert "签收日期" not in fields
    assert "carrier timeout" in fields["物流查询错误"]


def test_pending_source_is_not_treated_as_fresh_progress():
    old_event_at = NOW_MS - 80 * 3600 * 1000
    provider = FakeProvider([
        _result(
            status=TrackingStatus.IN_TRANSIT,
            sync_status=TrackingSyncStatus.SOURCE_PENDING,
            event_time=NOW - timedelta(hours=1),
        )
    ])
    client = FakeClient([
        _record(
            synced=True,
            latest_status="运输中",
            latest_progress="Last confirmed event",
            latest_event_at=old_event_at,
            stagnation_started_at=old_event_at,
        )
    ])

    refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
        force=True,
    )

    fields = client.updates[0]["fields"]
    assert "最新物流进度" not in fields
    assert "物流停滞起算时间" not in fields
    assert "暂不判断滞留" in fields["物流查询错误"]


def test_new_event_resets_stagnation_but_same_event_does_not():
    old_event = NOW - timedelta(hours=80)
    old_event_ms = int(old_event.timestamp() * 1000)
    record = _record(
        synced=True,
        latest_status="运输中",
        latest_progress="Shanghai｜Departed facility",
        latest_event_at=old_event_ms,
        stagnation_started_at=old_event_ms,
    )

    same_provider = FakeProvider([_result(event_time=old_event)])
    same_client = FakeClient([record])
    refresh_module.refresh_sample_logistics(
        provider=same_provider,
        client=same_client,
        table_id="table",
        now=NOW,
        force=True,
    )
    assert "物流停滞起算时间" not in same_client.updates[0]["fields"]

    new_time = NOW - timedelta(hours=1)
    new_provider = FakeProvider([
        _result(event_time=new_time, description="Arrived at destination")
    ])
    new_client = FakeClient([record])
    refresh_module.refresh_sample_logistics(
        provider=new_provider,
        client=new_client,
        table_id="table",
        now=NOW,
        force=True,
    )
    assert new_client.updates[0]["fields"]["物流停滞起算时间"] == int(
        new_time.timestamp() * 1000
    )


def test_changed_progress_with_same_event_time_restarts_clock_at_detection():
    old_event = NOW - timedelta(hours=80)
    old_event_ms = int(old_event.timestamp() * 1000)
    record = _record(
        synced=True,
        latest_status="运输中",
        latest_progress="Shanghai｜Departed facility",
        latest_event_at=old_event_ms,
        stagnation_started_at=old_event_ms,
    )
    provider = FakeProvider([
        _result(event_time=old_event, description="Arrived at destination")
    ])
    client = FakeClient([record])

    refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
        force=True,
    )

    assert client.updates[0]["fields"]["物流停滞起算时间"] == NOW_MS


def test_older_provider_event_does_not_regress_latest_progress_or_reset_clock():
    latest_event = NOW - timedelta(hours=24)
    latest_event_ms = int(latest_event.timestamp() * 1000)
    record = _record(
        synced=True,
        latest_status="运输中",
        latest_progress="Shanghai｜Arrived at destination",
        latest_event_at=latest_event_ms,
        stagnation_started_at=latest_event_ms,
    )
    provider = FakeProvider([
        _result(
            event_time=latest_event - timedelta(hours=12),
            description="Older departure event",
        )
    ])
    client = FakeClient([record])

    refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
        force=True,
    )

    fields = client.updates[0]["fields"]
    assert "最新物流进度" not in fields
    assert "最新物流时间" not in fields
    assert "物流停滞起算时间" not in fields


def test_no_track_info_starts_72_hour_clock_from_ship_date():
    shipped_at = NOW - timedelta(days=4)
    provider = FakeProvider([
        _result(
            status=TrackingStatus.NOT_FOUND,
            sync_status=TrackingSyncStatus.NO_TRACK_INFO,
            event_time=None,
        )
    ])
    client = FakeClient([_record(shipped_at=int(shipped_at.timestamp() * 1000))])

    outcome = refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
    )

    fields = client.updates[0]["fields"]
    assert fields["物流状态"] == "暂无轨迹"
    assert fields["物流停滞起算时间"] == int(shipped_at.timestamp() * 1000)
    assert outcome["stagnant_candidates"] == ["SMP-000001"]


def test_tracking_number_change_clears_old_parcel_and_reminder_state():
    old_key = refresh_module._shipment_key("17track", "ups", "OLD-001")
    record = _record(
        tracking_number="NEW-001",
        latest_status="已签收",
        latest_progress="Old parcel delivered",
        latest_event_at=NOW_MS - 2 * 86400 * 1000,
        source_synced_at=NOW_MS - 2 * 86400 * 1000,
        reminder={
            "提醒类型": "送达次日",
            "提醒状态": "已发送",
            "提醒事件键": "old-event",
            "提醒发送时间": NOW_MS - 86400 * 1000,
            "提醒错误": "",
        },
    )
    record["fields"]["已同步运单键"] = old_key
    provider = FakeProvider([
        _result(
            number="NEW-001",
            status=TrackingStatus.UNKNOWN,
            sync_status=TrackingSyncStatus.INVALID_NUMBER,
            error_code="-18019902",
            error_message="invalid number",
        )
    ])
    client = FakeClient([record])

    refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
    )

    fields = client.updates[0]["fields"]
    assert fields["已同步运单键"] == ""
    assert fields["物流状态"] == "待查询"
    assert fields["最新物流进度"] == ""
    assert fields["最新物流时间"] is None
    assert fields["提醒状态"] is None
    assert fields["提醒事件键"] == ""
    assert "invalid number" in fields["物流查询错误"]


def test_write_before_read_skips_when_operator_changes_tracking_number():
    original = _record(tracking_number="TRACK-001")
    changed = _record(tracking_number="TRACK-002")
    provider = FakeProvider([_result(event_time=NOW)])
    client = FakeClient([original], latest_records=[changed])

    outcome = refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
    )

    assert outcome["planned_updates"] == 1
    assert outcome["concurrent_skips"] == 1
    assert outcome["actual_updates"] == 0
    assert client.updates == []


def test_provider_failure_preserves_latest_successful_progress():
    latest_event_at = NOW_MS - 10 * 3600 * 1000
    provider = FakeProvider(error=RuntimeError("API failed 17token=super-secret"))
    client = FakeClient([
        _record(
            synced=True,
            latest_status="运输中",
            latest_progress="Last successful event",
            latest_event_at=latest_event_at,
            source_synced_at=latest_event_at,
        )
    ])

    refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
        force=True,
    )

    fields = client.updates[0]["fields"]
    assert fields["上次查询时间"] == NOW_MS
    assert "物流状态" not in fields
    assert "最新物流进度" not in fields
    assert "最新物流时间" not in fields
    assert "物流源同步时间" not in fields
    assert "super-secret" not in fields["物流查询错误"]


def test_failed_query_uses_short_backoff_then_retries_instead_of_success_cache():
    _assert_failure_retry(synced=True)


def test_new_parcel_failure_also_uses_bounded_retry_without_fake_registration():
    _assert_failure_retry(synced=False)


def _assert_failure_retry(*, synced):
    record = _record(synced=synced)
    first_client = FakeClient([record])
    refresh_module.refresh_sample_logistics(
        provider=FakeProvider(error=RuntimeError("temporary failure")),
        client=first_client, table_id="table", now=NOW,
    )
    record["fields"].update(first_client.updates[0]["fields"])
    provider = FakeProvider([_result(event_time=NOW)])
    blocked = refresh_module.refresh_sample_logistics(
        provider=provider, client=FakeClient([record]), table_id="table",
        now=NOW + timedelta(minutes=1),
    )
    assert blocked["skipped_counts"] == {"retry_backoff": 1}
    assert provider.calls == []
    retried = refresh_module.refresh_sample_logistics(
        provider=provider, client=FakeClient([record]), table_id="table",
        now=NOW + timedelta(hours=1),
    )
    assert retried["candidate_records"] == 1
    assert len(provider.calls) == 1

    record["fields"]["物流单号"] = "NEW-002"
    changed = refresh_module.refresh_sample_logistics(
        provider=provider, client=FakeClient([record]), table_id="table",
        now=NOW + timedelta(minutes=1), dry_run=True,
    )
    assert changed["candidate_records"] == 1


def test_changed_parcel_preserves_manual_date_but_uses_new_delivery_event():
    old_delivery = NOW_MS - 10 * 86400 * 1000
    new_delivery = NOW - timedelta(hours=1)
    record = _record(tracking_number="NEW-001", delivered_at=old_delivery)
    record["fields"]["已同步运单键"] = refresh_module._shipment_key("17track", "ups", "OLD-001")
    record["fields"][PROVIDER_DELIVERED_AT_FIELD] = old_delivery
    client = FakeClient([record])
    refresh_module.refresh_sample_logistics(
        provider=FakeProvider([_result(number="NEW-001", status=TrackingStatus.DELIVERED, event_time=new_delivery)]),
        client=client, table_id="table", now=NOW,
    )
    update = client.updates[0]["fields"]
    assert "签收日期" not in update
    assert update[PROVIDER_DELIVERED_AT_FIELD] == int(new_delivery.timestamp() * 1000)
    assert update[DELIVERED_DATE_REVIEW_FIELD] == "已有签收日期待人工确认"


def test_legacy_signed_row_backfills_provider_date_without_clearing_history():
    historical_date = NOW_MS - 8 * 86400 * 1000
    record = _record(synced=True, sample_status="已签收", delivered_at=historical_date)
    provider_time = NOW - timedelta(days=2)
    client = FakeClient([record])
    result = refresh_module.refresh_sample_logistics(
        provider=FakeProvider([_result(status=TrackingStatus.DELIVERED, event_time=provider_time)]),
        client=client, table_id="table", now=NOW,
    )
    assert result["actual_updates"] == 1
    update = client.updates[0]["fields"]
    assert "签收日期" not in update
    assert update[PROVIDER_DELIVERED_AT_FIELD] == int(provider_time.timestamp() * 1000)
    assert update[DELIVERED_DATE_REVIEW_FIELD] == "已有签收日期待人工确认"


def test_manually_adjusted_auto_date_is_preserved_and_its_review_state_changes():
    record = _record(synced=True, delivered_at=NOW_MS - 5 * 86400 * 1000)
    record["fields"][PROVIDER_DELIVERED_AT_FIELD] = NOW_MS - 2 * 86400 * 1000
    record["fields"][DELIVERED_DATE_REVIEW_FIELD] = "物流填写"
    client = FakeClient([record])
    refresh_module.refresh_sample_logistics(
        provider=FakeProvider([_result(status=TrackingStatus.DELIVERED, event_time=NOW - timedelta(days=2))]),
        client=client, table_id="table", now=NOW,
    )
    update = client.updates[0]["fields"]
    assert "签收日期" not in update
    assert update[DELIVERED_DATE_REVIEW_FIELD] == "签收日期已人工调整"


def test_delivery_without_event_time_does_not_invent_or_reuse_delivery_date():
    old_date = NOW_MS - 8 * 86400 * 1000
    client = FakeClient([_record(delivered_at=old_date)])
    refresh_module.refresh_sample_logistics(
        provider=FakeProvider([_result(status=TrackingStatus.DELIVERED)]),
        client=client, table_id="table", now=NOW,
    )
    update = client.updates[0]["fields"]
    assert "签收日期" not in update
    assert update[PROVIDER_DELIVERED_AT_FIELD] is None
    assert update[DELIVERED_DATE_REVIEW_FIELD] == "物流签收时间缺失待人工确认"


def test_older_event_status_revision_keeps_existing_business_policy():
    old_time = NOW - timedelta(hours=80)
    client = FakeClient([_record(
        synced=True, latest_status="派送中", latest_progress="Destination facility",
        latest_event_at=int(old_time.timestamp() * 1000),
        stagnation_started_at=int(old_time.timestamp() * 1000),
    )])
    refresh_module.refresh_sample_logistics(
        provider=FakeProvider([_result(status=TrackingStatus.IN_TRANSIT, event_time=old_time - timedelta(hours=12))]),
        client=client, table_id="table", now=NOW, force=True,
    )
    update = client.updates[0]["fields"]
    assert update["物流状态"] == "运输中"
    assert update["物流停滞起算时间"] == NOW_MS
    assert "最新物流时间" not in update
    assert "最新物流进度" not in update


def test_result_samples_expose_field_names_but_not_logistics_values():
    tracking_number = "SENSITIVE-TRACK-001"
    provider = FakeProvider([
        _result(
            number=tracking_number,
            event_time=NOW,
            location="Sensitive street address",
        )
    ])
    client = FakeClient([_record(tracking_number=tracking_number)])

    outcome = refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
    )

    sample = outcome["samples"][0]
    assert "fields" not in sample
    assert "updated_field_names" in sample
    serialized = repr(outcome)
    assert tracking_number not in serialized
    assert "Sensitive street address" not in serialized


def test_batch_write_failure_falls_back_to_individual_updates():
    class FallbackClient(FakeClient):
        def batch_update_records(self, table_id, updates):
            raise RuntimeError("batch failed")

        def update_record(self, table_id, record_id, fields):
            if record_id == "rec2":
                raise RuntimeError("bad record")
            return super().update_record(table_id, record_id, fields)

    records = [
        _record("rec1", sample_id="SMP-000001"),
        _record("rec2", sample_id="SMP-000002"),
    ]
    provider = FakeProvider([_result(event_time=NOW)])
    client = FallbackClient(records)

    outcome = refresh_module.refresh_sample_logistics(
        provider=provider,
        client=client,
        table_id="table",
        now=NOW,
    )

    assert outcome["write_fallback_used"] is True
    assert outcome["actual_updates"] == 1
    assert outcome["write_failures"] == 1
    assert [item["record_id"] for item in client.single_updates] == ["rec1"]
