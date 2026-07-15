#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill and refresh enrichment fields for existing influencer records."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set
from urllib.parse import parse_qs, urlparse

from feishu.bitable import BitableClient
from feishu.value_utils import field_to_text
from filter.activity_evaluator import evaluate_channel_activity
from filter.channel_classifier import classify_channel
from filter.email_extractor import extract_email
from filter.kol_name_extractor import extract_kol_name
from youtube.channels import fetch_channel_details
from youtube.playlists import get_channel_uploads
from youtube.videos import fetch_video_details


logger = logging.getLogger("kol_workflow.workflow.refresh_influencers")

ALL_REFRESH_FIELDS = {"name", "activity", "assessment", "rep-title"}


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
) -> Dict[str, Any]:
    """Refresh only the five enrichment fields on existing Feishu rows.

    Existing confirmed KOL names are never overwritten by default. The
    placeholder ``手动确认`` may be upgraded when a later run finds a confident
    name. ``replace_kol_names`` is reserved for a controlled migration before
    users begin manually curating the newly-created field.
    Other business/manual fields are not included in update payloads.
    """
    selected = _selected_fields(fields)
    requested_ids = {str(value).strip() for value in (channel_ids or []) if str(value).strip()}

    records = []
    if limit is not None and limit <= 0:
        return {
            "selected_fields": sorted(selected),
            "source_records": 0,
            "estimated_quota": 0,
            "planned_updates": 0,
            "actual_updates": 0,
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

    estimated_quota = _estimate_quota(records, selected)
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
            video_id = _video_id_from_url(
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
        if selected & {"activity", "assessment"}:
            video_ids, recent_error = get_channel_uploads(
                api_key=api_key,
                channel_id=channel_id,
                uploads_playlist_id=str(channel.get("uploads_playlist_id", "") or ""),
                max_results=recent_video_count,
                quota_tracker=quota_tracker,
            )
            if not recent_error and video_ids:
                recent_videos, _, recent_error = fetch_video_details(
                    api_key,
                    video_ids,
                    quota_tracker=quota_tracker,
                )

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
            rep_id = _video_id_from_url(field_to_text(existing.get("代表视频URL")))
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

    actual_updates = 0
    if updates and not dry_run:
        actual_updates = client.batch_update_records(table_id, updates)

    return {
        "selected_fields": sorted(selected),
        "source_records": len(records),
        "estimated_quota": estimated_quota,
        "planned_updates": len(updates),
        "actual_updates": actual_updates,
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


def _estimate_quota(records: Sequence[Dict[str, Any]], selected: Set[str]) -> int:
    count = len(records)
    if not count:
        return 0
    cost = 0
    if selected & {"name", "assessment"}:
        cost += math.ceil(count / 50)  # channels.list
    if selected & {"activity", "assessment"}:
        cost += count * 2  # playlistItems.list + videos.list per channel
    if "rep-title" in selected:
        representative_ids = {
            _video_id_from_url(field_to_text((record.get("fields") or {}).get("代表视频URL")))
            for record in records
        }
        representative_ids.discard("")
        cost += math.ceil(len(representative_ids) / 50)
    return cost


def _utc_timestamp_ms(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _video_id_from_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" not in text and "?" not in text and len(text) == 11:
        return text
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    host = parsed.netloc.lower().split(":", 1)[0]
    if host in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/", 1)[0]
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            return (parse_qs(parsed.query).get("v") or [""])[0]
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
            return parts[1]
    return ""
