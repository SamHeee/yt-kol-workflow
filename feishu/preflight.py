#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu preflight checks before running the YouTube workflow."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .auth import FeishuAuth
from .bitable import BitableClient
from .schema import (
    INFLUENCERS_FIELDS,
    INFLUENCERS_TABLE,
    INFLUENCER_VIDEOS_FIELDS,
    INFLUENCER_VIDEOS_TABLE,
    SEARCH_TASKS_FIELDS,
    SEARCH_TASKS_TABLE,
    SEARCH_VIDEOS_FIELDS,
    SEARCH_VIDEOS_TABLE,
    SchemaManager,
    format_influencer_record,
    format_influencer_video_record,
    format_search_task_record,
    format_search_video_record,
)

logger = logging.getLogger("kol_workflow.feishu.preflight")

TABLE_SCHEMAS: List[Tuple[str, List[Tuple[str, int]]]] = [
    (SEARCH_TASKS_TABLE, SEARCH_TASKS_FIELDS),
    (SEARCH_VIDEOS_TABLE, SEARCH_VIDEOS_FIELDS),
    (INFLUENCERS_TABLE, INFLUENCERS_FIELDS),
    (INFLUENCER_VIDEOS_TABLE, INFLUENCER_VIDEOS_FIELDS),
]

CREATED_TIME_FIELDS = {
    SEARCH_VIDEOS_TABLE: "视频记录日期",
    INFLUENCERS_TABLE: "网红记录日期",
}


class FeishuHealthCheck:
    """Feishu Bitable preflight checker for workflow use.

    Usage:
        checker = FeishuHealthCheck(app_id, app_secret, app_token)
        checker = FeishuHealthCheck(client=cli_bitable_client)
        client, schema_mgr = checker.run()
    """

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        app_token: str = "",
        *,
        client: Optional[BitableClient] = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self._injected_client = client is not None
        self.client = client or BitableClient(app_token, FeishuAuth(app_id, app_secret))
        self.app_token = app_token or getattr(self.client, "app_token", "")

    # ---- Individual checks ----

    def check_config(self) -> bool:
        """Validate credentials for legacy mode or only the target for CLI mode.

        Raises RuntimeError on failure.
        Returns True on success.
        """
        missing = []
        if not self._injected_client:
            if not self.app_id:
                missing.append("APP_ID")
            if not self.app_secret:
                missing.append("APP_SECRET")
        if not self.app_token:
            missing.append("app_token")
        if missing:
            raise RuntimeError(f"飞书配置缺失: {', '.join(missing)}")
        return True

    def check_connection(self) -> dict:
        """Check app info (name, status) via API.

        Returns {"name": str, "status": str}.
        Raises RuntimeError on failure.
        """
        tables = self.client.list_tables()
        return {
            "name": "ok",
            "status": "ok",
            "table_count": len(tables),
        }

    def check_permissions_write(self) -> bool:
        """Verify write permission by creating + deleting a test table.

        Returns True on success.
        Raises RuntimeError on failure.
        """
        # Create test table
        test_table_name = "_权限测试表"
        table_id = self.client.create_table(test_table_name)
        if not table_id:
            raise RuntimeError("创建测试表失败，无法验证写入权限")

        # Delete through the client transport.  This is important for CLI user
        # auth, where there is intentionally no token exposed to Python.
        try:
            self.client.delete_table(table_id)
        except Exception as e:
            logger.warning(f"清理测试表异常: {e}")

        return True

    def check_schema(self) -> Dict[str, str]:
        """Ensure all 4 tables exist with correct fields.

        Returns {table_name: table_id} mapping.
        Raises RuntimeError on failure.
        """
        schema_mgr = SchemaManager(self.client)
        table_ids = schema_mgr.ensure_all_tables()
        return table_ids

    def check_fields(self, table_ids: Dict[str, str]) -> bool:
        """Validate field types match schema.py definitions.

        Returns True on success.
        Raises RuntimeError on mismatch.
        """
        _validate_required_fields(self.client, table_ids)
        return True

    def check_write_permission_records(self, schema_mgr: SchemaManager) -> bool:
        """Write 1 test record to each of the 4 tables, then clean up.

        Returns True on success.
        Raises RuntimeError on failure.
        """
        _write_and_cleanup_test_records(self.client, schema_mgr)
        return True

    # ---- Orchestration ----

    def run(self) -> Tuple[BitableClient, SchemaManager]:
        """Run all preflight checks silently.

        Returns (BitableClient, SchemaManager) on full success.
        Raises RuntimeError on any check failure.
        """
        # 1. Config
        self.check_config()

        # 2. Connection
        self.check_connection()

        # 3. Write permission via test table
        self.check_permissions_write()

        # 4. Ensure schema (tables + fields)
        table_ids = self.check_schema()

        # 5. Validate field types
        self.check_fields(table_ids)

        # 6. Write test records and cleanup
        client = self.client
        schema_mgr = SchemaManager(client)
        schema_mgr.ensure_all_tables()  # Ensure table_ids are populated
        self.check_write_permission_records(schema_mgr)

        logger.info("飞书多维表格预检通过")
        return client, schema_mgr


def run_feishu_preflight(
    app_id: str = "",
    app_secret: str = "",
    app_token: str = "",
    *,
    client: Optional[BitableClient] = None,
) -> Tuple[BitableClient, SchemaManager]:
    """Ensure Feishu tables/fields are ready, then write and delete test records.

    This is a convenience wrapper around FeishuHealthCheck.run().
    """
    return FeishuHealthCheck(
        app_id,
        app_secret,
        app_token,
        client=client,
    ).run()


# ============================================================================
# Internal helpers
# ============================================================================

def _validate_required_fields(client: BitableClient, table_ids: Dict[str, str]):
    for table_name, field_specs in TABLE_SCHEMAS:
        table_id = table_ids.get(table_name, "")
        if not table_id:
            raise RuntimeError(f"飞书数据表缺失: {table_name}")

        fields = client.list_fields(table_id)
        actual = {f.get("field_name", ""): f.get("type") for f in fields}
        missing = []
        mismatched = []

        for field_name, expected_type in field_specs:
            if field_name not in actual:
                missing.append(field_name)
                continue
            if actual[field_name] != expected_type:
                mismatched.append(
                    f"{field_name}(当前{actual[field_name]}, 期望{expected_type})"
                )

        if missing or mismatched:
            detail = []
            if missing:
                detail.append(f"缺失字段: {', '.join(missing)}")
            if mismatched:
                detail.append(f"字段类型不一致: {', '.join(mismatched)}")
            raise RuntimeError(f"飞书数据表字段异常: {table_name}; {'; '.join(detail)}")


def _write_and_cleanup_test_records(client: BitableClient, schema_mgr: SchemaManager):
    marker = datetime.now().strftime("__KOL_PREFLIGHT_TEST__%Y%m%d_%H%M%S")
    test_records = _build_test_records(marker)
    created_by_table: Dict[str, List[str]] = {}

    try:
        for table_name, records in test_records.items():
            table_id = schema_mgr.get_table_id(table_name)
            created = client.batch_create_records_with_ids(table_id, records)
            record_ids = [r.get("record_id", "") for r in created if r.get("record_id")]
            if len(record_ids) != len(records):
                raise RuntimeError(f"飞书测试写入返回记录数异常: {table_name}")
            created_by_table[table_id] = record_ids
            _validate_created_time_field(
                client,
                table_name,
                table_id,
                record_ids,
            )
            logger.debug(f"飞书预检测试写入: {table_name} {len(record_ids)} 条")
    finally:
        cleanup_errors = []
        for table_id, record_ids in created_by_table.items():
            try:
                client.batch_delete_records(table_id, record_ids)
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if cleanup_errors:
            raise RuntimeError("飞书测试数据清理失败: " + "; ".join(cleanup_errors))


def _validate_created_time_field(
    client: BitableClient,
    table_name: str,
    table_id: str,
    record_ids: List[str],
) -> None:
    """Read back and validate a deployed system-created-time field.

    The conditional field lookup keeps this helper rollout-safe: calling it
    against a production table before the new field has been deployed must not
    fail the otherwise valid record-write preflight.  Once the field exists,
    however, both its named cell and Feishu's automatic ``created_time`` audit
    value are required.
    """
    field_name = CREATED_TIME_FIELDS.get(table_name)
    if not field_name:
        return

    fields = {
        field.get("field_name", ""): field
        for field in client.list_fields(table_id)
        if field.get("field_name")
    }
    field = fields.get(field_name)
    if not field:
        logger.info(f"飞书预检跳过创建时间回读（字段尚未部署）: {table_name}.{field_name}")
        return
    if field.get("type") != 1001:
        raise RuntimeError(
            f"飞书创建时间字段类型异常: {table_name}.{field_name}="
            f"{field.get('type')}，期望1001"
        )

    records = client.batch_get_records(
        table_id,
        record_ids,
        automatic_fields=True,
    )
    records_by_id = {
        record.get("record_id", ""): record
        for record in records
        if record.get("record_id")
    }
    for record_id in record_ids:
        record = records_by_id.get(record_id)
        if not record:
            raise RuntimeError(f"飞书创建时间回读缺少测试记录: {table_name} {record_id}")
        created_time = record.get("created_time")
        field_value = (record.get("fields") or {}).get(field_name)
        if not created_time or not field_value:
            raise RuntimeError(
                f"飞书创建时间字段为空: {table_name}.{field_name} {record_id}"
            )


def _build_test_records(marker: str) -> Dict[str, List[dict]]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    video = {
        "video_id": marker,
        "video_url": "https://www.youtube.com/watch?v=test",
        "channel_id": marker,
        "channel_title": "KOL Preflight Test Channel",
        "title": "KOL Preflight Test Video",
        "published_at": now,
        "tags": "preflight,test",
        "view_count": 12345,
        "like_count": 123,
        "comment_count": 12,
        "engagement_rate": 1.09,
        "duration_seconds": 60,
        "duration_hms": "00:01:00",
        "has_caption": "false",
        "is_qualified": True,
        "filter_reason": "预检测试",
    }
    channel = {
        "channel_id": marker,
        "channel_title": "KOL Preflight Test Channel",
        "kol_name": "Sarah",
        "channel_url": "https://www.youtube.com/channel/test",
        "channel_description": "KOL workflow Feishu preflight test record.",
        "channel_initial_assessment": (
            "领域=户外/露营; 内容=产品测评,教程; "
            "主体=个人创作者; 自有品牌=疑似"
        ),
        "latest_published_at": now,
        "activity_status": "持续更新",
        "rep_video_title": "KOL Preflight Test Video",
        "subscriber_count": 100,
        "total_video_count": 10,
        "total_view_count": 1000,
        "channel_created_at": now,
        "country": "US",
        "contact_email": "",
    }

    return {
        SEARCH_TASKS_TABLE: [
            format_search_task_record({
                "keyword": marker,
                "sort_order": "relevance",
                "region": "US",
                "search_time": now,
                "result_count": 1,
                "qualified_count": 1,
                "unique_channels": 1,
                "new_channels": 1,
                "quota_used": 0,
                "status": "预检测试",
                "note": marker,
            })
        ],
        SEARCH_VIDEOS_TABLE: [format_search_video_record(video, marker)],
        INFLUENCERS_TABLE: [format_influencer_record(channel, video, marker)],
        INFLUENCER_VIDEOS_TABLE: [format_influencer_video_record(video)],
    }
