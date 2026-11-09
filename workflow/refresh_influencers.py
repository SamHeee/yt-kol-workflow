#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill and refresh enrichment fields for existing influencer records."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from feishu.bitable import BitableClient
from feishu.value_utils import field_to_text, number_value
from filter.activity_evaluator import evaluate_channel_activity
from filter.channel_classifier import classify_channel
from filter.email_extractor import extract_email
from filter.kol_name_extractor import extract_kol_name
from youtube.channels import fetch_channel_details
from youtube.recent_videos import collect_recent_videos
from workflow.recent_video_metrics import calculate_recent_metrics, select_table_videos, metric_fields, METRIC_FIELDS
from workflow.recent_video_store import save_sample, table_videos
from youtube.quota import estimate_channel_detail_cost
from youtube.videos import extract_video_id, fetch_video_details


logger = logging.getLogger("kol_workflow.workflow.refresh_influencers")

ALL_REFRESH_FIELDS = {"name", "activity", "assessment", "rep-title", "recent-views"}


def refresh_influencers(
    *,
    api_key: str,
    client: BitableClient,
    table_id: str,
    fields: str = "all",
    channel_ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    quota_tracker=None,
    recent_video_count: int = 10,
    replace_kol_names: bool = False,
    videos_table_id: str = "",
    recent_source: str = "youtube",
) -> Dict[str, Any]:
    """Refresh selected enrichment fields and recent-upload statistics.

    Existing confirmed KOL names are never overwritten by default. The
    placeholder ``手动确认`` may be upgraded when a later run finds a confident
    name. ``replace_kol_names`` is reserved for a controlled migration before
    users begin manually curating the newly-created field.
    Other business/manual fields are not included in update payloads.
    """
    selected = _selected_fields(fields)
    if recent_source not in {"youtube", "table"}:
        raise ValueError("未知最近视频数据源")
    if recent_source == "table" and selected != {"recent-views"}:
        raise ValueError("table 数据源仅支持 --fields recent-views")
    if "recent-views" in selected and not videos_table_id:
        raise ValueError("最近视频统计需要网红视频表")
    requested_ids = {str(value).strip() for value in (channel_ids or []) if str(value).strip()}

    records = []
    if limit is not None and limit <= 0:
        return {
            "selected_fields": sorted(selected),
            "source_records": 0,
            "estimated_quota": 0,
            "planned_updates": 0,
            "actual_updates": 0,
            "concurrent_skips": 0,
            "missing_before_write": 0,
            "protected_names": 0,
            "dry_run": dry_run,
            "replace_kol_names": replace_kol_names,
            "status_counts": {"持续更新": 0, "有断更风险": 0, "待确认": 0},
            "manual_confirmation_count": 0,
            "errors": [],
            "samples": [],
        }
    for record in client.get_all_records(table_id):
        record_fields = record.get("fields") or {}
        channel_id = field_to_text(record_fields.get("Channel ID"))
        if not channel_id or (requested_ids and channel_id not in requested_ids):
            continue
        records.append(record)
        if limit is not None and len(records) >= limit:
            break

    estimated_quota = 0 if recent_source == "table" else _estimate_quota(records, selected, recent_video_count=recent_video_count)
    if quota_tracker is not None and estimated_quota > quota_tracker.remaining():
        raise RuntimeError(
            f"刷新预计需要约 {estimated_quota} units，当前进程预算仅剩 "
            f"{quota_tracker.remaining()} units；请使用 --limit 分批执行。"
        )

    channel_data_by_id: Dict[str, Dict[str, Any]] = {}
    if records and selected & {"name", "assessment"}:
        ids = [field_to_text((record.get("fields") or {}).get("Channel ID")) for record in records]
        channels, channel_error = fetch_channel_details(api_key, ids, quota_tracker=quota_tracker)
        channel_data_by_id = {channel.get("channel_id", ""): channel for channel in channels}
        if channel_error:
            logger.warning("刷新频道基础资料时出现错误，将使用飞书已有值回退: %s", channel_error)

    representative_titles: Dict[str, str] = {}
    representative_error = ""
    if records and "rep-title" in selected:
        rep_ids = []
        for record in records:
            video_id = extract_video_id(
                field_to_text((record.get("fields") or {}).get("代表视频URL"))
            )
            if video_id and video_id not in rep_ids:
                rep_ids.append(video_id)
        if rep_ids:
            videos, _, representative_error = fetch_video_details(
                api_key,
                rep_ids,
                quota_tracker=quota_tracker,
            )
            representative_titles = {
                video.get("video_id", ""): str(video.get("title", "") or "")
                for video in videos
                if video.get("video_id")
            }

    updates: List[Dict[str, Any]] = []
    samples: List[Dict[str, Any]] = []
    status_counts = {"持续更新": 0, "有断更风险": 0, "待确认": 0}
    manual_confirmation_count = 0
    errors: List[Dict[str, str]] = []

    recent_cache = {}
    snapshots = {}
    source_videos = table_videos(client.get_all_records(videos_table_id)) if recent_source == "table" else []

    for record in records:
        record_id = record.get("record_id", "")
        existing = record.get("fields") or {}
        channel_id = field_to_text(existing.get("Channel ID"))
        channel = channel_data_by_id.get(channel_id, {})
        channel_name = str(
            channel.get("channel_title") or field_to_text(existing.get("Channel Name"))
        )
        description = str(
            channel.get("channel_description") or field_to_text(existing.get("频道描述"))
        )
        contact_email = field_to_text(existing.get("联系邮箱")) or extract_email(description) or ""
        source_keyword = field_to_text(existing.get("来源关键词"))
        update_fields: Dict[str, Any] = {}

        if "name" in selected:
            existing_name = field_to_text(existing.get("KOL Name"))
            candidate = extract_kol_name(channel_name, description, contact_email)
            if replace_kol_names or not existing_name or existing_name == "手动确认":
                update_fields["KOL Name"] = candidate
            if candidate == "手动确认":
                manual_confirmation_count += 1

        recent_videos: List[Dict[str, Any]] = []
        recent_error = ""
        if selected & {"activity", "assessment", "recent-views"}:
            if channel_id not in recent_cache:
                try:
                    if recent_source == "table":
                        fetched = select_table_videos(source_videos, channel_id, recent_video_count)
                        recent_cache[channel_id] = (fetched, "")
                    else:
                        recent_cache[channel_id] = collect_recent_videos(
                            api_key, channel_id,
                            uploads_playlist_id=str(channel.get("uploads_playlist_id", "") or ""),
                            count=recent_video_count, quota_tracker=quota_tracker,
                        )
                except ValueError as exc:
                    recent_cache[channel_id] = ([], str(exc))
            recent_videos, recent_error = recent_cache[channel_id]
            for video in recent_videos:
                video["channel_title"] = channel_name

        if "recent-views" in selected:
            if recent_error:
                errors.append({"channel_id": channel_id, "stage": "recent-views", "error": recent_error})
            else:
                snapshot = calculate_recent_metrics(recent_videos, channel_id=channel_id)
                snapshots[channel_id] = snapshot
                update_fields.update(metric_fields(snapshot))

        if "activity" in selected:
            activity = evaluate_channel_activity(recent_videos, fetch_error=recent_error)
            status = activity["activity_status"]
            update_fields["断更评估"] = status
            status_counts[status] = status_counts.get(status, 0) + 1
            if activity["latest_published_at"]:
                update_fields["最新发布日期"] = _utc_timestamp_ms(
                    activity["latest_published_at"]
                )
            if activity["error"]:
                errors.append({"channel_id": channel_id, "stage": "activity", "error": activity["error"]})

        if "assessment" in selected:
            update_fields["频道初步判断"] = classify_channel(
                channel_name,
                description,
                recent_videos,
                source_keyword,
            )

        if "rep-title" in selected:
            rep_id = extract_video_id(field_to_text(existing.get("代表视频URL")))
            title = representative_titles.get(rep_id, "")
            if title:
                update_fields["代表视频标题"] = title
            elif rep_id and representative_error:
                errors.append(
                    {"channel_id": channel_id, "stage": "representative_video", "error": representative_error}
                )

        if record_id and update_fields:
            updates.append({"record_id": record_id, "fields": update_fields})
            if len(samples) < 10:
                samples.append({"channel_id": channel_id, "fields": update_fields})

    video_records_saved = 0
    if "recent-views" in selected and recent_source == "youtube" and not dry_run:
        video_inventory = client.get_all_records(videos_table_id) if snapshots else []
        for cid in list(snapshots):
            try:
                video_records_saved += save_sample(client, videos_table_id, recent_cache[cid][0], video_inventory)
            except Exception as exc:
                errors.append({"channel_id": cid, "stage": "save-videos", "error": str(exc)})
                snapshots.pop(cid)
        by_record_id = {r["record_id"]: field_to_text((r.get("fields") or {}).get("Channel ID")) for r in records}
        for update in updates:
            if by_record_id[update["record_id"]] not in snapshots:
                for name in METRIC_FIELDS:
                    update["fields"].pop(name, None)
        updates = [u for u in updates if u["fields"]]

    actual_updates = 0
    concurrent_skips = 0
    missing_before_write = 0
    protected_names = 0
    if updates and not dry_run:
        originals = {
            record["record_id"]: record.get("fields") or {}
            for record in records if record.get("record_id")
        }
        for offset in range(0, len(updates), 100):
            batch = updates[offset:offset + 100]
            # Re-read immediately before each write batch. A long YouTube fetch
            # must not overwrite a name or source that a person changed meanwhile.
            latest_records = client.batch_get_records(
                table_id, [update["record_id"] for update in batch]
            )
            latest_by_id = {
                record["record_id"]: record.get("fields") or {}
                for record in latest_records if record.get("record_id")
            }
            safe_updates = []
            for update in batch:
                record_id = update["record_id"]
                latest = latest_by_id.get(record_id)
                if latest is None:
                    missing_before_write += 1
                    continue
                original = originals[record_id]
                if _source_signature(latest) != _source_signature(original):
                    concurrent_skips += 1
                    continue
                safe_fields = dict(update["fields"])
                latest_name = field_to_text(latest.get("KOL Name"))
                original_name = field_to_text(original.get("KOL Name"))
                if "KOL Name" in safe_fields and (
                    latest_name != original_name
                    or (latest_name and latest_name != "手动确认" and not replace_kol_names)
                ):
                    safe_fields.pop("KOL Name")
                    protected_names += 1
                if any(name in safe_fields for name in METRIC_FIELDS) and any(
                        number_value(latest.get(name)) != number_value(original.get(name))
                        for name in METRIC_FIELDS):
                    for name in METRIC_FIELDS:
                        safe_fields.pop(name, None)
                    concurrent_skips += 1
                if safe_fields:
                    safe_updates.append({"record_id": record_id, "fields": safe_fields})
            if safe_updates:
                written = client.batch_update_records(table_id, safe_updates)
                actual_updates += written
                if written != len(safe_updates):
                    errors.append({"stage": "write-creators", "error": "网红更新数量不一致"})
                metric_updates = [u for u in safe_updates if all(k in u["fields"] for k in METRIC_FIELDS)]
                if metric_updates:
                    checked = {r["record_id"]: r.get("fields") or {} for r in client.batch_get_records(
                        table_id, [u["record_id"] for u in metric_updates])}
                    for update in metric_updates:
                        actual = checked.get(update["record_id"])
                        if actual is None or any(not _metric_equal(actual.get(k), update["fields"][k]) for k in METRIC_FIELDS):
                            errors.append({"record_id": update["record_id"], "stage": "verify-metrics",
                                           "error": "频道指标读回不一致"})

    return {
        "selected_fields": sorted(selected),
        "recent_source": recent_source,
        "recent_video_snapshots": snapshots,
        "video_records_saved": video_records_saved,
        "source_records": len(records),
        "estimated_quota": estimated_quota,
        "planned_updates": len(updates),
        "actual_updates": actual_updates,
        "concurrent_skips": concurrent_skips,
        "missing_before_write": missing_before_write,
        "protected_names": protected_names,
        "dry_run": dry_run,
        "replace_kol_names": replace_kol_names,
        "status_counts": status_counts,
        "manual_confirmation_count": manual_confirmation_count,
        "errors": errors,
        "samples": samples,
    }


def _selected_fields(choice: str) -> Set[str]:
    if choice == "all":
        return set(ALL_REFRESH_FIELDS)
    if choice not in ALL_REFRESH_FIELDS:
        raise ValueError(f"未知刷新字段: {choice}")
    return {choice}


def _source_signature(fields: Dict[str, Any]) -> tuple:
    return tuple(field_to_text(fields.get(name)) for name in (
        "Channel ID", "Channel Name", "频道描述", "联系邮箱", "来源关键词", "代表视频URL",
    ))


def _estimate_quota(
    records: Sequence[Dict[str, Any]],
    selected: Set[str],
    recent_video_count: int = 10,
) -> int:
    count = len(records)
    if not count:
        return 0
    cost = 0
    if selected & {"name", "assessment"}:
        cost += math.ceil(count / 50)  # channels.list
    if selected & {"activity", "assessment", "recent-views"}:
        detail = estimate_channel_detail_cost(count, recent_video_count=recent_video_count)
        cost += detail["recent_videos"] + detail["recent_video_detail"]
    if "rep-title" in selected:
        representative_ids = {
            extract_video_id(field_to_text((record.get("fields") or {}).get("代表视频URL")))
            for record in records
        }
        representative_ids.discard("")
        cost += math.ceil(len(representative_ids) / 50)
    return cost


def _utc_timestamp_ms(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _metric_equal(actual, expected):
    if expected is None:
        return actual is None
    if isinstance(actual, bool):
        return False
    parsed = number_value(actual)
    return parsed is not None and math.isfinite(parsed) and math.isclose(
        parsed, expected, rel_tol=1e-9, abs_tol=1e-8)
