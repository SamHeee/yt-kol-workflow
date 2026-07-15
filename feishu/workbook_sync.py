#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sync a merged KOL workbook into the matching Feishu Bitable tables."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import FeishuConfig, YouTubeConfig
from feishu import schema as feishu_schema
from feishu.bitable import BitableClient
from feishu.client_factory import (
    create_bitable_client_from_config,
    initialize_created_base_schema,
)
from feishu.cleanup import cleanup_empty_records, mask_token
from feishu.value_utils import (
    bool_value,
    clean_text,
    field_to_text,
    format_value,
    is_blank,
    normalize_text,
    number_value,
)
from youtube.channels import fetch_channel_details


BATCH_SIZE = 50
REQUEST_SLEEP_SECONDS = 0.12
PRIMARY_FIELD = getattr(feishu_schema, "PRIMARY_FIELD_NAME", "多行文本")
DESCRIPTION_FIELD = "频道描述"
INFLUENCERS_TABLE_KEY = "influencers"
KOL_NAME_FIELD = "KOL Name"
KOL_NAME_MANUAL_CONFIRMATION = "手动确认"


def _schema_field_names(field_definitions: Iterable[Tuple[str, int]]) -> List[str]:
    names = [name for name, _field_type in field_definitions]
    if PRIMARY_FIELD not in names:
        names.insert(0, PRIMARY_FIELD)
    return names


def _read_only_fields(field_definitions: Iterable[Tuple[str, int]]) -> set:
    return {PRIMARY_FIELD} | {
        name for name, field_type in field_definitions if field_type == 1001
    }


def _identity_field_map(field_definitions: Iterable[Tuple[str, int]]) -> Dict[str, str]:
    return {name: name for name in _schema_field_names(field_definitions)}


# Old local exports remain readable.  Rows are normalized to these canonical
# Feishu headers before any key lookup, formatting, verification, or write.
LEGACY_HEADER_ALIASES: Dict[str, Dict[str, str]] = {
    "search_tasks": {},
    "search_videos": {
        "频道ID": "Channel ID",
        "频道名称": "Channel Name",
        "视频标题": "Video Title",
        "发布时间": "Publish Time",
        "播放量": "Views",
        "点赞数": "Likes",
        "评论数": "Comments",
        "时长(秒)": "Duration (sec)",
        "时长": "Duration (H:M:S)",
        "标签": "Tags",
        "有字幕": "Has Subtitles",
        "通过筛选": "是否通过筛选",
    },
    "influencers": {
        "频道ID": "Channel ID",
        "频道名称": "Channel Name",
        "代表视频互动率(%)": "代表视频互动率",
    },
    "influencer_videos": {
        "频道ID": "Channel ID",
        "频道名称": "Channel Name",
        "视频标题": "Video Title",
        "发布时间": "Publish Time",
        "播放量": "Views",
        "点赞数": "Likes",
        "评论数": "Comments",
        "时长(秒)": "Duration (sec)",
        "时长": "Duration (H:M:S)",
        "标签": "Tags",
    },
}


TABLE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "search_tasks": {
        "sheet": "search_tasks",
        "table_name": feishu_schema.SEARCH_TASKS_TABLE,
        "key_field": "唯一键",
        "key_column": "唯一键",
        "field_map": _identity_field_map(feishu_schema.SEARCH_TASKS_FIELDS),
        "header_aliases": LEGACY_HEADER_ALIASES["search_tasks"],
        "read_only_fields": _read_only_fields(feishu_schema.SEARCH_TASKS_FIELDS),
        "preserve_existing_nonblank": set(),
    },
    "search_videos": {
        "sheet": "search_videos",
        "table_name": feishu_schema.SEARCH_VIDEOS_TABLE,
        "key_field": "唯一键",
        "key_column": "唯一键",
        "reuse_blank_records": False,
        "field_map": _identity_field_map(feishu_schema.SEARCH_VIDEOS_FIELDS),
        "header_aliases": LEGACY_HEADER_ALIASES["search_videos"],
        "read_only_fields": _read_only_fields(feishu_schema.SEARCH_VIDEOS_FIELDS),
        "preserve_existing_nonblank": set(),
    },
    "influencers": {
        "sheet": "influencers",
        "table_name": feishu_schema.INFLUENCERS_TABLE,
        "key_field": "Channel ID",
        "key_column": "Channel ID",
        "reuse_blank_records": False,
        "field_map": _identity_field_map(feishu_schema.INFLUENCERS_FIELDS),
        "header_aliases": LEGACY_HEADER_ALIASES["influencers"],
        "read_only_fields": _read_only_fields(feishu_schema.INFLUENCERS_FIELDS),
        "preserve_existing_nonblank": {"联系邮箱", "邮箱状态", "开发状态", "开发负责人", "备注"},
    },
    "influencer_videos": {
        "sheet": "influencer_videos",
        "table_name": feishu_schema.INFLUENCER_VIDEOS_TABLE,
        "key_field": "唯一键",
        "key_column": "唯一键",
        "field_map": _identity_field_map(feishu_schema.INFLUENCER_VIDEOS_FIELDS),
        "header_aliases": LEGACY_HEADER_ALIASES["influencer_videos"],
        "read_only_fields": _read_only_fields(feishu_schema.INFLUENCER_VIDEOS_FIELDS),
        "preserve_existing_nonblank": set(),
    },
}


def chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def table_keys_from_choice(choice: str) -> List[str]:
    if choice == "all":
        return list(TABLE_CONFIGS.keys())
    return [choice]


def resolve_table_ids(client: BitableClient) -> Dict[str, str]:
    tables = client.list_tables()
    by_name = {table.get("name", ""): table.get("table_id", "") for table in tables}
    resolved: Dict[str, str] = {}
    missing = []
    for table_key, config in TABLE_CONFIGS.items():
        table_id = by_name.get(config["table_name"])
        if not table_id:
            missing.append(config["table_name"])
        else:
            resolved[table_key] = table_id
    if missing:
        raise RuntimeError(f"飞书多维表格缺少数据表: {missing}")
    return resolved


def list_field_meta(client: BitableClient, table_id: str) -> Dict[str, Dict[str, Any]]:
    return {
        field.get("field_name", ""): field
        for field in client.list_fields(table_id)
        if field.get("field_name")
    }


def load_source_rows(workbook_path: Path, sheet_name: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    df = pd.read_excel(workbook_path, sheet_name=sheet_name, dtype=object)
    if limit is not None:
        df = df.head(limit)
    df = df.where(pd.notna(df), None)
    return df.to_dict(orient="records")


def load_table_source_rows(
    workbook_path: Path,
    table_key: str,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Load a configured sheet while accepting legacy three-sheet summaries."""
    config = TABLE_CONFIGS[table_key]
    try:
        return load_source_rows(workbook_path, config["sheet"], limit=limit)
    except ValueError as exc:
        # ``search_tasks`` did not exist in historical summary workbooks.
        # Treat only that newly introduced sheet as empty; missing legacy
        # sheets remain errors exactly as before.
        if table_key == "search_tasks" and "Worksheet named" in str(exc):
            return []
        raise


def normalize_source_row(table_key: str, source_row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one current/legacy Excel row to canonical Feishu headers."""
    config = TABLE_CONFIGS[table_key]
    normalized = dict(source_row)

    # Seed every standard column so a new workbook and a legacy workbook take
    # the same downstream path.  Exact standard headers always win.
    for canonical_header in config["field_map"]:
        normalized[canonical_header] = source_row.get(canonical_header)

    for legacy_header, canonical_header in config.get("header_aliases", {}).items():
        if is_blank(normalized.get(canonical_header)):
            legacy_value = source_row.get(legacy_header)
            if not is_blank(legacy_value):
                normalized[canonical_header] = legacy_value
        if legacy_header != canonical_header:
            normalized.pop(legacy_header, None)

    # Fill persisted workflow keys missing from historical workbooks.
    if table_key == "search_tasks" and is_blank(normalized.get("唯一键")):
        keyword = clean_text(normalized.get("搜索关键词"))
        if keyword:
            normalized["唯一键"] = keyword
    elif table_key == "search_videos" and is_blank(normalized.get("唯一键")):
        video_id = clean_text(normalized.get("Video ID"))
        if video_id:
            normalized["唯一键"] = f"{clean_text(normalized.get('搜索关键词'))}|{video_id}"
    elif table_key == "influencer_videos" and is_blank(normalized.get("唯一键")):
        video_id = clean_text(normalized.get("Video ID"))
        if video_id:
            normalized["唯一键"] = video_id

    return normalized


def normalize_source_rows(table_key: str, source_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [normalize_source_row(table_key, row) for row in source_rows]


def apply_channel_descriptions(
    table_key: str,
    source_rows: List[Dict[str, Any]],
    descriptions: Optional[Dict[str, str]] = None,
) -> None:
    """Fill blank source descriptions in-memory before syncing influencers."""
    if table_key != INFLUENCERS_TABLE_KEY or not descriptions:
        return
    for row in source_rows:
        channel_id = clean_text(row.get("Channel ID"))
        if not channel_id:
            continue
        if normalize_text(row.get(DESCRIPTION_FIELD)):
            continue
        description = normalize_text(descriptions.get(channel_id, ""))
        if description:
            row[DESCRIPTION_FIELD] = description


def fetch_source_channel_descriptions(
    workbook_path: Path,
    *,
    youtube_api_key: str = "",
) -> Dict[str, Any]:
    """Fetch YouTube descriptions for influencer rows whose local description is blank."""
    api_key = youtube_api_key or YouTubeConfig().api_key
    if not api_key:
        raise RuntimeError("未找到 YOUTUBE_API_KEY，请检查 .env 或环境变量。")

    rows = normalize_source_rows(
        INFLUENCERS_TABLE_KEY,
        load_source_rows(workbook_path, TABLE_CONFIGS[INFLUENCERS_TABLE_KEY]["sheet"]),
    )
    channel_ids = []
    seen = set()
    for row in rows:
        channel_id = clean_text(row.get("Channel ID"))
        if not channel_id or channel_id in seen:
            continue
        if normalize_text(row.get(DESCRIPTION_FIELD)):
            continue
        seen.add(channel_id)
        channel_ids.append(channel_id)

    descriptions: Dict[str, str] = {}
    if channel_ids:
        channels, error = fetch_channel_details(api_key, channel_ids)
        if error:
            raise RuntimeError(f"YouTube 频道详情获取失败: {error}")
        descriptions = {
            channel.get("channel_id", ""): normalize_text(channel.get("channel_description", ""))
            for channel in channels
            if channel.get("channel_id")
        }

    return {
        "requested_channel_ids": len(channel_ids),
        "returned_channels": len(descriptions),
        "empty_descriptions": sum(1 for text in descriptions.values() if not text),
        "descriptions": descriptions,
    }


def source_key_column(config: Dict[str, Any]) -> str:
    if config.get("key_column"):
        return str(config["key_column"])
    reverse_map = {target: source for source, target in config["field_map"].items()}
    return reverse_map[config["key_field"]]


def is_empty_existing_record(record: Dict[str, Any], key_field: str) -> bool:
    fields = record.get("fields") or {}
    if field_to_text(fields.get(key_field)):
        return False
    for value in fields.values():
        if is_blank(value):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
            continue
        return False
    return True


def should_preserve_existing_value(
    target_field: str,
    existing_value: Any,
    incoming_value: Any,
) -> bool:
    """Return whether a manually curated value must survive workbook sync.

    A non-placeholder KOL name may have been confirmed by a human. It must not
    be replaced by another automatic guess, a blank value, or ``手动确认``.
    The placeholder itself remains replaceable once the workflow finds a
    confident name.
    """
    if target_field != KOL_NAME_FIELD:
        return False
    existing_text = normalize_text(field_to_text(existing_value))
    return bool(existing_text and existing_text != KOL_NAME_MANUAL_CONFIRMATION)


def build_fields(
    source_row: Dict[str, Any],
    config: Dict[str, Any],
    field_meta: Dict[str, Dict[str, Any]],
    existing_fields: Optional[Dict[str, Any]] = None,
    only_target_fields: Optional[set] = None,
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    existing_fields = existing_fields or {}
    preserve_nonblank = config.get("preserve_existing_nonblank", set())
    read_only_fields = config.get("read_only_fields", set())

    for source_column, target_field in config["field_map"].items():
        if only_target_fields and target_field not in only_target_fields:
            continue
        if target_field in read_only_fields:
            continue
        if target_field not in field_meta:
            continue
        if target_field in preserve_nonblank and field_to_text(existing_fields.get(target_field)):
            continue
        if should_preserve_existing_value(
            target_field,
            existing_fields.get(target_field),
            source_row.get(source_column),
        ):
            continue
        formatted = format_value(source_row.get(source_column), target_field, field_meta[target_field])
        if formatted is None or is_blank(formatted):
            continue
        fields[target_field] = formatted

    return fields


def plan_table(
    client: BitableClient,
    workbook_path: Path,
    table_key: str,
    table_id: str,
    *,
    limit: Optional[int] = None,
    channel_descriptions: Optional[Dict[str, str]] = None,
    only_target_fields: Optional[set] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    config = TABLE_CONFIGS[table_key]
    field_meta = list_field_meta(client, table_id)
    read_only_fields = config.get("read_only_fields", set())
    required_fields = set(config["field_map"].values()) - set(read_only_fields)
    if only_target_fields:
        required_fields = (set(only_target_fields) - set(read_only_fields)) | {config["key_field"]}
    missing_fields = [
        target
        for target in required_fields
        if target not in field_meta
    ]
    if missing_fields:
        raise RuntimeError(f"{config['table_name']} 缺少字段: {missing_fields}")

    source_rows = normalize_source_rows(
        table_key,
        load_table_source_rows(workbook_path, table_key, limit=limit),
    )
    apply_channel_descriptions(table_key, source_rows, channel_descriptions)
    existing_records = client.get_all_records(table_id)
    key_field = config["key_field"]
    key_column = source_key_column(config)
    reuse_blank_records = config.get("reuse_blank_records", True)

    existing_by_key: Dict[str, Dict[str, Any]] = {}
    duplicate_existing_keys = 0
    blank_records: List[Dict[str, Any]] = []
    for record in existing_records:
        fields = record.get("fields") or {}
        key = field_to_text(fields.get(key_field))
        if key:
            if key in existing_by_key:
                duplicate_existing_keys += 1
            else:
                existing_by_key[key] = record
        elif is_empty_existing_record(record, key_field):
            blank_records.append(record)

    updates: List[Dict[str, Any]] = []
    creates: List[Dict[str, Any]] = []
    source_seen_keys = set()
    duplicate_source_keys = 0
    skipped_blank_key = 0
    skipped_missing_existing = 0
    blank_index = 0

    for row in source_rows:
        key = clean_text(row.get(key_column))
        if not key:
            skipped_blank_key += 1
            continue
        if key in source_seen_keys:
            duplicate_source_keys += 1
            continue
        source_seen_keys.add(key)

        if key in existing_by_key:
            existing = existing_by_key[key]
            fields = build_fields(
                row,
                config,
                field_meta,
                existing.get("fields") or {},
                only_target_fields=only_target_fields,
            )
            if fields:
                updates.append({"record_id": existing["record_id"], "fields": fields})
            continue

        if only_target_fields:
            skipped_missing_existing += 1
            continue

        fields = build_fields(row, config, field_meta, only_target_fields=only_target_fields)
        if not fields:
            skipped_blank_key += 1
            continue

        if reuse_blank_records and blank_index < len(blank_records):
            updates.append({"record_id": blank_records[blank_index]["record_id"], "fields": fields})
            blank_index += 1
        else:
            creates.append({"fields": fields})

    stats = {
        "table_key": table_key,
        "table_name": config["table_name"],
        "table_id": table_id,
        "source_rows": len(source_rows),
        "unique_source_keys": len(source_seen_keys),
        "existing_records_before": len(existing_records),
        "existing_keys_before": len(existing_by_key),
        "blank_records_available": len(blank_records),
        "blank_records_reused": blank_index,
        "reuse_blank_records": reuse_blank_records,
        "blank_records_left_unreused": len(blank_records) - blank_index,
        "planned_updates": len(updates),
        "planned_creates": len(creates),
        "duplicate_source_keys": duplicate_source_keys,
        "duplicate_existing_keys": duplicate_existing_keys,
        "skipped_blank_key": skipped_blank_key,
        "skipped_missing_existing": skipped_missing_existing,
        "only_target_fields": sorted(only_target_fields) if only_target_fields else [],
        "unmapped_source_columns": [
            column
            for column in source_rows[0].keys()
            if column not in config["field_map"]
        ] if source_rows else [],
    }
    return stats, updates, creates


def _post_batch(client: BitableClient, table_id: str, endpoint: str, records: List[Dict[str, Any]]) -> int:
    if endpoint == "batch_create":
        return client.batch_create_records(table_id, records)
    if endpoint == "batch_update":
        try:
            return client.batch_update_records(table_id, records)
        except RuntimeError:
            # Retain compatibility with Bases that reject batch update while
            # accepting the single-record endpoint.
            return _put_records_one_by_one(client, table_id, records)
    raise ValueError(f"不支持的飞书批量端点: {endpoint}")


def _put_records_one_by_one(client: BitableClient, table_id: str, records: List[Dict[str, Any]]) -> int:
    """Fallback for bases that reject batch_update but accept single-record PUT."""
    total = 0
    for record in records:
        record_id = record.get("record_id", "")
        fields = record.get("fields") or {}
        if not record_id or not fields:
            continue
        client.update_record(table_id, record_id, fields)
        total += 1
        time.sleep(REQUEST_SLEEP_SECONDS)
    return total


def sync_table(
    client: BitableClient,
    workbook_path: Path,
    table_key: str,
    table_id: str,
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
    channel_descriptions: Optional[Dict[str, str]] = None,
    only_target_fields: Optional[set] = None,
) -> Dict[str, Any]:
    stats, updates, creates = plan_table(
        client,
        workbook_path,
        table_key,
        table_id,
        limit=limit,
        channel_descriptions=channel_descriptions,
        only_target_fields=only_target_fields,
    )
    if dry_run:
        stats.update({"actual_updates": 0, "actual_creates": 0, "dry_run": True})
        return stats

    stats["actual_updates"] = _post_batch(client, table_id, "batch_update", updates) if updates else 0
    stats["actual_creates"] = _post_batch(client, table_id, "batch_create", creates) if creates else 0
    stats["existing_records_after"] = len(client.get_all_records(table_id))
    stats["dry_run"] = False
    return stats


def comparable_value(value: Any, field_meta: Dict[str, Any]) -> Any:
    field_type = field_meta.get("type")
    ui_type = field_meta.get("ui_type")
    if field_type == 15 or ui_type == "Url":
        return normalize_text(field_to_text(value))
    if field_type == 2 or ui_type == "Number":
        return number_value(value)
    if field_type == 5 or ui_type == "DateTime":
        return number_value(value)
    if field_type == 7 or ui_type == "Checkbox":
        return bool_value(value)
    return normalize_text(field_to_text(value))


def expected_value(value: Any, target_field: str, field_meta: Dict[str, Any]) -> Any:
    formatted = format_value(value, target_field, field_meta)
    if formatted is None or is_blank(formatted):
        return None
    return comparable_value(formatted, field_meta)


def verify_table(
    client: BitableClient,
    workbook_path: Path,
    table_key: str,
    table_id: str,
    *,
    limit: Optional[int] = None,
    channel_descriptions: Optional[Dict[str, str]] = None,
    only_target_fields: Optional[set] = None,
) -> Dict[str, Any]:
    config = TABLE_CONFIGS[table_key]
    field_meta = list_field_meta(client, table_id)
    source_rows = normalize_source_rows(
        table_key,
        load_table_source_rows(workbook_path, table_key, limit=limit),
    )
    apply_channel_descriptions(table_key, source_rows, channel_descriptions)
    key_column = source_key_column(config)
    records = client.get_all_records(table_id)

    records_by_key: Dict[str, Dict[str, Any]] = {}
    for record in records:
        fields = record.get("fields") or {}
        key = field_to_text(fields.get(config["key_field"]))
        if key and key not in records_by_key:
            records_by_key[key] = record

    checked = 0
    seen_keys = set()
    missing_keys: List[str] = []
    mismatches: List[Dict[str, Any]] = []
    preserve_nonblank = config.get("preserve_existing_nonblank", set())
    read_only_fields = config.get("read_only_fields", set())

    for row in source_rows:
        key = clean_text(row.get(key_column))
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        record = records_by_key.get(key)
        if not record:
            if only_target_fields:
                continue
            missing_keys.append(key)
            continue

        fields = record.get("fields") or {}
        if field_to_text(fields.get(PRIMARY_FIELD)):
            mismatches.append({"key": key, "field": PRIMARY_FIELD, "issue": "primary field is not blank"})

        for source_column, target_field in config["field_map"].items():
            if only_target_fields and target_field not in only_target_fields:
                continue
            if target_field in read_only_fields:
                continue
            if target_field not in field_meta:
                continue
            if target_field in preserve_nonblank:
                continue
            if should_preserve_existing_value(
                target_field,
                fields.get(target_field),
                row.get(source_column),
            ):
                continue
            expected = expected_value(row.get(source_column), target_field, field_meta[target_field])
            if expected is None:
                continue
            actual = comparable_value(fields.get(target_field), field_meta[target_field])
            if actual != expected:
                mismatches.append(
                    {
                        "key": key,
                        "field": target_field,
                        "expected": expected,
                        "actual": actual,
                    }
                )
                if len(mismatches) >= 10:
                    break
        checked += 1
        if len(mismatches) >= 10:
            break

    return {
        "table_key": table_key,
        "table_name": config["table_name"],
        "verified_source_rows": len(source_rows),
        "verified_unique_keys": checked,
        "missing_keys": missing_keys[:10],
        "mismatches": mismatches,
        "passed": not missing_keys and not mismatches,
    }


def clear_primary_field(
    client: BitableClient,
    table_key: str,
    table_id: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    field_meta = list_field_meta(client, table_id)
    config = TABLE_CONFIGS[table_key]
    if PRIMARY_FIELD not in field_meta:
        return {
            "table_key": table_key,
            "table_name": config["table_name"],
            "primary_field_present": False,
            "planned_primary_clears": 0,
            "actual_primary_clears": 0,
        }

    records = client.get_all_records(table_id)
    updates = [
        {"record_id": record["record_id"], "fields": {PRIMARY_FIELD: ""}}
        for record in records
        if field_to_text((record.get("fields") or {}).get(PRIMARY_FIELD))
    ]
    cleared = 0 if dry_run else _post_batch(client, table_id, "batch_update", updates) if updates else 0
    return {
        "table_key": table_key,
        "table_name": config["table_name"],
        "primary_field_present": True,
        "planned_primary_clears": len(updates),
        "actual_primary_clears": cleared,
        "dry_run": dry_run,
    }


def run_phase(
    client: BitableClient,
    workbook_path: Path,
    table_ids: Dict[str, str],
    table_keys: List[str],
    *,
    phase: str,
    limit: Optional[int],
    dry_run: bool,
    channel_descriptions: Optional[Dict[str, str]] = None,
    only_target_fields: Optional[set] = None,
) -> Dict[str, Any]:
    sync_results = []
    verify_results = []
    for table_key in table_keys:
        table_id = table_ids[table_key]
        sync_result = sync_table(
            client,
            workbook_path,
            table_key,
            table_id,
            limit=limit,
            dry_run=dry_run,
            channel_descriptions=channel_descriptions,
            only_target_fields=only_target_fields,
        )
        sync_results.append(sync_result)
        if not dry_run:
            verify_result = verify_table(
                client,
                workbook_path,
                table_key,
                table_id,
                limit=limit,
                channel_descriptions=channel_descriptions,
                only_target_fields=only_target_fields,
            )
            verify_results.append(verify_result)
            if not verify_result["passed"]:
                raise RuntimeError(f"{phase} 回读校验失败: {json.dumps(verify_result, ensure_ascii=False)}")

    return {
        "phase": phase,
        "limit": limit,
        "dry_run": dry_run,
        "sync_results": sync_results,
        "verify_results": verify_results,
    }


def sync_workbook_to_feishu(
    workbook_path: str,
    *,
    app_token: str = "",
    app_id: str = "",
    app_secret: str = "",
    table: str = "all",
    dry_run: bool = False,
    skip_test: bool = False,
    test_only: bool = False,
    test_limit: int = 10,
    cleanup_empty_rows: bool = False,
    clear_primary: bool = False,
    fill_channel_descriptions: bool = False,
    description_only: bool = False,
    youtube_api_key: str = "",
    stats_path: Optional[str] = None,
    client: Optional[BitableClient] = None,
) -> Dict[str, Any]:
    workbook = Path(workbook_path).resolve()
    if not workbook.exists():
        raise FileNotFoundError(workbook)

    created_context = None
    if client is None:
        feishu_config = FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            app_token=FeishuConfig.extract_app_token(app_token),
        )
        context = create_bitable_client_from_config(
            feishu_config,
            create_base_if_missing=not dry_run,
            progress=lambda message: print(f"[飞书] {message}"),
        )
        client = context.client
        app_token = context.app_token
        created_context = context
    app_token = app_token or getattr(client, "app_token", "")
    if created_context is not None:
        initialize_created_base_schema(created_context)
    table_ids = resolve_table_ids(client)
    table_keys = table_keys_from_choice(table)
    if description_only:
        if table_keys != [INFLUENCERS_TABLE_KEY]:
            raise RuntimeError("--description-only 只能与 --table influencers 一起使用")
        if not fill_channel_descriptions:
            raise RuntimeError("--description-only 需要同时使用 --fill-channel-descriptions")

    description_fetch_stats: Dict[str, Any] = {}
    channel_descriptions: Dict[str, str] = {}
    if fill_channel_descriptions:
        if INFLUENCERS_TABLE_KEY not in table_keys:
            raise RuntimeError("--fill-channel-descriptions 只能用于包含 influencers 的同步")
        description_fetch_stats = fetch_source_channel_descriptions(
            workbook,
            youtube_api_key=youtube_api_key,
        )
        channel_descriptions = description_fetch_stats.pop("descriptions", {})

    only_target_fields = {DESCRIPTION_FIELD} if description_only else None
    started_at = datetime.now().isoformat(timespec="seconds")

    phases = []
    if clear_primary:
        phases.append(
            {
                "phase": "clear_primary",
                "results": [
                    clear_primary_field(client, table_key, table_ids[table_key], dry_run=dry_run)
                    for table_key in table_keys
                ],
            }
        )
    if not skip_test:
        phases.append(
            run_phase(
                client,
                workbook,
                table_ids,
                table_keys,
                phase="test_rows",
                limit=test_limit,
                dry_run=dry_run,
                channel_descriptions=channel_descriptions,
                only_target_fields=only_target_fields,
            )
        )
    if not test_only:
        phases.append(
            run_phase(
                client,
                workbook,
                table_ids,
                table_keys,
                phase="full_sync",
                limit=None,
                dry_run=dry_run,
                channel_descriptions=channel_descriptions,
                only_target_fields=only_target_fields,
            )
        )

    cleanup_results = []
    if cleanup_empty_rows:
        cleanup_table_names = [TABLE_CONFIGS[table_key]["table_name"] for table_key in table_keys]
        cleanup_results = cleanup_empty_records(client, cleanup_table_names, dry_run=dry_run)

    payload = {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "workbook": str(workbook),
        "app_token": mask_token(app_token),
        "tables": table_keys,
        "channel_descriptions": description_fetch_stats,
        "phases": phases,
        "cleanup_empty_rows": cleanup_results,
    }

    if stats_path:
        stats = Path(stats_path).resolve()
        stats.parent.mkdir(parents=True, exist_ok=True)
        stats.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["stats_path"] = str(stats)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将 KOL 汇总工作簿同步到飞书多维表格")
    parser.add_argument("--workbook", required=True, help="汇总 xlsx 文件")
    parser.add_argument(
        "--feishu-app-token",
        default="",
        help="飞书多维表格 app_token 或 URL；不填则复用或自动创建",
    )
    parser.add_argument("--table", choices=["all", *TABLE_CONFIGS.keys()], default="all")
    parser.add_argument("--dry-run", action="store_true", help="只生成计划，不写入")
    parser.add_argument("--skip-test", action="store_true", help="跳过 10 条测试，直接执行")
    parser.add_argument("--test-only", action="store_true", help="只执行测试写入")
    parser.add_argument("--test-limit", type=int, default=10, help="测试行数")
    parser.add_argument("--cleanup-empty-rows", action="store_true", help="同步后删除全空行")
    parser.add_argument("--clear-primary", action="store_true", help="同步前清空 A 列主字段")
    parser.add_argument("--fill-channel-descriptions", action="store_true", help="同步 influencers 时调用 YouTube API 补空频道描述")
    parser.add_argument("--description-only", action="store_true", help="只更新 influencers 表的频道描述字段")
    parser.add_argument("--youtube-api-key", default="", help="YouTube API Key；不填则读取环境变量")
    parser.add_argument("--stats", default="", help="统计 JSON 输出路径")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    feishu_cfg = FeishuConfig()
    feishu_cfg.app_token = FeishuConfig.extract_app_token(args.feishu_app_token)
    payload = sync_workbook_to_feishu(
        args.workbook,
        app_token=feishu_cfg.app_token,
        app_id=feishu_cfg.app_id,
        app_secret=feishu_cfg.app_secret,
        table=args.table,
        dry_run=args.dry_run,
        skip_test=args.skip_test,
        test_only=args.test_only,
        test_limit=args.test_limit,
        cleanup_empty_rows=args.cleanup_empty_rows,
        clear_primary=args.clear_primary,
        fill_channel_descriptions=args.fill_channel_descriptions,
        description_only=args.description_only,
        youtube_api_key=args.youtube_api_key,
        stats_path=args.stats or None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
