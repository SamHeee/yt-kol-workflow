#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Utilities for removing fully empty Feishu Bitable records."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import FeishuConfig
from feishu.bitable import BitableClient
from feishu.client_factory import (
    create_bitable_client_from_config,
    initialize_created_base_schema,
)
from feishu.value_utils import is_blank


DEFAULT_TABLE_NAMES = ["搜索任务表", "视频数据表", "网红详情表", "网红视频表"]
AUTOMATIC_FIELD_TYPES = {1001, 1002, 1003, 1004, 1005}


def mask_token(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def is_record_empty(
    record: Dict[str, Any],
    ignored_fields: Optional[Iterable[str]] = None,
) -> bool:
    """Return whether all non-automatic field values are blank."""
    fields = record.get("fields") or {}
    if not fields:
        return True
    ignored = set(ignored_fields or [])
    return all(
        is_blank(value)
        for field_name, value in fields.items()
        if field_name not in ignored
    )


def _automatic_field_names(client: BitableClient, table_id: str) -> Set[str]:
    """Resolve read-only automatic fields that must not make a row non-empty."""
    return {
        field.get("field_name", "")
        for field in client.list_fields(table_id)
        if field.get("field_name") and field.get("type") in AUTOMATIC_FIELD_TYPES
    }


def _resolve_tables(client: BitableClient, table_names: Optional[Iterable[str]] = None) -> List[Dict[str, str]]:
    wanted = list(table_names or DEFAULT_TABLE_NAMES)
    tables = client.list_tables()
    by_name = {table.get("name", ""): table.get("table_id", "") for table in tables}
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise RuntimeError(f"飞书多维表格缺少数据表: {missing}")
    return [{"name": name, "table_id": by_name[name]} for name in wanted]


def cleanup_empty_records(
    client: BitableClient,
    table_names: Optional[Iterable[str]] = None,
    *,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Delete fully empty rows from selected tables."""
    results = []
    for table in _resolve_tables(client, table_names):
        automatic_fields = _automatic_field_names(client, table["table_id"])
        records_before = client.get_all_records(table["table_id"])
        empty_ids = [
            record.get("record_id", "")
            for record in records_before
            if is_record_empty(record, automatic_fields)
        ]
        empty_ids = [record_id for record_id in empty_ids if record_id]
        deleted = 0 if dry_run else client.batch_delete_records(table["table_id"], empty_ids)
        records_after = client.get_all_records(table["table_id"]) if not dry_run else records_before
        empty_after = [
            record.get("record_id", "")
            for record in records_after
            if is_record_empty(record, automatic_fields)
        ]
        results.append(
            {
                "table_name": table["name"],
                "table_id": table["table_id"],
                "records_before": len(records_before),
                "empty_rows_before": len(empty_ids),
                "deleted_empty_rows": deleted,
                "records_after": len(records_after),
                "empty_rows_after": len(empty_after) if not dry_run else len(empty_ids),
                "automatic_fields_ignored": sorted(automatic_fields),
                "dry_run": dry_run,
            }
        )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="删除飞书多维表格中的全空行")
    parser.add_argument(
        "--feishu-app-token",
        default="",
        help="飞书多维表格 app_token 或 URL；不填则复用或自动创建",
    )
    parser.add_argument("--table", action="append", default=[], help="只清理指定表名，可重复传入")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    parser.add_argument("--stats", default="", help="统计 JSON 输出路径")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    feishu_cfg = FeishuConfig()
    feishu_cfg.app_token = FeishuConfig.extract_app_token(args.feishu_app_token)
    context = create_bitable_client_from_config(
        feishu_cfg,
        create_base_if_missing=not args.dry_run,
        progress=lambda message: print(f"[飞书] {message}"),
    )
    client = context.client
    feishu_cfg.app_token = context.app_token
    initialize_created_base_schema(context)

    started_at = datetime.now().isoformat(timespec="seconds")
    results = cleanup_empty_records(
        client,
        table_names=args.table or None,
        dry_run=args.dry_run,
    )
    payload = {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "app_token": mask_token(feishu_cfg.app_token),
        "results": results,
    }
    if args.stats:
        stats_path = Path(args.stats).resolve()
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
