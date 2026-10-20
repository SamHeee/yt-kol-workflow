#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh YouTube data for existing cooperation-content records.

The workflow never creates or deletes Feishu records.  It reads the operator's
URL and cooperation keys, validates the returned YouTube channel, and updates
only the explicit system-field whitelist from
``feishu.cooperation_content``.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from feishu.bitable import BitableClient
from feishu.cooperation_content import (
    CHANNEL_ID_FIELD,
    COMMENTS_FIELD,
    CONTENT_ID_FIELD,
    CONTENT_STATUS_FIELD,
    COOPERATION_AUTO_ID_FIELD,
    COOPERATION_RECORD_ID_FIELD,
    DATA_UPDATED_AT_FIELD,
    ENGAGEMENT_RATE_FIELD,
    LIKES_FIELD,
    PUBLISH_TIME_FIELD,
    SUCCESS_SYNC_STATUSES,
    SYNC_ERROR_FIELD,
    SYNC_STATUS_FIELD,
    SYNC_STATUS_OPTIONS,
    TERMINAL_CONTENT_STATUSES,
    VIDEO_ID_FIELD,
    VIDEO_TITLE_FIELD,
    VIDEO_URL_FIELD,
    VIEWS_FIELD,
    validate_system_update_fields,
)
from feishu.value_utils import field_to_text, timestamp_ms
from youtube.videos import extract_video_id, fetch_video_details


logger = logging.getLogger("kol_workflow.workflow.refresh_cooperation_content")

MAX_ERROR_LENGTH = 500
VIDEO_API_BATCH_SIZE = 50
FEISHU_WRITE_BATCH_SIZE = 200
VALID_CONTENT_ID_PATTERN = re.compile(r"^CNT-\d{6}$")
OFFICIAL_FEISHU_URL_PATTERN = re.compile(
    r"(?i)https?://(?:[a-z0-9-]+\.)*(?:feishu\.cn|larksuite\.com|larkoffice\.com)"
    r"(?::\d+)?(?:/[^\s\"'<>]*)?"
)
SENSITIVE_TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:bascn|wikcn|doccn|shtcn)[a-z0-9_-]{4,}\b|\bcli_[a-z0-9_-]{4,}\b"
)


def refresh_cooperation_content(
    *,
    api_key: str,
    client: BitableClient,
    table_id: str,
    content_ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    stale_hours: float = 24,
    force: bool = False,
    dry_run: bool = False,
    quota_tracker=None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Plan and optionally apply YouTube updates to existing content rows.

    ``dry_run`` still calls YouTube so the returned plan is accurate, matching
    the behaviour of the existing influencer refresh command.  It never calls
    Feishu's write endpoint.  ``limit`` applies to records that need work, not
    to fresh or terminal rows.
    """
    if not api_key:
        raise ValueError("YouTube API Key 不能为空")
    if not table_id:
        raise ValueError("合作内容数据表 table_id 不能为空")
    if stale_hours < 0:
        raise ValueError("stale_hours 不能小于 0")
    if limit is not None and limit < 0:
        raise ValueError("limit 不能小于 0")

    current_time = _as_utc(now or datetime.now(timezone.utc))
    current_timestamp_ms = int(current_time.timestamp() * 1000)
    requested_content_ids = {
        str(value).strip()
        for value in (content_ids or [])
        if str(value).strip()
    }

    records = client.get_all_records(table_id)
    selected_records = [
        record
        for record in records
        if not requested_content_ids
        or field_to_text((record.get("fields") or {}).get(CONTENT_ID_FIELD))
        in requested_content_ids
    ]

    parsed_ids_by_record: Dict[str, str] = {}
    records_by_video_id: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for record in records:
        record_id = str(record.get("record_id") or "")
        fields = record.get("fields") or {}
        content_id = field_to_text(fields.get(CONTENT_ID_FIELD))
        video_id = extract_video_id(field_to_text(fields.get(VIDEO_URL_FIELD)))
        if record_id:
            parsed_ids_by_record[record_id] = video_id
        # Terminal or unmatched rows cannot own a video: they will never be
        # refreshed below, so letting them win would strand valid rows.
        if (
            record_id and video_id
            and field_to_text(fields.get(CONTENT_STATUS_FIELD)) not in TERMINAL_CONTENT_STATUSES
            and not _cooperation_match_error(fields)
        ):
            records_by_video_id[video_id].append((content_id, record_id))
    duplicate_primary_by_record: Dict[str, str] = {}
    for occurrences in records_by_video_id.values():
        if len(occurrences) <= 1:
            continue
        ordered = sorted(occurrences, key=_duplicate_primary_sort_key)
        primary_content_id, primary_record_id = ordered[0]
        primary_label = _duplicate_primary_label(primary_content_id, primary_record_id)
        for _, duplicate_record_id in ordered[1:]:
            duplicate_primary_by_record[duplicate_record_id] = primary_label

    plans: List[Dict[str, Any]] = []
    api_candidates: Dict[str, Dict[str, Any]] = {}
    skipped_counts: Counter = Counter()
    errors: List[Dict[str, str]] = []
    candidate_count = 0

    for record in selected_records:
        record_id = str(record.get("record_id") or "")
        fields = record.get("fields") or {}
        content_id = field_to_text(fields.get(CONTENT_ID_FIELD))
        content_status = field_to_text(fields.get(CONTENT_STATUS_FIELD))
        video_url = field_to_text(fields.get(VIDEO_URL_FIELD))
        parsed_video_id = parsed_ids_by_record.get(record_id, "")

        if not record_id:
            skipped_counts["missing_record_id"] += 1
            continue
        if content_status in TERMINAL_CONTENT_STATUSES:
            skipped_counts["terminal_status"] += 1
            continue
        if not video_url:
            skipped_counts["empty_url"] += 1
            continue

        cooperation_error = _cooperation_match_error(fields)
        local_status = ""
        local_error = ""
        if not parsed_video_id:
            local_status = "URL无效"
            local_error = "视频URL不是受支持的 YouTube URL 或有效的 11 位 Video ID"
        elif record_id in duplicate_primary_by_record:
            local_status = "重复视频"
            local_error = (
                f"Video ID {parsed_video_id} 在合作内容数据表中出现多次；"
                f"主记录标识：{duplicate_primary_by_record[record_id]}"
            )
        elif cooperation_error:
            local_status = "合作匹配失败"
            local_error = cooperation_error
        elif not _needs_refresh(
            fields,
            parsed_video_id,
            current_time,
            stale_hours=stale_hours,
            force=force,
        ):
            skipped_counts["fresh"] += 1
            continue

        if limit is not None and candidate_count >= limit:
            skipped_counts["limit"] += 1
            continue
        candidate_count += 1

        snapshot = {
            "record_id": record_id,
            "content_id": content_id,
            "fields": fields,
            "video_url": video_url,
            "video_id": parsed_video_id,
            "signature": _source_signature(fields),
        }
        if local_status:
            plan = _failure_plan(snapshot, local_status, local_error, api_key=api_key)
            plans.append(plan)
            errors.append(_error_item(plan))
        else:
            api_candidates[parsed_video_id] = snapshot

    video_ids = list(api_candidates)
    estimated_quota = math.ceil(len(video_ids) / VIDEO_API_BATCH_SIZE) if video_ids else 0
    if quota_tracker is not None and estimated_quota > quota_tracker.remaining():
        raise RuntimeError(
            f"合作内容刷新预计需要 {estimated_quota} units，当前进程预算仅剩 "
            f"{quota_tracker.remaining()} units；请使用 limit 分批执行"
        )

    videos_by_id: Dict[str, Dict[str, Any]] = {}
    api_failures: Dict[str, str] = {}
    unavailable_ids = set()
    api_batches = 0
    for offset in range(0, len(video_ids), VIDEO_API_BATCH_SIZE):
        batch = video_ids[offset:offset + VIDEO_API_BATCH_SIZE]
        api_batches += 1
        try:
            videos, missing_ids, api_error = fetch_video_details(
                api_key,
                batch,
                quota_tracker=quota_tracker,
            )
        except Exception as exc:  # keep one failed batch from discarding other batches
            videos, missing_ids, api_error = [], batch, str(exc)
        for video in videos:
            video_id = str(video.get("video_id") or "")
            if video_id:
                videos_by_id[video_id] = video
        found = set(videos_by_id).intersection(batch)
        not_found = set(batch) - found
        if api_error:
            clean_error = _clean_error(api_error, api_key=api_key)
            for video_id in not_found:
                api_failures[video_id] = clean_error or "YouTube API 请求失败"
        else:
            unavailable_ids.update(not_found)
            unavailable_ids.update(video_id for video_id in missing_ids if video_id in batch)

    partial_metric_records = 0
    for video_id in video_ids:
        snapshot = api_candidates[video_id]
        if video_id in api_failures:
            plan = _failure_plan(
                snapshot,
                "API失败",
                api_failures[video_id],
                api_key=api_key,
            )
            plans.append(plan)
            errors.append(_error_item(plan))
            continue
        if video_id in unavailable_ids or video_id not in videos_by_id:
            plan = _failure_plan(
                snapshot,
                "视频不可用",
                "YouTube 未返回该视频；视频可能不存在、已删除或为私密状态",
                api_key=api_key,
            )
            plans.append(plan)
            errors.append(_error_item(plan))
            continue

        video = videos_by_id[video_id]
        expected_channel_id = field_to_text(snapshot["fields"].get(CHANNEL_ID_FIELD))
        actual_channel_id = str(video.get("channel_id") or "").strip()
        if not actual_channel_id or actual_channel_id != expected_channel_id:
            plan = _failure_plan(
                snapshot,
                "频道不匹配",
                f"视频频道 {actual_channel_id or '未知'} 与合作 Channel ID {expected_channel_id} 不一致",
                api_key=api_key,
            )
            plans.append(plan)
            errors.append(_error_item(plan))
            continue

        plan = _success_plan(snapshot, video, current_timestamp_ms)
        if plan["status"] == "部分指标不可用":
            partial_metric_records += 1
        plans.append(plan)

    status_counts = Counter(plan["status"] for plan in plans)
    planned_updates = len(plans)
    actual_updates = 0
    concurrent_skips = 0
    missing_before_write = 0
    write_fallback_used = False
    write_failures = 0
    batch_write_error = ""

    if plans and not dry_run:
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
            safe_updates.append({
                "record_id": plan["record_id"],
                "fields": validate_system_update_fields(plan["update_fields"]),
            })
        if safe_updates:
            plans_by_record_id = {plan["record_id"]: plan for plan in plans}
            for offset in range(0, len(safe_updates), FEISHU_WRITE_BATCH_SIZE):
                write_batch = safe_updates[offset:offset + FEISHU_WRITE_BATCH_SIZE]
                try:
                    actual_updates += client.batch_update_records(table_id, write_batch)
                    continue
                except Exception as batch_exc:
                    clean_batch_error = _clean_error(batch_exc, api_key=api_key)
                    if _is_rate_limit_error(clean_batch_error):
                        raise RuntimeError(
                            "飞书内容批量写入触发限流，已停止后续写入: "
                            + clean_batch_error
                        ) from batch_exc
                    write_fallback_used = True
                    if not batch_write_error:
                        batch_write_error = clean_batch_error
                    logger.warning(
                        "合作内容批量写入失败，改为逐条重试: %s",
                        clean_batch_error,
                    )
                    errors.append({
                        "content_id": "",
                        "record_id": "",
                        "status": "",
                        "stage": "feishu_batch_write",
                        "error": "飞书批量写入失败，已降级逐条重试: "
                        + clean_batch_error,
                    })
                for update in write_batch:
                    record_id = update["record_id"]
                    try:
                        client.update_record(table_id, record_id, update["fields"])
                        actual_updates += 1
                    except Exception as item_exc:
                        write_failures += 1
                        plan = plans_by_record_id.get(record_id, {})
                        errors.append({
                            "content_id": str(plan.get("content_id") or ""),
                            "record_id": record_id,
                            "status": str(plan.get("status") or ""),
                            "stage": "feishu_write",
                            "error": "飞书逐条写入失败: "
                            + _clean_error(item_exc, api_key=api_key),
                        })

    samples = [
        {
            "content_id": plan["content_id"],
            "record_id": plan["record_id"],
            "status": plan["status"],
            "fields": dict(plan["update_fields"]),
        }
        for plan in plans[:10]
    ]
    normalized_status_counts = {
        status: status_counts.get(status, 0)
        for status in SYNC_STATUS_OPTIONS
    }
    return {
        "source_records": len(records),
        "selected_records": len(selected_records),
        "candidate_records": candidate_count,
        "requested_video_ids": len(video_ids),
        "api_batches": api_batches,
        "estimated_quota": estimated_quota,
        "planned_updates": planned_updates,
        "actual_updates": actual_updates,
        "dry_run": dry_run,
        "force": force,
        "stale_hours": stale_hours,
        "partial_metric_records": partial_metric_records,
        "concurrent_skips": concurrent_skips,
        "missing_before_write": missing_before_write,
        "write_fallback_used": write_fallback_used,
        "write_failures": write_failures,
        "batch_write_error": batch_write_error,
        "status_counts": normalized_status_counts,
        "skipped_counts": dict(sorted(skipped_counts.items())),
        "errors": errors,
        "samples": samples,
    }


def _needs_refresh(
    fields: Mapping[str, Any],
    parsed_video_id: str,
    now: datetime,
    *,
    stale_hours: float,
    force: bool,
) -> bool:
    if force:
        return True
    existing_video_id = field_to_text(fields.get(VIDEO_ID_FIELD))
    sync_status = field_to_text(fields.get(SYNC_STATUS_FIELD))
    if not existing_video_id or existing_video_id != parsed_video_id:
        return True
    if sync_status not in SUCCESS_SYNC_STATUSES:
        return True
    updated_at = _field_datetime(fields.get(DATA_UPDATED_AT_FIELD))
    if updated_at is None:
        return True
    age_hours = max(0.0, (now - updated_at).total_seconds() / 3600)
    return age_hours >= stale_hours


def _duplicate_primary_sort_key(item: Tuple[str, str]) -> Tuple[int, str, str]:
    """Prefer a canonical content ID; otherwise use the stable record ID."""
    content_id, record_id = item
    if VALID_CONTENT_ID_PATTERN.fullmatch(content_id):
        return 0, content_id, record_id
    return 1, record_id, ""


def _duplicate_primary_label(content_id: str, record_id: str) -> str:
    if VALID_CONTENT_ID_PATTERN.fullmatch(content_id):
        return f"内容记录ID={content_id}（record_id={record_id}）"
    return f"record_id={record_id}"


def _cooperation_match_error(fields: Mapping[str, Any]) -> str:
    if not field_to_text(fields.get(COOPERATION_AUTO_ID_FIELD)):
        return "合作自动编号为空"
    if _lookup_value_count(fields.get(COOPERATION_RECORD_ID_FIELD)) != 1:
        return "合作记录ID查找引用未唯一匹配一条合作记录"
    if _lookup_value_count(fields.get(CHANNEL_ID_FIELD)) != 1:
        return "Channel ID查找引用未唯一匹配一条合作记录"
    return ""


def _lookup_value_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, list):
        return sum(1 for item in value if field_to_text(item))
    return 1 if field_to_text(value) else 0


def _success_plan(
    snapshot: Mapping[str, Any],
    video: Mapping[str, Any],
    updated_at_ms: int,
) -> Dict[str, Any]:
    existing = snapshot["fields"]
    video_id = str(video.get("video_id") or snapshot["video_id"])
    existing_video_id = field_to_text(existing.get(VIDEO_ID_FIELD))
    identity_changed = existing_video_id != video_id

    update_fields: Dict[str, Any] = {VIDEO_ID_FIELD: video_id}
    title = str(video.get("title") or "")
    if title or identity_changed:
        update_fields[VIDEO_TITLE_FIELD] = title
    published_at = timestamp_ms(
        video.get("published_at_raw") or video.get("published_at")
    )
    if published_at is not None or identity_changed:
        update_fields[PUBLISH_TIME_FIELD] = published_at

    metric_specs = (
        (VIEWS_FIELD, "view_count", "view_count_available"),
        (LIKES_FIELD, "like_count", "like_count_available"),
        (COMMENTS_FIELD, "comment_count", "comment_count_available"),
    )
    missing_metrics = []
    metric_values: Dict[str, int] = {}
    for field_name, value_key, availability_key in metric_specs:
        available = video.get(availability_key)
        if available is None:
            available = value_key in video and video.get(value_key) is not None
        if available:
            metric_value = _safe_nonnegative_int(video.get(value_key))
            metric_values[field_name] = metric_value
            update_fields[field_name] = metric_value
        else:
            missing_metrics.append(field_name)
            if identity_changed:
                update_fields[field_name] = None

    if not missing_metrics:
        views = metric_values[VIEWS_FIELD]
        likes = metric_values[LIKES_FIELD]
        comments = metric_values[COMMENTS_FIELD]
        engagement_rate = round((likes + comments) / views * 100, 2) if views else 0.0
        update_fields[ENGAGEMENT_RATE_FIELD] = engagement_rate
    elif identity_changed:
        update_fields[ENGAGEMENT_RATE_FIELD] = None

    status = "部分指标不可用" if missing_metrics else "同步成功"
    update_fields[DATA_UPDATED_AT_FIELD] = updated_at_ms
    update_fields[SYNC_STATUS_FIELD] = status
    if missing_metrics:
        missing_action = "新视频对应字段已清空" if identity_changed else "未返回字段保留原值"
        update_fields[SYNC_ERROR_FIELD] = (
            "YouTube 未返回指标: " + ", ".join(missing_metrics) + f"；{missing_action}"
        )
    else:
        update_fields[SYNC_ERROR_FIELD] = ""
    content_status = field_to_text(existing.get(CONTENT_STATUS_FIELD))
    live_broadcast_content = str(
        video.get("live_broadcast_content") or "none"
    ).strip().lower()
    is_upcoming = live_broadcast_content == "upcoming"
    is_future_publication = (
        published_at is not None and published_at > updated_at_ms
    )
    if (
        content_status in {"", "待发布"}
        and not is_upcoming
        and not is_future_publication
    ):
        update_fields[CONTENT_STATUS_FIELD] = "已发布"

    return {
        "record_id": snapshot["record_id"],
        "content_id": snapshot["content_id"],
        "signature": snapshot["signature"],
        "status": status,
        "update_fields": validate_system_update_fields(update_fields),
    }


def _failure_plan(
    snapshot: Mapping[str, Any],
    status: str,
    message: str,
    *,
    api_key: str,
) -> Dict[str, Any]:
    update_fields = validate_system_update_fields({
        SYNC_STATUS_FIELD: status,
        SYNC_ERROR_FIELD: _clean_error(message, api_key=api_key),
    })
    return {
        "record_id": snapshot["record_id"],
        "content_id": snapshot["content_id"],
        "signature": snapshot["signature"],
        "status": status,
        "update_fields": update_fields,
    }


def _error_item(plan: Mapping[str, Any]) -> Dict[str, str]:
    return {
        "content_id": str(plan.get("content_id") or ""),
        "record_id": str(plan.get("record_id") or ""),
        "status": str(plan.get("status") or ""),
        "error": field_to_text((plan.get("update_fields") or {}).get(SYNC_ERROR_FIELD)),
    }


def _source_signature(fields: Mapping[str, Any]) -> Tuple[str, ...]:
    """Fields whose concurrent edits make a previously planned write unsafe."""
    return (
        _signature_text(fields.get(VIDEO_URL_FIELD)),
        _signature_text(fields.get(COOPERATION_AUTO_ID_FIELD)),
        _signature_text(fields.get(CHANNEL_ID_FIELD)),
        _signature_text(fields.get(COOPERATION_RECORD_ID_FIELD)),
        _signature_text(fields.get(CONTENT_STATUS_FIELD)),
        _signature_text(fields.get(VIDEO_ID_FIELD)),
        _signature_text(fields.get(DATA_UPDATED_AT_FIELD)),
        _signature_text(fields.get(SYNC_STATUS_FIELD)),
    )


def _signature_text(value: Any) -> str:
    """Normalize list and batch-get envelopes before concurrency comparison.

    Feishu's record-list endpoint returns lookup cells as a list of text
    segments, while batch-get may wrap the same segments in
    ``{"type": ..., "value": [...]}``.  They represent the same cell and
    must not look like an operator edit during write-before-reread.
    """
    if isinstance(value, Mapping) and "value" in value:
        return field_to_text(value.get("value"))
    return field_to_text(value)


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


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _clean_error(value: Any, *, api_key: str = "") -> str:
    text = " ".join(str(value or "").split())
    if api_key:
        text = text.replace(api_key, "***")
    text = OFFICIAL_FEISHU_URL_PATTERN.sub(_mask_official_feishu_url, text)
    secret_names = (
        r"device[_-]?code|access[_-]?token|refresh[_-]?token|"
        r"tenant[_-]?access[_-]?token|user[_-]?access[_-]?token|"
        r"app[_-]?(?:id|secret|token)|client[_-]?secret|base[_-]?token|"
        r"wiki[_-]?token|authorization[_-]?code|verification[_-]?(?:url|uri)"
        r"(?:[_-]?complete)?"
    )
    text = re.sub(
        rf"(?i)([\"']?(?:{secret_names})[\"']?\s*[:=]\s*[\"']?)"
        r"[^\s\"',}\]&]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)\bBearer\s+[^\s\"',}\]]+",
        "Bearer <redacted>",
        text,
    )
    text = SENSITIVE_TOKEN_PATTERN.sub("<redacted>", text)
    return text[:MAX_ERROR_LENGTH]


def _mask_official_feishu_url(match: re.Match[str]) -> str:
    url = match.group(0)
    origin_match = re.match(r"(?i)(https?://[^/]+)", url)
    origin = origin_match.group(1) if origin_match else "<feishu>"
    return f"{origin}/<redacted>"


def _is_rate_limit_error(value: str) -> bool:
    text = str(value or "").lower()
    return "1254291" in text or "too many request" in text or "rate limit" in text
