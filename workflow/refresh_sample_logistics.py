#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh logistics data for existing Feishu sample records.

The workflow owns selection, de-duplication, idempotency and conservative
business-state transitions.  Provider clients only perform registration and
tracking lookups, while all Feishu access is supplied through the configured
``BitableClient`` transport (the command layer wires that to ``lark-cli``).
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from feishu.bitable import BitableClient
from feishu.sample_logistics import (
    CARRIER_CODE_FIELD,
    CARRIER_FIELD,
    DELIVERED_DATE_FIELD,
    DELIVERED_DATE_REVIEW_FIELD,
    ELIGIBLE_SAMPLE_STATUSES,
    LAST_QUERIED_AT_FIELD,
    LAST_QUERY_SHIPMENT_KEY_FIELD,
    LATEST_EVENT_AT_FIELD,
    LATEST_PROGRESS_FIELD,
    LOGISTICS_STATUS_FIELD,
    LOGISTICS_STATUS_OPTIONS,
    QUERY_ERROR_FIELD,
    PROVIDER_DELIVERED_AT_FIELD,
    REMINDER_ERROR_FIELD,
    REMINDER_EVENT_KEY_FIELD,
    REMINDER_SENT_AT_FIELD,
    REMINDER_STATUS_FIELD,
    REMINDER_TYPE_FIELD,
    SAMPLE_RECORD_ID_FIELD,
    SAMPLE_STATUS_FIELD,
    SHIPPED_DATE_FIELD,
    SOURCE_SYNCED_AT_FIELD,
    STAGNATION_STARTED_AT_FIELD,
    SYNCED_SHIPMENT_KEY_FIELD,
    TRACKING_NUMBER_FIELD,
    validate_logistics_update_fields,
)
from feishu.value_utils import field_to_text
from logistics.base import (
    TrackingEvent,
    TrackingRequest,
    TrackingResult,
    TrackingStatus,
    TrackingSyncStatus,
)
from logistics.shipment_identity import (
    shipment_key as _shipment_key,
    normalize_carrier as _normalize_carrier,
    normalize_tracking_number as _normalize_number,
)


logger = logging.getLogger("kol_workflow.workflow.refresh_sample_logistics")

FEISHU_WRITE_BATCH_SIZE = 200
MAX_ERROR_LENGTH = 500
DEFAULT_STALE_HOURS = 20.0
STAGNATION_HOURS = 72.0
FAILED_QUERY_RETRY_HOURS = 5 / 60

_ACCEPTED_REGISTRATION_OUTCOMES = frozenset({
    TrackingSyncStatus.SUCCESS.value,
    TrackingSyncStatus.REGISTERED.value,
    TrackingSyncStatus.NO_TRACK_INFO.value,
    TrackingSyncStatus.SOURCE_PENDING.value,
    TrackingSyncStatus.SOURCE_FAILED.value,
})

_LOGISTICS_STATUS_MAP = {
    TrackingStatus.PENDING_QUERY.value: "待查询",
    TrackingStatus.NOT_FOUND.value: "暂无轨迹",
    TrackingStatus.INFO_RECEIVED.value: "待揽收",
    TrackingStatus.IN_TRANSIT.value: "运输中",
    TrackingStatus.AVAILABLE_FOR_PICKUP.value: "待取件",
    TrackingStatus.OUT_FOR_DELIVERY.value: "派送中",
    TrackingStatus.DELIVERY_FAILURE.value: "投递失败",
    TrackingStatus.DELIVERED.value: "已签收",
    TrackingStatus.RETURNED.value: "退回",
    TrackingStatus.EXPIRED.value: "已过期",
    TrackingStatus.EXCEPTION.value: "异常",
    TrackingStatus.UNKNOWN.value: "未知",
}

_SYNC_ERROR_LABELS = {
    TrackingSyncStatus.SOURCE_PENDING.value: "物流源仍在同步，暂不判断滞留",
    TrackingSyncStatus.SOURCE_FAILED.value: "物流源同步失败",
    TrackingSyncStatus.NOT_REGISTERED.value: "运单尚未在物流平台注册",
    TrackingSyncStatus.INVALID_NUMBER.value: "物流单号无效",
    TrackingSyncStatus.UNSUPPORTED_CARRIER.value: "物流商不支持",
    TrackingSyncStatus.ADDITIONAL_PARAMETER_REQUIRED.value: "物流商要求附加参数，MVP 不上传收件人信息",
    TrackingSyncStatus.REJECTED.value: "物流平台拒绝查询",
    TrackingSyncStatus.API_ERROR.value: "物流 API 查询失败",
}


def refresh_sample_logistics(
    *,
    provider: Any,
    client: BitableClient,
    table_id: str,
    sample_ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    stale_hours: float = DEFAULT_STALE_HOURS,
    force: bool = False,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    provider_name: str = "",
) -> Dict[str, Any]:
    """Plan and optionally apply daily logistics updates.

    Eligible rows have a non-empty tracking number and a sample status of
    ``已寄出`` or ``异常``.  Identical carrier/number pairs are queried once.
    Dry-run is deliberately side-effect free: it performs no provider call,
    does not register shipments and never calls a Feishu write endpoint.
    """
    if provider is None:
        raise ValueError("物流 provider 不能为空")
    if not table_id:
        raise ValueError("样品跟踪表 table_id 不能为空")
    if limit is not None and limit < 0:
        raise ValueError("limit 不能小于 0")
    if stale_hours < 0:
        raise ValueError("stale_hours 不能小于 0")

    current_time = _as_utc(now or datetime.now(timezone.utc))
    current_ms = _datetime_ms(current_time)
    requested_sample_ids = {
        str(value).strip() for value in (sample_ids or []) if str(value).strip()
    }
    normalized_provider_name = str(
        provider_name
        or getattr(provider, "provider_name", "")
        or "17track"
    ).strip().lower()

    records = client.get_all_records(table_id)
    selected_records = [
        record
        for record in records
        if not requested_sample_ids
        or field_to_text((record.get("fields") or {}).get(SAMPLE_RECORD_ID_FIELD))
        in requested_sample_ids
    ]

    candidates: List[Dict[str, Any]] = []
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    skipped_counts: Counter = Counter()

    for record in selected_records:
        record_id = str(record.get("record_id") or "").strip()
        fields = record.get("fields") or {}
        sample_id = field_to_text(fields.get(SAMPLE_RECORD_ID_FIELD))
        sample_status = field_to_text(fields.get(SAMPLE_STATUS_FIELD))
        tracking_number = field_to_text(fields.get(TRACKING_NUMBER_FIELD)).strip()
        carrier_code = field_to_text(fields.get(CARRIER_CODE_FIELD)).strip()

        if not record_id:
            skipped_counts["missing_record_id"] += 1
            continue
        # Existing signed rows may need the new parcel-bound delivery field
        # backfilled. Cancellation still remains an absolute terminal state.
        delivery_backfill = (
            sample_status == "已签收"
            and _field_datetime(fields.get(PROVIDER_DELIVERED_AT_FIELD)) is None
        )
        if sample_status not in ELIGIBLE_SAMPLE_STATUSES and not delivery_backfill:
            skipped_counts["ineligible_status"] += 1
            continue
        if not tracking_number:
            skipped_counts["empty_tracking_number"] += 1
            continue

        shipment_key = _shipment_key(
            normalized_provider_name,
            carrier_code,
            tracking_number,
        )
        identity_changed = (
            field_to_text(fields.get(SYNCED_SHIPMENT_KEY_FIELD)) != shipment_key
        )
        retrying_failure = bool(field_to_text(fields.get(QUERY_ERROR_FIELD)))
        fresh_hours = FAILED_QUERY_RETRY_HOURS if retrying_failure else stale_hours
        attempted_key = field_to_text(fields.get(LAST_QUERY_SHIPMENT_KEY_FIELD)) or field_to_text(
            fields.get(SYNCED_SHIPMENT_KEY_FIELD)
        )
        if (
            not force
            and attempted_key == shipment_key
            and (retrying_failure or not identity_changed)
            and _is_fresh(fields.get(LAST_QUERIED_AT_FIELD), current_time, stale_hours=fresh_hours)
        ):
            skipped_counts["retry_backoff" if retrying_failure else "fresh"] += 1
            continue
        if limit is not None and len(candidates) >= limit:
            skipped_counts["limit"] += 1
            continue

        snapshot = {
            "record_id": record_id,
            "sample_id": sample_id,
            "fields": fields,
            "tracking_number": tracking_number,
            "carrier_code": carrier_code,
            "shipment_key": shipment_key,
            "identity_changed": identity_changed,
            "signature": _source_signature(fields),
        }
        candidates.append(snapshot)
        groups[_group_key(carrier_code, tracking_number)].append(snapshot)

    registration_groups = sum(
        1
        for snapshots in groups.values()
        if not any(
            field_to_text(item["fields"].get(SYNCED_SHIPMENT_KEY_FIELD))
            == item["shipment_key"]
            for item in snapshots
        )
    )

    if dry_run:
        return _result_payload(
            records=records,
            selected_records=selected_records,
            candidates=candidates,
            groups=groups,
            plans=[],
            dry_run=True,
            force=force,
            stale_hours=stale_hours,
            registration_groups=registration_groups,
            provider_requests=0,
            actual_updates=0,
            concurrent_skips=0,
            missing_before_write=0,
            write_fallback_used=False,
            write_failures=0,
            batch_write_error="",
            skipped_counts=skipped_counts,
            errors=[],
            dry_run_planned_updates=len(candidates),
            current_time=current_time,
        )

    known_requests: List[TrackingRequest] = []
    new_requests: List[TrackingRequest] = []
    request_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for group_key, snapshots in groups.items():
        primary = snapshots[0]
        request = TrackingRequest(
            number=primary["tracking_number"],
            carrier=primary["carrier_code"] or None,
            tag=primary["sample_id"],
        )
        request_groups[group_key] = snapshots
        is_known = any(
            field_to_text(item["fields"].get(SYNCED_SHIPMENT_KEY_FIELD))
            == item["shipment_key"]
            for item in snapshots
        )
        (known_requests if is_known else new_requests).append(request)

    provider_results: List[TrackingResult] = []
    errors: List[Dict[str, str]] = []
    for requests, register_first in ((known_requests, False), (new_requests, True)):
        if not requests:
            continue
        try:
            returned = provider.refresh(requests, register_first=register_first)
            provider_results.extend(returned or [])
        except Exception as exc:
            clean_error = _clean_error(
                exc,
                redactions=[request.number for request in requests],
            )
            for request in requests:
                provider_results.append(TrackingResult(
                    number=request.number,
                    carrier=str(request.carrier or ""),
                    sync_status=TrackingSyncStatus.API_ERROR,
                    error_message=clean_error or "物流 API 查询失败",
                ))

    result_by_group = _match_results_to_requests(
        [*known_requests, *new_requests],
        provider_results,
    )
    plans: List[Dict[str, Any]] = []
    for group_key, snapshots in request_groups.items():
        result = result_by_group.get(group_key)
        if result is None:
            first = snapshots[0]
            result = TrackingResult(
                number=first["tracking_number"],
                carrier=first["carrier_code"],
                sync_status=TrackingSyncStatus.API_ERROR,
                error_message="物流 API 未返回该运单结果",
            )
        for snapshot in snapshots:
            plan = _plan_result(
                snapshot,
                result,
                current_time=current_time,
                current_ms=current_ms,
                provider_name=normalized_provider_name,
            )
            plans.append(plan)
            if plan["error"]:
                errors.append({
                    "sample_id": plan["sample_id"],
                    "record_id": plan["record_id"],
                    "stage": "provider",
                    "error": plan["error"],
                })

    actual_updates = 0
    concurrent_skips = 0
    missing_before_write = 0
    write_fallback_used = False
    write_failures = 0
    batch_write_error = ""

    if plans:
        latest_records = client.batch_get_records(
            table_id,
            [plan["record_id"] for plan in plans],
        )
        latest_by_id = {
            str(record.get("record_id") or ""): record
            for record in latest_records
            if record.get("record_id")
        }
        safe_updates = []
        for plan in plans:
            latest = latest_by_id.get(plan["record_id"])
            if not latest:
                missing_before_write += 1
                skipped_counts["missing_before_write"] += 1
                continue
            latest_fields = latest.get("fields") or {}
            if _source_signature(latest_fields) != plan["signature"]:
                concurrent_skips += 1
                skipped_counts["changed_before_write"] += 1
                continue
            # Eligibility is part of the signature, but keep this explicit so
            # a future signature relaxation cannot overwrite cancelled rows.
            latest_status = field_to_text(latest_fields.get(SAMPLE_STATUS_FIELD))
            if latest_status not in ELIGIBLE_SAMPLE_STATUSES and not (
                latest_status == "已签收"
                and _field_datetime(latest_fields.get(PROVIDER_DELIVERED_AT_FIELD)) is None
            ):
                concurrent_skips += 1
                skipped_counts["ineligible_before_write"] += 1
                continue
            safe_updates.append({
                "record_id": plan["record_id"],
                "fields": validate_logistics_update_fields(plan["update_fields"]),
            })

        plans_by_record_id = {plan["record_id"]: plan for plan in plans}
        for offset in range(0, len(safe_updates), FEISHU_WRITE_BATCH_SIZE):
            write_batch = safe_updates[offset:offset + FEISHU_WRITE_BATCH_SIZE]
            try:
                actual_updates += client.batch_update_records(table_id, write_batch)
                continue
            except Exception as batch_exc:
                clean_batch_error = _clean_error(
                    batch_exc,
                    redactions=[
                        str(candidate.get("tracking_number") or "")
                        for candidate in candidates
                    ],
                )
                write_fallback_used = True
                if not batch_write_error:
                    batch_write_error = clean_batch_error
                logger.warning("样品物流批量写入失败，改为逐条重试: %s", clean_batch_error)
                errors.append({
                    "sample_id": "",
                    "record_id": "",
                    "stage": "feishu_batch_write",
                    "error": clean_batch_error,
                })
            for update in write_batch:
                try:
                    client.update_record(table_id, update["record_id"], update["fields"])
                    actual_updates += 1
                except Exception as item_exc:
                    write_failures += 1
                    plan = plans_by_record_id.get(update["record_id"], {})
                    errors.append({
                        "sample_id": str(plan.get("sample_id") or ""),
                        "record_id": update["record_id"],
                        "stage": "feishu_write",
                        "error": _clean_error(
                            item_exc,
                            redactions=[str(plan.get("tracking_number") or "")],
                        ),
                    })

    return _result_payload(
        records=records,
        selected_records=selected_records,
        candidates=candidates,
        groups=groups,
        plans=plans,
        dry_run=False,
        force=force,
        stale_hours=stale_hours,
        registration_groups=registration_groups,
        provider_requests=len(groups),
        actual_updates=actual_updates,
        concurrent_skips=concurrent_skips,
        missing_before_write=missing_before_write,
        write_fallback_used=write_fallback_used,
        write_failures=write_failures,
        batch_write_error=batch_write_error,
        skipped_counts=skipped_counts,
        errors=errors,
        current_time=current_time,
    )


def _plan_result(
    snapshot: Mapping[str, Any],
    result: TrackingResult,
    *,
    current_time: datetime,
    current_ms: int,
    provider_name: str,
) -> Dict[str, Any]:
    existing = snapshot["fields"]
    identity_changed = bool(snapshot["identity_changed"])
    sync_status = _enum_value(result.sync_status)
    logistics_status = _map_logistics_status(result.latest_status)
    result_carrier = str(result.carrier or snapshot["carrier_code"] or "").strip()
    accepted = sync_status in _ACCEPTED_REGISTRATION_OUTCOMES

    update_fields: Dict[str, Any] = {
        LAST_QUERIED_AT_FIELD: current_ms,
        LAST_QUERY_SHIPMENT_KEY_FIELD: _shipment_key(
            provider_name, result_carrier, snapshot["tracking_number"]
        ),
    }
    if identity_changed:
        update_fields.update(_identity_reset_fields())

    existing_delivery = _field_datetime(existing.get(DELIVERED_DATE_FIELD))
    if existing_delivery is not None and (
        identity_changed or not field_to_text(existing.get(DELIVERED_DATE_REVIEW_FIELD))
    ):
        update_fields[DELIVERED_DATE_REVIEW_FIELD] = "已有签收日期待人工确认"
    elif (
        existing_delivery is not None
        and field_to_text(existing.get(DELIVERED_DATE_REVIEW_FIELD)) == "物流填写"
        and existing_delivery != _field_datetime(existing.get(PROVIDER_DELIVERED_AT_FIELD))
    ):
        update_fields[DELIVERED_DATE_REVIEW_FIELD] = "签收日期已人工调整"

    if result_carrier and result_carrier != snapshot["carrier_code"]:
        update_fields[CARRIER_CODE_FIELD] = result_carrier

    if accepted:
        update_fields[SYNCED_SHIPMENT_KEY_FIELD] = _shipment_key(
            provider_name,
            result_carrier,
            snapshot["tracking_number"],
        )

    error = _result_error(result)
    update_fields[QUERY_ERROR_FIELD] = error

    if result.source_sync_succeeded and result.track_time is not None:
        update_fields[SOURCE_SYNCED_AT_FIELD] = _datetime_ms(result.track_time)

    event = result.latest_event
    event_ms = _event_time_ms(event)
    progress = _event_progress(event)
    existing_status = field_to_text(existing.get(LOGISTICS_STATUS_FIELD))
    existing_progress = field_to_text(existing.get(LATEST_PROGRESS_FIELD))
    existing_event_ms = _field_timestamp_ms(existing.get(LATEST_EVENT_AT_FIELD))

    usable_tracking_data = sync_status in {
        TrackingSyncStatus.SUCCESS.value,
        TrackingSyncStatus.REGISTERED.value,
        TrackingSyncStatus.NO_TRACK_INFO.value,
    }
    if usable_tracking_data:
        update_fields[LOGISTICS_STATUS_FIELD] = logistics_status
        event_can_update = event is not None and (
            identity_changed
            or event_ms is None
            or existing_event_ms is None
            or event_ms >= existing_event_ms
        )
        if event_can_update:
            update_fields[LATEST_PROGRESS_FIELD] = progress
            if event_ms is not None:
                update_fields[LATEST_EVENT_AT_FIELD] = event_ms

        progress_changed = (
            logistics_status != existing_status
            or (event_can_update and progress != existing_progress)
            or (
                event_ms is not None
                and (existing_event_ms is None or event_ms > existing_event_ms)
            )
        )
        if identity_changed or progress_changed:
            reset_at = _progress_reset_time(
                event=event,
                track_time=result.track_time,
                now=current_time,
            )
            # A carrier can publish a changed description/status while keeping
            # the same event timestamp (or omitting/regressing it).  In that
            # case the only reliable start of the new no-progress interval is
            # when we detected the change, not the reused provider timestamp.
            if (
                not identity_changed
                and progress_changed
                and (
                    event_ms is None
                    or (
                        existing_event_ms is not None
                        and event_ms <= existing_event_ms
                    )
                )
            ):
                reset_at = current_time
            if event is None and logistics_status in {"待查询", "暂无轨迹"}:
                reset_at = _field_datetime(existing.get(SHIPPED_DATE_FIELD)) or current_time
            update_fields[STAGNATION_STARTED_AT_FIELD] = _datetime_ms(reset_at)
        elif _field_datetime(existing.get(STAGNATION_STARTED_AT_FIELD)) is None:
            fallback = (
                _field_datetime(existing.get(LATEST_EVENT_AT_FIELD))
                or _field_datetime(existing.get(SHIPPED_DATE_FIELD))
                or current_time
            )
            update_fields[STAGNATION_STARTED_AT_FIELD] = _datetime_ms(fallback)

    delivered = (
        sync_status == TrackingSyncStatus.SUCCESS.value
        and _enum_value(result.latest_status) == TrackingStatus.DELIVERED.value
    )
    if delivered:
        update_fields[SAMPLE_STATUS_FIELD] = "已签收"
        # A delivery reminder needs an actual delivery event, not the time at
        # which a cached response was fetched. Unknown timestamps require
        # confirmation rather than inventing a delivery date.
        delivered_at = event.event_time if event is not None else None
        if delivered_at is not None:
            update_fields[PROVIDER_DELIVERED_AT_FIELD] = _datetime_ms(delivered_at)
            if existing_delivery is None:
                update_fields[DELIVERED_DATE_FIELD] = _datetime_ms(delivered_at)
                update_fields[DELIVERED_DATE_REVIEW_FIELD] = "物流填写"
        elif identity_changed or _field_datetime(existing.get(PROVIDER_DELIVERED_AT_FIELD)) is None:
            update_fields[DELIVERED_DATE_REVIEW_FIELD] = "物流签收时间缺失待人工确认"

    return {
        "record_id": snapshot["record_id"],
        "sample_id": snapshot["sample_id"],
        "tracking_number": snapshot["tracking_number"],
        "signature": snapshot["signature"],
        "status": logistics_status,
        "sync_status": sync_status,
        "error": error,
        "delivered": delivered,
        "update_fields": validate_logistics_update_fields(update_fields),
    }


def _identity_reset_fields() -> Dict[str, Any]:
    """Clear every derived value that may belong to the previous parcel."""
    return {
        SYNCED_SHIPMENT_KEY_FIELD: "",
        LOGISTICS_STATUS_FIELD: "待查询",
        LATEST_PROGRESS_FIELD: "",
        LATEST_EVENT_AT_FIELD: None,
        STAGNATION_STARTED_AT_FIELD: None,
        SOURCE_SYNCED_AT_FIELD: None,
        QUERY_ERROR_FIELD: "",
        REMINDER_TYPE_FIELD: None,
        REMINDER_STATUS_FIELD: None,
        REMINDER_EVENT_KEY_FIELD: "",
        REMINDER_SENT_AT_FIELD: None,
        REMINDER_ERROR_FIELD: "",
        PROVIDER_DELIVERED_AT_FIELD: None,
        DELIVERED_DATE_REVIEW_FIELD: "",
    }


def _result_payload(
    *,
    records: Sequence[Mapping[str, Any]],
    selected_records: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    groups: Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]],
    plans: Sequence[Mapping[str, Any]],
    dry_run: bool,
    force: bool,
    stale_hours: float,
    registration_groups: int,
    provider_requests: int,
    actual_updates: int,
    concurrent_skips: int,
    missing_before_write: int,
    write_fallback_used: bool,
    write_failures: int,
    batch_write_error: str,
    skipped_counts: Counter,
    errors: Sequence[Mapping[str, str]],
    dry_run_planned_updates: Optional[int] = None,
    current_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    status_counts = Counter(str(plan.get("status") or "") for plan in plans)
    sync_status_counts = Counter(str(plan.get("sync_status") or "") for plan in plans)
    delivery_events = [
        str(plan.get("sample_id") or "")
        for plan in plans
        if plan.get("delivered")
    ]
    current_time = _as_utc(current_time or datetime.now(timezone.utc))
    stagnant_candidates = []
    for plan in plans:
        if plan.get("delivered"):
            continue
        fields = dict(
            next(
                (
                    candidate.get("fields") or {}
                    for candidate in candidates
                    if candidate.get("record_id") == plan.get("record_id")
                ),
                {},
            )
        )
        fields.update(plan.get("update_fields") or {})
        started_at = _field_datetime(fields.get(STAGNATION_STARTED_AT_FIELD))
        if started_at and current_time - started_at >= timedelta(hours=STAGNATION_HOURS):
            stagnant_candidates.append(str(plan.get("sample_id") or ""))

    planned_updates = (
        dry_run_planned_updates
        if dry_run_planned_updates is not None
        else len(plans)
    )
    return {
        "source_records": len(records),
        "selected_records": len(selected_records),
        "candidate_records": len(candidates),
        "unique_shipments": len(groups),
        "registration_shipments": registration_groups,
        "provider_requests": provider_requests,
        "planned_updates": planned_updates,
        "actual_updates": actual_updates,
        "dry_run": dry_run,
        "force": force,
        "stale_hours": stale_hours,
        "concurrent_skips": concurrent_skips,
        "missing_before_write": missing_before_write,
        "write_fallback_used": write_fallback_used,
        "write_failures": write_failures,
        "batch_write_error": batch_write_error,
        "status_counts": {
            status: status_counts.get(status, 0)
            for status in LOGISTICS_STATUS_OPTIONS
        },
        "sync_status_counts": dict(sorted(sync_status_counts.items())),
        "skipped_counts": dict(sorted(skipped_counts.items())),
        "delivery_events": delivery_events,
        "stagnant_candidates": stagnant_candidates,
        "errors": [dict(error) for error in errors],
        "samples": [
            {
                "sample_id": str(plan.get("sample_id") or ""),
                "record_id": str(plan.get("record_id") or ""),
                "status": str(plan.get("status") or ""),
                "sync_status": str(plan.get("sync_status") or ""),
                "updated_field_names": sorted((plan.get("update_fields") or {}).keys()),
            }
            for plan in plans[:10]
        ],
    }


def _match_results_to_requests(
    requests: Sequence[TrackingRequest],
    results: Sequence[TrackingResult],
) -> Dict[Tuple[str, str], TrackingResult]:
    """Match normalized results even when auto-detection fills the carrier."""
    unmatched = list(results)
    matched: Dict[Tuple[str, str], TrackingResult] = {}
    for request in requests:
        request_key = _group_key(str(request.carrier or ""), request.number)
        chosen_index: Optional[int] = None
        for index, result in enumerate(unmatched):
            if _normalize_number(result.number) != _normalize_number(request.number):
                continue
            result_carrier = _normalize_carrier(result.carrier)
            request_carrier = _normalize_carrier(request.carrier)
            if request_carrier and result_carrier and request_carrier != result_carrier:
                continue
            chosen_index = index
            break
        if chosen_index is not None:
            matched[request_key] = unmatched.pop(chosen_index)
    return matched


def _group_key(carrier_code: Any, tracking_number: Any) -> Tuple[str, str]:
    return _normalize_carrier(carrier_code), _normalize_number(tracking_number)


def _map_logistics_status(value: Any) -> str:
    return _LOGISTICS_STATUS_MAP.get(_enum_value(value), "未知")


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _result_error(result: TrackingResult) -> str:
    sync_status = _enum_value(result.sync_status)
    label = _SYNC_ERROR_LABELS.get(sync_status, "")
    details = " ".join(
        part for part in (
            str(result.error_code or "").strip(),
            str(result.error_message or "").strip(),
        ) if part
    )
    if not label and not details:
        return ""
    if label and details:
        return _clean_error(
            f"{label}：{details}",
            redactions=[result.number],
        )
    return _clean_error(label or details, redactions=[result.number])


def _event_progress(event: Optional[TrackingEvent]) -> str:
    if event is None:
        return ""
    location = " ".join(str(event.location or "").split())
    description = " ".join(str(event.description or "").split())
    if location and description:
        return f"{location}｜{description}"
    return location or description or " ".join(str(event.sub_status or "").split())


def _event_time_ms(event: Optional[TrackingEvent]) -> Optional[int]:
    if event is None or event.event_time is None:
        return None
    return _datetime_ms(event.event_time)


def _progress_reset_time(
    *,
    event: Optional[TrackingEvent],
    track_time: Optional[datetime],
    now: datetime,
) -> datetime:
    value = (event.event_time if event is not None else None) or track_time or now
    normalized = _as_utc(value)
    return min(normalized, now)


def _source_signature(fields: Mapping[str, Any]) -> Tuple[str, ...]:
    """Fields whose concurrent edits make a planned logistics write unsafe."""
    return (
        field_to_text(fields.get(TRACKING_NUMBER_FIELD)),
        field_to_text(fields.get(CARRIER_CODE_FIELD)),
        field_to_text(fields.get(CARRIER_FIELD)),
        field_to_text(fields.get(SAMPLE_STATUS_FIELD)),
        field_to_text(fields.get(SHIPPED_DATE_FIELD)),
        field_to_text(fields.get(DELIVERED_DATE_FIELD)),
        field_to_text(fields.get(SYNCED_SHIPMENT_KEY_FIELD)),
        field_to_text(fields.get(LOGISTICS_STATUS_FIELD)),
        field_to_text(fields.get(LATEST_EVENT_AT_FIELD)),
        field_to_text(fields.get(LATEST_PROGRESS_FIELD)),
        field_to_text(fields.get(LAST_QUERIED_AT_FIELD)),
        field_to_text(fields.get(LAST_QUERY_SHIPMENT_KEY_FIELD)),
        field_to_text(fields.get(PROVIDER_DELIVERED_AT_FIELD)),
        field_to_text(fields.get(DELIVERED_DATE_REVIEW_FIELD)),
    )


def _is_fresh(value: Any, now: datetime, *, stale_hours: float) -> bool:
    queried_at = _field_datetime(value)
    if queried_at is None:
        return False
    return max(0.0, (now - queried_at).total_seconds() / 3600) < stale_hours


def _field_timestamp_ms(value: Any) -> Optional[int]:
    parsed = _field_datetime(value)
    return _datetime_ms(parsed) if parsed is not None else None


def _field_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    raw = value
    if isinstance(value, (list, dict)):
        raw = field_to_text(value)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        numeric = float(raw)
        if numeric > 10_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return _field_datetime(int(text))
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_ms(value: datetime) -> int:
    return int(_as_utc(value).timestamp() * 1000)


def _clean_error(value: Any, *, redactions: Iterable[str] = ()) -> str:
    text = " ".join(str(value or "").split())
    for secret in sorted(
        {str(item).strip() for item in redactions if str(item).strip()},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "<tracking-number redacted>")
    text = re.sub(
        r"(?i)\b(?:17token|api[_-]?key|authorization)\s*[:=]\s*[^\s,;}]+",
        "<credential redacted>",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s,;}]+", "Bearer <redacted>", text)
    text = re.sub(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "<email redacted>",
        text,
    )
    return text[:MAX_ERROR_LENGTH]
