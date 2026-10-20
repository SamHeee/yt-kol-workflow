#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plan and dispatch the daily sample-logistics reminder digest.

Delivery reminders become due on the next Beijing calendar day.  Stagnation
reminders become due after a continuous 72-hour period without a new logistics
event.  Each condition has a deterministic event key, so a delivered parcel or
one unchanged logistics node can produce at most one confirmed reminder.

The dispatcher uses a small outbox state machine in the sample table:

``待发送/发送失败 -> 发送中 -> 已发送``

An indeterminate CLI outcome becomes ``待人工确认`` and is never retried
automatically.  This is intentionally conservative: a timeout can occur after
Feishu has accepted the message.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

from feishu.bitable import BitableClient
from feishu.sample_logistics import (
    CARRIER_CODE_FIELD,
    CARRIER_FIELD,
    CONTACT_EMAIL_FIELD,
    PROVIDER_DELIVERED_AT_FIELD,
    ELIGIBLE_SAMPLE_STATUSES,
    KOL_NAME_FIELD,
    LAST_QUERIED_AT_FIELD,
    LATEST_EVENT_AT_FIELD,
    LATEST_PROGRESS_FIELD,
    LOGISTICS_STATUS_FIELD,
    QUERY_ERROR_FIELD,
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
    validate_reminder_update_fields,
)
from feishu.value_utils import field_to_text
from logistics.shipment_identity import shipment_is_current
from notifications.feishu_cli import (
    FeishuCliAmbiguousDeliveryError,
    FeishuCliNotificationError,
    FeishuCliNotifier,
)


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
UTC = timezone.utc
DELIVERY_REMINDER = "送达次日"
STAGNATION_REMINDER = "三天无进度"
DELIVERED_STATUS = "已签收"
REMINDER_PENDING = "待发送"
REMINDER_SENDING = "发送中"
REMINDER_SENT = "已发送"
REMINDER_FAILED = "发送失败"
REMINDER_MANUAL = "待人工确认"
MAX_REMINDER_ERROR_LENGTH = 500
MAX_DYNAMIC_TEXT_LENGTH = 180
REMINDER_LEASE_PREFIX = "__KOL_REMINDER_LEASE_V1__:"
REMINDER_LEASE_TTL = timedelta(minutes=30)
REMINDER_LEASE_FUTURE_SKEW = timedelta(minutes=5)


class SampleReminderError(RuntimeError):
    """Base error raised by reminder planning or delivery."""


class ReminderStateError(SampleReminderError):
    """The table outbox state could not be updated safely."""


class ReminderDeliveryAmbiguousError(SampleReminderError):
    """The message may have been delivered and needs manual confirmation."""


@dataclass(frozen=True)
class ReminderCandidate:
    record_id: str
    sample_record_id: str
    reminder_type: str
    event_key: str
    kol_name: str
    contact_email: str
    carrier: str
    tracking_number: str
    logistics_status: str
    progress: str
    event_at: datetime
    elapsed_hours: float
    no_trajectory: bool = False


@dataclass(frozen=True)
class ReminderSendingLease:
    """Persisted attempt metadata for one member of an outbound digest."""

    attempted_at: datetime
    idempotency_key: str
    event_count: int


@dataclass(frozen=True)
class ReminderLeaseGroup:
    attempted_at: datetime
    idempotency_key: str
    candidates: Tuple[ReminderCandidate, ...]


def dispatch_sample_reminders(
    *,
    client: BitableClient,
    table_id: str,
    notifier: FeishuCliNotifier,
    recipient_user_id: str = "",
    base_url: str = "",
    sample_ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    stagnation_hours: float = 72,
    source_fresh_hours: float = 36,
) -> Dict[str, Any]:
    """Send at most one daily interactive-card digest and update its outbox rows.

    ``dry_run`` performs reads and planning only: it does not update the Base
    and never invokes the notifier.  The result deliberately excludes the
    rendered message because it contains contact email addresses and may be
    persisted as a statistics artifact by the command-line entry point.
    """
    if not table_id or not str(table_id).strip():
        raise ValueError("sample tracking table_id cannot be empty")
    if stagnation_hours <= 0:
        raise ValueError("stagnation_hours must be greater than zero")
    if source_fresh_hours <= 0:
        raise ValueError("source_fresh_hours must be greater than zero")
    if limit is not None and limit < 0:
        raise ValueError("limit cannot be less than zero")
    if not dry_run and not str(recipient_user_id or "").strip():
        raise ValueError("SAMPLE_REMINDER_USER_OPEN_ID cannot be empty")

    current_time = _as_utc(now or datetime.now(UTC))
    all_records = client.get_all_records(table_id)
    requested_sample_ids = {
        str(value).strip()
        for value in (sample_ids or [])
        if str(value).strip()
    }
    records = all_records
    if requested_sample_ids:
        records = [
            record
            for record in records
            if field_to_text(
                (record.get("fields") or {}).get(SAMPLE_RECORD_ID_FIELD)
            ) in requested_sample_ids
        ]
    # Dry runs deliberately leave the outbox untouched.  In a real dispatch,
    # resolve an existing send lease before planning any new digest.  This is
    # what makes a process crash between the lease write and result write
    # recoverable without changing the Feishu idempotency key.
    if dry_run:
        candidates, skipped = plan_sample_reminders(
            records,
            now=current_time,
            stagnation_hours=stagnation_hours,
            source_fresh_hours=source_fresh_hours,
        )
        if limit is not None and len(candidates) > limit:
            skipped["limit"] += len(candidates) - limit
            candidates = candidates[:limit]
        return _base_result(candidates, skipped, dry_run=True)

    stale_groups, recent_groups, invalid_lease_records = _classify_sending_leases(
        all_records,
        now=current_time,
        stagnation_hours=stagnation_hours,
        source_fresh_hours=source_fresh_hours,
    )
    skipped: Counter = Counter()
    if invalid_lease_records:
        _mark_invalid_leases_for_manual_confirmation(
            client,
            table_id,
            invalid_lease_records,
        )
        skipped["invalid_sending_lease_manual_confirmation"] += len(
            invalid_lease_records
        )

    # Any recent group may still be owned by another process.  Do not start a
    # second digest (even for newly due records) while that attempt is active.
    if recent_groups:
        skipped["sending_lease_in_progress"] += sum(
            len(group.candidates) for group in recent_groups
        )
        skipped["dispatch_deferred_by_active_lease"] += 1
        return _base_result([], skipped, dry_run=False)

    recovering_group: Optional[ReminderLeaseGroup] = None
    if stale_groups:
        # Complete exactly one old outbound transaction per invocation.  New
        # candidates and other stale groups must never alter this digest key.
        stale_groups.sort(key=lambda group: (group.attempted_at, group.idempotency_key))
        recovering_group = stale_groups[0]
        skipped["stale_sending_lease_recovered"] += len(
            recovering_group.candidates
        )
        if len(stale_groups) > 1:
            skipped["stale_sending_lease_groups_deferred"] += len(stale_groups) - 1
        candidates = list(recovering_group.candidates)
        idempotency_key = recovering_group.idempotency_key
    else:
        candidates, planned_skipped = plan_sample_reminders(
            records,
            now=current_time,
            stagnation_hours=stagnation_hours,
            source_fresh_hours=source_fresh_hours,
        )
        skipped.update(planned_skipped)
        if limit is not None and len(candidates) > limit:
            skipped["limit"] += len(candidates) - limit
            candidates = candidates[:limit]
        if not candidates:
            return _base_result([], skipped, dry_run=False)
        idempotency_key = build_digest_idempotency_key(candidates)

    try:
        notifier.assert_current_user(str(recipient_user_id).strip())
    except FeishuCliNotificationError as exc:
        raise SampleReminderError(_clean_error(str(exc))) from exc

    # A last read narrows the race window with an operator changing the parcel,
    # reminder status, or lease while the daily logistics refresh is finishing.
    latest_records = client.batch_get_records(
        table_id,
        [candidate.record_id for candidate in candidates],
    )
    if recovering_group is not None:
        latest_stale, latest_recent, latest_invalid = _classify_sending_leases(
            latest_records,
            now=current_time,
            stagnation_hours=stagnation_hours,
            source_fresh_hours=source_fresh_hours,
        )
        matching_recent = [
            group
            for group in latest_recent
            if group.idempotency_key == idempotency_key
        ]
        if matching_recent:
            skipped["sending_lease_in_progress"] += len(
                matching_recent[0].candidates
            )
            return _base_result([], skipped, dry_run=False)
        matching_stale = [
            group
            for group in latest_stale
            if group.idempotency_key == idempotency_key
        ]
        if latest_invalid or len(matching_stale) != 1:
            unsafe_records = [
                record
                for record in latest_records
                if field_to_text(
                    (record.get("fields") or {}).get(REMINDER_STATUS_FIELD)
                ) == REMINDER_SENDING
            ]
            if unsafe_records:
                _mark_invalid_leases_for_manual_confirmation(
                    client,
                    table_id,
                    unsafe_records,
                )
                skipped["lease_changed_before_recovery_manual_confirmation"] += len(
                    unsafe_records
                )
            return _base_result([], skipped, dry_run=False)
        safe_candidates = list(matching_stale[0].candidates)
    else:
        latest_candidates, latest_skipped = plan_sample_reminders(
            latest_records,
            now=current_time,
            stagnation_hours=stagnation_hours,
            source_fresh_hours=source_fresh_hours,
        )
        latest_by_key = {
            candidate.event_key: candidate for candidate in latest_candidates
        }
        safe_candidates = [
            latest_by_key[candidate.event_key]
            for candidate in candidates
            if candidate.event_key in latest_by_key
        ]
        skipped.update(latest_skipped)
        skipped["changed_before_send"] += len(candidates) - len(safe_candidates)
        if not safe_candidates:
            return _base_result([], skipped, dry_run=False)
        # The final read can remove candidates, so the new transaction key is
        # derived only after the exact lease group is known.
        idempotency_key = build_digest_idempotency_key(safe_candidates)

    sending_lease = ReminderSendingLease(
        attempted_at=current_time,
        idempotency_key=idempotency_key,
        event_count=len(safe_candidates),
    )
    encoded_lease = _encode_sending_lease(sending_lease)
    sending_updates = [
        {
            "record_id": candidate.record_id,
            "fields": validate_reminder_update_fields(
                {
                    REMINDER_TYPE_FIELD: candidate.reminder_type,
                    REMINDER_STATUS_FIELD: REMINDER_SENDING,
                    REMINDER_EVENT_KEY_FIELD: candidate.event_key,
                    REMINDER_SENT_AT_FIELD: None,
                    REMINDER_ERROR_FIELD: encoded_lease,
                }
            ),
        }
        for candidate in safe_candidates
    ]
    try:
        marked = client.batch_update_records(table_id, sending_updates)
    except Exception as exc:
        raise ReminderStateError(
            "未能在飞书表内预占提醒事件；消息未发送"
        ) from exc
    if marked != len(sending_updates):
        raise ReminderStateError(
            f"提醒发送前仅有 {marked}/{len(sending_updates)} 条记录进入发送中；消息未发送"
        )

    card = build_daily_reminder_card(
        safe_candidates,
        now=current_time,
        base_url=base_url,
        table_id=table_id,
    )
    card_content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    try:
        receipt = notifier.send_card(
            user_id=str(recipient_user_id).strip(),
            card=card,
            idempotency_key=idempotency_key,
        )
    except FeishuCliAmbiguousDeliveryError as exc:
        error = _clean_error(
            str(exc),
            candidates=safe_candidates,
            message_content=card_content,
        )
        _persist_outcome(
            client,
            table_id,
            safe_candidates,
            status=REMINDER_MANUAL,
            error=error,
            sent_at=None,
        )
        raise ReminderDeliveryAmbiguousError(error) from None
    except FeishuCliNotificationError as exc:
        error = _clean_error(
            str(exc),
            candidates=safe_candidates,
            message_content=card_content,
        )
        _persist_outcome(
            client,
            table_id,
            safe_candidates,
            status=REMINDER_FAILED,
            error=error,
            sent_at=None,
        )
        raise SampleReminderError(error) from None
    except Exception as exc:
        # Unknown notifier exceptions are ambiguous by definition; treating
        # them as retryable could duplicate a message under a new digest key.
        error = _clean_error(
            f"提醒发送结果未知，需要人工确认：{exc.__class__.__name__}"
        )
        _persist_outcome(
            client,
            table_id,
            safe_candidates,
            status=REMINDER_MANUAL,
            error=error,
            sent_at=None,
        )
        raise ReminderDeliveryAmbiguousError(error) from None

    sent_timestamp_ms = int(current_time.timestamp() * 1000)
    try:
        _persist_outcome(
            client,
            table_id,
            safe_candidates,
            status=REMINDER_SENT,
            error="",
            sent_at=sent_timestamp_ms,
        )
    except ReminderStateError as exc:
        raise ReminderStateError(
            "飞书提醒已发送，但表内发送结果未能确认；记录保持发送中，请人工核对后再处理"
        ) from exc

    result = _base_result(safe_candidates, skipped, dry_run=False)
    result.update(
        {
            "sent": len(safe_candidates),
            "messages_sent": 1,
            "message_id": getattr(receipt, "message_id", "") or "",
            "idempotency_key": idempotency_key,
        }
    )
    return result


def plan_sample_reminders(
    records: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    stagnation_hours: float = 72,
    source_fresh_hours: float = 36,
) -> Tuple[List[ReminderCandidate], Counter]:
    """Return due reminder events and skip-reason counts without side effects."""
    current_time = _as_utc(now)
    candidates: List[ReminderCandidate] = []
    skipped: Counter = Counter()
    for record in records:
        candidate, reason = _candidate_for_record(
            record,
            now=current_time,
            stagnation_hours=stagnation_hours,
            source_fresh_hours=source_fresh_hours,
        )
        if candidate is None:
            skipped[reason or "not_due"] += 1
            continue
        fields = record.get("fields") or {}
        existing_key = field_to_text(fields.get(REMINDER_EVENT_KEY_FIELD))
        existing_status = field_to_text(fields.get(REMINDER_STATUS_FIELD))
        if (
            candidate.reminder_type == DELIVERY_REMINDER
            and field_to_text(fields.get(REMINDER_TYPE_FIELD)) == DELIVERY_REMINDER
            and existing_status == REMINDER_SENT
        ):
            # A verified delivery-time correction (including migration from
            # a historical manual date) is still the same parcel's delivery.
            # A changed parcel is excluded above until refresh resets state.
            skipped["already_sent"] += 1
            continue
        if existing_key == candidate.event_key:
            if existing_status == REMINDER_SENT:
                skipped["already_sent"] += 1
                continue
            if existing_status == REMINDER_SENDING:
                skipped["sending_in_progress"] += 1
                continue
            if existing_status == REMINDER_MANUAL:
                skipped["manual_confirmation_required"] += 1
                continue
            # 待发送 and explicit 发送失败 are safe to attempt with the same
            # deterministic idempotency key.
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (0 if item.reminder_type == DELIVERY_REMINDER else 1,
                          item.event_at, item.sample_record_id, item.record_id)
    )
    return candidates, skipped


def build_digest_idempotency_key(candidates: Sequence[ReminderCandidate]) -> str:
    """Build the <=50-character key required by ``+messages-send``."""
    if not candidates:
        raise ValueError("cannot build an idempotency key for an empty digest")
    material = "\n".join(sorted(candidate.event_key for candidate in candidates))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    key = f"sample-rem-{digest}"
    if len(key) > 50:  # defensive guard against future prefix changes
        raise AssertionError("generated idempotency key exceeds 50 characters")
    return key


def build_daily_reminder_card(
    candidates: Sequence[ReminderCandidate],
    *,
    now: datetime,
    base_url: str = "",
    table_id: str = "",
) -> Dict[str, Any]:
    """Render one Feishu interactive card with delivery and stagnation sections."""
    if not candidates:
        raise ValueError("cannot render an empty reminder card")
    local_day = _as_utc(now).astimezone(BEIJING_TZ).date().isoformat()
    deliveries = [item for item in candidates if item.reminder_type == DELIVERY_REMINDER]
    stagnations = [item for item in candidates if item.reminder_type == STAGNATION_REMINDER]
    elements: List[Dict[str, Any]] = []
    _append_card_section(elements, "已送达待邮件确认", deliveries)
    _append_card_section(elements, "连续 72 小时无进展", stagnations)

    table_url = _safe_sample_table_url(base_url, table_id)
    elements.append({"tag": "hr"})
    if table_url:
        elements.extend(
            [
                {
                    "tag": "button",
                    "element_id": "open_sample_table",
                    "text": {
                        "tag": "plain_text",
                        "content": "查看样品跟踪表",
                    },
                    "type": "primary",
                    "width": "default",
                    "size": "medium",
                    "behaviors": [
                        {
                            "type": "open_url",
                            "default_url": table_url,
                        }
                    ],
                },
                {"tag": "hr"},
            ]
        )
    elements.append(
        {
            "tag": "markdown",
            "content": "来自 **样品跟踪表**",
        }
    )
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "summary": {"content": f"样品物流每日提醒｜{local_day}"},
        },
        "header": {
            "template": "purple",
            "title": {
                "tag": "plain_text",
                "content": f"样品物流每日提醒｜{local_day}",
            },
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }


def build_daily_reminder_markdown(
    candidates: Sequence[ReminderCandidate],
    *,
    now: datetime,
    base_url: str = "",
    table_id: str = "",
) -> str:
    """Render one compact markdown digest with delivery and stagnation sections."""
    if not candidates:
        raise ValueError("cannot render an empty reminder digest")
    local_day = _as_utc(now).astimezone(BEIJING_TZ).date().isoformat()
    deliveries = [item for item in candidates if item.reminder_type == DELIVERY_REMINDER]
    stagnations = [item for item in candidates if item.reminder_type == STAGNATION_REMINDER]
    lines = [f"## 样品物流每日提醒｜{local_day}"]
    table_url = _safe_sample_table_url(base_url, table_id)
    if table_url:
        lines.extend(["", f"[打开样品跟踪表]({table_url})"])
    if deliveries:
        lines.extend(["", f"### 已送达待邮件确认（{len(deliveries)}）"])
        for item in deliveries:
            lines.extend(
                [
                    f"- **{_md(item.sample_record_id or '未编号')}**",
                    *_reminder_detail_lines(item),
                    f"  - 送达：{_format_local(item.event_at)}",
                    "  - 请向红人确认是否已收到包裹。",
                ]
            )
    if stagnations:
        lines.extend(["", f"### 连续 72 小时无进展（{len(stagnations)}）"])
        for item in stagnations:
            heading = "发货后仍无轨迹" if item.no_trajectory else "物流节点未变化"
            lines.extend(
                [
                    f"- **{_md(item.sample_record_id or '未编号')}**",
                    *_reminder_detail_lines(item),
                    f"  - {heading}：约 {int(item.elapsed_hours)} 小时｜起算：{_format_local(item.event_at)}",
                    "  - 请跟进物流商或核对运单。",
                ]
            )
    return "\n".join(lines)


def _candidate_for_record(
    record: Mapping[str, Any],
    *,
    now: datetime,
    stagnation_hours: float,
    source_fresh_hours: float,
) -> Tuple[Optional[ReminderCandidate], str]:
    record_id = str(record.get("record_id") or "").strip()
    fields = record.get("fields") or {}
    if not record_id or not isinstance(fields, Mapping):
        return None, "invalid_record"

    sample_status = field_to_text(fields.get(SAMPLE_STATUS_FIELD))
    logistics_status = field_to_text(fields.get(LOGISTICS_STATUS_FIELD))
    common = _candidate_values(record_id, fields, logistics_status)

    # Cancellation is an explicit operator decision and always wins over a
    # previously cached delivery result.  This mirrors the refresh workflow,
    # which never overwrites 已取消 with 已签收.
    if sample_status == "已取消":
        return None, "sample_status_ineligible"

    if not shipment_is_current(
        field_to_text(fields.get(CARRIER_CODE_FIELD)),
        field_to_text(fields.get(TRACKING_NUMBER_FIELD)),
        field_to_text(fields.get(SYNCED_SHIPMENT_KEY_FIELD)),
    ):
        return None, "shipment_changed_or_unsynced"

    if logistics_status == DELIVERED_STATUS:
        delivered_at = _parse_datetime(fields.get(PROVIDER_DELIVERED_AT_FIELD))
        if delivered_at is None:
            return None, "delivered_time_missing"
        delivered_day = delivered_at.astimezone(BEIJING_TZ).date()
        today = now.astimezone(BEIJING_TZ).date()
        if today <= delivered_day:
            return None, "delivery_same_day"
        event_key = _event_key(
            "delivery",
            record_id,
            common["shipment_identity"],
            delivered_day.isoformat(),
        )
        return ReminderCandidate(
            record_id=record_id,
            reminder_type=DELIVERY_REMINDER,
            event_key=event_key,
            event_at=delivered_at,
            elapsed_hours=max(0.0, (now - delivered_at).total_seconds() / 3600),
            no_trajectory=False,
            **common["display"],
        ), ""

    if sample_status not in ELIGIBLE_SAMPLE_STATUSES:
        return None, "sample_status_ineligible"
    stagnation_started = _parse_datetime(fields.get(STAGNATION_STARTED_AT_FIELD))
    if stagnation_started is None:
        stagnation_started = (
            _parse_datetime(fields.get(LATEST_EVENT_AT_FIELD))
            or _parse_datetime(fields.get(SHIPPED_DATE_FIELD))
            or _parse_datetime(fields.get(SOURCE_SYNCED_AT_FIELD))
        )
    if stagnation_started is None:
        return None, "stagnation_start_missing"
    elapsed_hours = (now - stagnation_started).total_seconds() / 3600
    if elapsed_hours < 0:
        return None, "stagnation_time_in_future"
    if elapsed_hours < stagnation_hours:
        return None, "stagnation_not_due"
    if not _provider_data_is_fresh(
        fields,
        now=now,
        source_fresh_hours=source_fresh_hours,
    ):
        return None, "provider_data_stale"

    no_trajectory = logistics_status in {"", "待查询", "暂无轨迹"}
    event_key = _event_key(
        "stagnant",
        record_id,
        common["shipment_identity"],
        stagnation_started.isoformat(),
        _stagnation_node_fingerprint(fields, logistics_status),
    )
    return ReminderCandidate(
        record_id=record_id,
        reminder_type=STAGNATION_REMINDER,
        event_key=event_key,
        event_at=stagnation_started,
        elapsed_hours=elapsed_hours,
        no_trajectory=no_trajectory,
        **common["display"],
    ), ""


def _candidate_values(
    record_id: str,
    fields: Mapping[str, Any],
    logistics_status: str,
) -> Dict[str, Any]:
    carrier = field_to_text(fields.get(CARRIER_FIELD))
    carrier_code = field_to_text(fields.get(CARRIER_CODE_FIELD))
    tracking_number = field_to_text(fields.get(TRACKING_NUMBER_FIELD))
    shipment_identity = field_to_text(fields.get(SYNCED_SHIPMENT_KEY_FIELD))
    if not shipment_identity:
        shipment_identity = f"{carrier_code or carrier}|{tracking_number}|{record_id}"
    return {
        "shipment_identity": shipment_identity,
        "display": {
            "sample_record_id": field_to_text(fields.get(SAMPLE_RECORD_ID_FIELD)),
            "kol_name": field_to_text(fields.get(KOL_NAME_FIELD)),
            "contact_email": field_to_text(fields.get(CONTACT_EMAIL_FIELD)),
            "carrier": carrier or carrier_code,
            "tracking_number": tracking_number,
            "logistics_status": logistics_status,
            "progress": field_to_text(fields.get(LATEST_PROGRESS_FIELD)),
        },
    }


def _provider_data_is_fresh(
    fields: Mapping[str, Any],
    *,
    now: datetime,
    source_fresh_hours: float,
) -> bool:
    source_synced = _parse_datetime(fields.get(SOURCE_SYNCED_AT_FIELD))
    query_error = field_to_text(fields.get(QUERY_ERROR_FIELD))
    freshness_point = source_synced
    if freshness_point is None and not query_error:
        freshness_point = _parse_datetime(fields.get(LAST_QUERIED_AT_FIELD))
    if freshness_point is None:
        return False
    age_hours = (now - freshness_point).total_seconds() / 3600
    return age_hours <= source_fresh_hours


def _encode_sending_lease(lease: ReminderSendingLease) -> str:
    payload = {
        "attempted_at": lease.attempted_at.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        ),
        "idempotency_key": lease.idempotency_key,
        "event_count": lease.event_count,
    }
    return REMINDER_LEASE_PREFIX + json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def _parse_sending_lease(value: Any) -> Optional[ReminderSendingLease]:
    text = field_to_text(value).strip()
    if not text.startswith(REMINDER_LEASE_PREFIX):
        return None
    try:
        payload = json.loads(text[len(REMINDER_LEASE_PREFIX) :])
        attempted_at = _parse_datetime(payload["attempted_at"])
        idempotency_key = str(payload["idempotency_key"])
        event_count = int(payload["event_count"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        attempted_at is None
        or re.fullmatch(r"sample-rem-[a-f0-9]{32}", idempotency_key) is None
        or event_count <= 0
    ):
        return None
    return ReminderSendingLease(
        attempted_at=attempted_at,
        idempotency_key=idempotency_key,
        event_count=event_count,
    )


def _classify_sending_leases(
    records: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    stagnation_hours: float,
    source_fresh_hours: float,
) -> Tuple[List[ReminderLeaseGroup], List[ReminderLeaseGroup], List[Mapping[str, Any]]]:
    """Validate and split complete sending groups into stale and active leases."""
    current_time = _as_utc(now)
    grouped: Dict[str, List[Tuple[Mapping[str, Any], ReminderCandidate, ReminderSendingLease]]] = {}
    invalid: List[Mapping[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        if field_to_text(fields.get(REMINDER_STATUS_FIELD)) != REMINDER_SENDING:
            continue
        lease = _parse_sending_lease(fields.get(REMINDER_ERROR_FIELD))
        candidate, _ = _candidate_for_record(
            record,
            now=current_time,
            stagnation_hours=stagnation_hours,
            source_fresh_hours=source_fresh_hours,
        )
        stored_event_key = field_to_text(fields.get(REMINDER_EVENT_KEY_FIELD))
        if (
            lease is None
            or candidate is None
            or candidate.event_key != stored_event_key
            or lease.attempted_at > current_time + REMINDER_LEASE_FUTURE_SKEW
        ):
            invalid.append(record)
            continue
        grouped.setdefault(lease.idempotency_key, []).append(
            (record, candidate, lease)
        )

    stale: List[ReminderLeaseGroup] = []
    recent: List[ReminderLeaseGroup] = []
    for key, members in grouped.items():
        leases = [member[2] for member in members]
        candidates = tuple(member[1] for member in members)
        attempted_values = {lease.attempted_at for lease in leases}
        expected_counts = {lease.event_count for lease in leases}
        valid_group = (
            len(attempted_values) == 1
            and expected_counts == {len(candidates)}
            and build_digest_idempotency_key(candidates) == key
        )
        if not valid_group:
            invalid.extend(member[0] for member in members)
            continue
        group = ReminderLeaseGroup(
            attempted_at=leases[0].attempted_at,
            idempotency_key=key,
            candidates=tuple(
                sorted(candidates, key=lambda candidate: candidate.event_key)
            ),
        )
        if current_time - group.attempted_at < REMINDER_LEASE_TTL:
            recent.append(group)
        else:
            stale.append(group)
    return stale, recent, invalid


def _mark_invalid_leases_for_manual_confirmation(
    client: BitableClient,
    table_id: str,
    records: Sequence[Mapping[str, Any]],
) -> None:
    error = "发送租约无法安全解析或租约组不完整；为避免重复发送，请人工确认"
    updates = []
    for record in records:
        record_id = str(record.get("record_id") or "").strip()
        fields = record.get("fields") or {}
        if not record_id:
            continue
        updates.append(
            {
                "record_id": record_id,
                "fields": validate_reminder_update_fields(
                    {
                        REMINDER_STATUS_FIELD: REMINDER_MANUAL,
                        REMINDER_SENT_AT_FIELD: None,
                        REMINDER_ERROR_FIELD: error,
                    }
                ),
            }
        )
    if not updates:
        return
    try:
        updated = client.batch_update_records(table_id, updates)
    except Exception as exc:
        raise ReminderStateError("无法将无效提醒租约转为待人工确认") from exc
    if updated != len(updates):
        raise ReminderStateError(
            f"无效提醒租约仅处理 {updated}/{len(updates)} 条记录"
        )


def _persist_outcome(
    client: BitableClient,
    table_id: str,
    candidates: Sequence[ReminderCandidate],
    *,
    status: str,
    error: str,
    sent_at: Optional[int],
) -> None:
    updates = [
        {
            "record_id": candidate.record_id,
            "fields": validate_reminder_update_fields(
                {
                    REMINDER_TYPE_FIELD: candidate.reminder_type,
                    REMINDER_STATUS_FIELD: status,
                    REMINDER_EVENT_KEY_FIELD: candidate.event_key,
                    REMINDER_SENT_AT_FIELD: sent_at,
                    REMINDER_ERROR_FIELD: _clean_error(error) if error else "",
                }
            ),
        }
        for candidate in candidates
    ]
    try:
        updated = client.batch_update_records(table_id, updates)
    except Exception as exc:
        raise ReminderStateError(
            f"提醒消息结果为“{status}”，但无法写回飞书表"
        ) from exc
    if updated != len(updates):
        raise ReminderStateError(
            f"提醒消息结果为“{status}”，但仅写回 {updated}/{len(updates)} 条记录"
        )


def _base_result(
    candidates: Sequence[ReminderCandidate],
    skipped: Counter,
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    type_counts = Counter(item.reminder_type for item in candidates)
    return {
        "dry_run": bool(dry_run),
        "planned_events": len(candidates),
        "planned_delivery_reminders": type_counts[DELIVERY_REMINDER],
        "planned_stagnation_reminders": type_counts[STAGNATION_REMINDER],
        "sent": 0,
        "messages_sent": 0,
        "skipped_counts": dict(sorted(skipped.items())),
        "event_keys": [candidate.event_key for candidate in candidates],
    }


def _event_key(prefix: str, *parts: str) -> str:
    material = "\x1f".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _stagnation_node_fingerprint(
    fields: Mapping[str, Any], logistics_status: str
) -> str:
    """Identify the last known node without exposing its contents in state."""
    normalized_status = (
        "no_trajectory"
        if logistics_status in {"", "待查询", "暂无轨迹"}
        else logistics_status
    )
    return _event_key(
        "node",
        normalized_status,
        field_to_text(fields.get(LATEST_EVENT_AT_FIELD)),
        field_to_text(fields.get(LATEST_PROGRESS_FIELD)),
    )


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        if not value:
            return None
        return _parse_datetime(value[0])
    if isinstance(value, Mapping):
        for name in ("timestamp", "value", "text"):
            if name in value:
                parsed = _parse_datetime(value.get(name))
                if parsed is not None:
                    return parsed
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=BEIJING_TZ)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if abs(numeric) > 10_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = field_to_text(value).strip()
        if not text:
            return None
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            try:
                return _parse_datetime(float(text))
            except ValueError:
                return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # The command's business clock is Beijing, so a caller-provided naive
        # test/operator timestamp is interpreted in that same timezone.
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(UTC)


def _format_local(value: datetime) -> str:
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")


def _append_card_section(
    elements: List[Dict[str, Any]],
    title: str,
    candidates: Sequence[ReminderCandidate],
) -> None:
    if not candidates:
        return
    if elements:
        elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "markdown",
            "content": f"**{title}（{len(candidates)}）**",
        }
    )
    for index, candidate in enumerate(candidates):
        elements.append(
            {
                "tag": "markdown",
                "content": _card_item_markdown(candidate),
            }
        )
        if index < len(candidates) - 1:
            elements.append({"tag": "hr"})


def _card_item_markdown(candidate: ReminderCandidate) -> str:
    lines = [
        f"**{_md(candidate.sample_record_id or '未编号')}**",
        "",
        f"**KOL Name：** {_md(candidate.kol_name or '未填写 KOL')}",
        f"**联系邮箱：** {_md(candidate.contact_email or '未填写')}",
        f"**物流状态：** {_md(candidate.logistics_status or '暂无轨迹')}",
        f"**最新物流进度：** {_md(candidate.progress or '未返回物流轨迹')}",
        f"**物流：** {_logistics_label(candidate)}",
    ]
    if candidate.reminder_type == DELIVERY_REMINDER:
        lines.extend(
            [
                f"**送达时间：** {_format_local(candidate.event_at)}",
                "",
                "请向红人确认是否已收到包裹。",
            ]
        )
    else:
        heading = "发货后仍无轨迹" if candidate.no_trajectory else "物流节点未变化"
        lines.extend(
            [
                f"**{heading}：** 约 {int(candidate.elapsed_hours)} 小时",
                f"**起算时间：** {_format_local(candidate.event_at)}",
                "",
                "请跟进物流商或核对运单。",
            ]
        )
    return "\n".join(lines)


def _reminder_detail_lines(candidate: ReminderCandidate) -> List[str]:
    """Render the common operator-facing fields for every reminder item."""
    return [
        f"  - KOL Name：{_md(candidate.kol_name or '未填写 KOL')}",
        f"  - 联系邮箱：{_md(candidate.contact_email or '未填写')}",
        f"  - 物流状态：{_md(candidate.logistics_status or '暂无轨迹')}",
        f"  - 最新物流进度：{_md(candidate.progress or '未返回物流轨迹')}",
        f"  - 物流：{_logistics_label(candidate)}",
    ]


def _logistics_label(candidate: ReminderCandidate) -> str:
    carrier = _md(candidate.carrier or "承运商未填写")
    # A tracking number must remain complete even when the carrier label is
    # unusually long.  Escape it for Markdown without the general 180-character
    # display limit used by prose fields.
    tracking_number = _md_unbounded(candidate.tracking_number or "运单号未填写")
    return f"{carrier}｜{tracking_number}"


def _safe_sample_table_url(base_url: str, table_id: str) -> str:
    """Return an official Feishu/Lark table URL without inherited query data."""
    raw_url = str(base_url or "").strip()
    normalized_table_id = str(table_id or "").strip()
    if not raw_url or re.fullmatch(r"[A-Za-z0-9_-]+", normalized_table_id) is None:
        return ""
    try:
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return ""
    official_host = any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in ("feishu.cn", "larksuite.com", "larkoffice.com")
    )
    if (
        parsed.scheme.lower() != "https"
        or not official_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or re.fullmatch(r"/(?:wiki|base)/[A-Za-z0-9_-]+/?", parsed.path) is None
    ):
        return ""
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit(
        (
            "https",
            netloc,
            parsed.path,
            urlencode({"table": normalized_table_id}),
            "",
        )
    )


def _md(value: str) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) > MAX_DYNAMIC_TEXT_LENGTH:
        text = text[: MAX_DYNAMIC_TEXT_LENGTH - 1] + "…"
    return re.sub(r"([\\`*_[\]<>])", r"\\\1", text)


def _md_unbounded(value: str) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return re.sub(r"([\\`*_[\]<>])", r"\\\1", text)


def _clean_error(
    value: str,
    *,
    candidates: Sequence[ReminderCandidate] = (),
    message_content: str = "",
) -> str:
    text = str(value or "")
    text = _redact_error_literal(
        text,
        message_content,
        "<redacted-reminder-content>",
    )
    for candidate in candidates:
        text = _redact_error_literal(
            text,
            candidate.contact_email,
            "<redacted-email>",
        )
        text = _redact_error_literal(
            text,
            candidate.progress,
            "<redacted-logistics-progress>",
        )
        text = _redact_error_tracking(text, candidate.tracking_number)
    text = " ".join(text.split())
    text = re.sub(r"\bou_[A-Za-z0-9_-]+\b", "<redacted-user-id>", text)
    return text[:MAX_REMINDER_ERROR_LENGTH] or "提醒发送失败"


def _redact_error_literal(text: str, value: str, replacement: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return text
    variants = {
        raw,
        _md_unbounded(raw),
        repr(raw)[1:-1],
    }
    variants.update(variant.replace("\\", "\\\\") for variant in tuple(variants))
    for variant in sorted((item for item in variants if item), key=len, reverse=True):
        text = re.sub(re.escape(variant), replacement, text, flags=re.IGNORECASE)
    return text


def _redact_error_tracking(text: str, tracking_number: str) -> str:
    tracking = str(tracking_number or "").strip()
    text = _redact_error_literal(text, tracking, "<redacted-tracking-number>")
    identity = [character for character in tracking if character.isalnum()]
    if identity and len(identity) <= 100:
        flexible_separator = r"[\W_]*"
        pattern = flexible_separator.join(re.escape(character) for character in identity)
        text = re.sub(
            pattern,
            "<redacted-tracking-number>",
            text,
            flags=re.IGNORECASE,
        )
    return text


__all__ = [
    "BEIJING_TZ",
    "DELIVERY_REMINDER",
    "ReminderCandidate",
    "ReminderDeliveryAmbiguousError",
    "ReminderStateError",
    "STAGNATION_REMINDER",
    "SampleReminderError",
    "build_daily_reminder_card",
    "build_daily_reminder_markdown",
    "build_digest_idempotency_key",
    "dispatch_sample_reminders",
    "plan_sample_reminders",
]
