#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu Bitable API client - table/field/record CRUD operations."""

import time
import logging
from typing import List, Dict, Tuple, Optional, Set

import requests

from .auth import FeishuAuth, FEISHU_API_BASE

logger = logging.getLogger("kol_workflow.feishu.bitable")


class BitableClient:
    """Feishu Bitable API client supporting multi-table operations."""

    def __init__(self, app_token: str, auth: FeishuAuth):
        self.app_token = app_token
        self.auth = auth

    def _url(self, path: str) -> str:
        return f"{FEISHU_API_BASE}/bitable/v1/apps/{self.app_token}{path}"

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            self._url(path),
            headers=self.auth.get_headers(),
            params=params,
            timeout=30,
        )
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        resp = requests.post(
            self._url(path),
            headers=self.auth.get_headers(),
            json=data,
            timeout=30,
        )
        return resp.json()

    def _put(self, path: str, data: dict) -> dict:
        resp = requests.put(
            self._url(path),
            headers=self.auth.get_headers(),
            json=data,
            timeout=60,
        )
        return resp.json()

    def _delete(self, path: str, data: dict = None) -> dict:
        resp = requests.delete(
            self._url(path),
            headers=self.auth.get_headers(),
            json=data,
            timeout=30,
        )
        return resp.json()

    # ======================== Tables ========================

    def list_tables(self) -> List[Dict]:
        """List all tables in the bitable."""
        result = self._get("/tables")
        if result.get("code") != 0:
            logger.error(f"列表数据表失败: {result.get('msg')}")
            return []
        return result.get("data", {}).get("items", [])

    def create_table(self, name: str) -> Optional[str]:
        """Create a new table. Returns table_id or None."""
        data = {"table": {"name": name}}
        result = self._post("/tables", data)
        if result.get("code") != 0:
            logger.error(f"创建数据表 '{name}' 失败: {result.get('msg')}")
            return None
        table_id = result.get("data", {}).get("table_id", "")
        logger.info(f"创建数据表: {name} -> {table_id}")
        return table_id

    def find_table_by_name(self, name: str) -> Optional[str]:
        """Find table_id by name. Returns None if not found."""
        for table in self.list_tables():
            if table.get("name") == name:
                return table.get("table_id")
        return None

    def get_or_create_table(self, name: str) -> str:
        """Get existing table or create new one. Returns table_id."""
        table_id = self.find_table_by_name(name)
        if table_id:
            logger.info(f"数据表已存在: {name} -> {table_id}")
            return table_id
        table_id = self.create_table(name)
        if not table_id:
            raise RuntimeError(f"无法创建数据表: {name}")
        return table_id

    def delete_table(self, table_id: str) -> bool:
        """Delete a table through the configured transport."""
        result = self._delete(f"/tables/{table_id}")
        if result.get("code") != 0:
            raise RuntimeError(
                f"删除数据表失败: {result.get('msg', 'Unknown error')} "
                f"(code={result.get('code')})"
            )
        return True

    # ======================== Fields ========================

    def list_fields(self, table_id: str) -> List[Dict]:
        """List all fields in a table."""
        result = self._get(f"/tables/{table_id}/fields")
        if result.get("code") != 0:
            logger.error(f"列表字段失败: {result.get('msg')}")
            return []
        return result.get("data", {}).get("items", [])

    def create_field(self, table_id: str, name: str, field_type: int = 1) -> Optional[str]:
        """Create a field.

        Common field types include 1=text, 2=number, 3=single_select,
        5=date, 7=checkbox, 15=url, and 1001=system created time.

        Returns field_id or None.
        """
        data = {"field_name": name, "type": field_type}
        result = self._post(f"/tables/{table_id}/fields", data)
        if result.get("code") != 0:
            # Field may already exist
            logger.debug(f"创建字段 '{name}' 返回: {result.get('msg')}")
            return None
        fid = result.get("data", {}).get("field", {}).get("field_id", "")
        logger.debug(f"创建字段: {name} -> {fid}")
        return fid

    def update_field(
        self,
        table_id: str,
        field_id: str,
        name: str,
        field_type: int,
        property: Optional[Dict] = None,
    ) -> Dict:
        """Fully update a field definition and return the updated field."""
        data: Dict = {"field_name": name, "type": field_type}
        if property is not None:
            data["property"] = property
        result = self._put(f"/tables/{table_id}/fields/{field_id}", data)
        if result.get("code") != 0:
            raise RuntimeError(
                f"更新字段 '{name}' 失败: {result.get('msg', 'Unknown error')} "
                f"(code={result.get('code')})"
            )
        return result.get("data", {}).get("field", {})

    def ensure_fields(self, table_id: str, field_specs: List[Tuple[str, int]]) -> Dict[str, str]:
        """Ensure all specified fields exist. Returns {field_name: field_id} mapping.

        field_specs: list of (field_name, field_type) tuples
        """
        existing = self.list_fields(table_id)
        name_to_id = {f.get("field_name", ""): f.get("field_id", "") for f in existing}

        for field_name, field_type in field_specs:
            if field_name not in name_to_id:
                fid = self.create_field(table_id, field_name, field_type)
                if fid:
                    name_to_id[field_name] = fid

        # Refresh to get all IDs
        existing = self.list_fields(table_id)
        return {f.get("field_name", ""): f.get("field_id", "") for f in existing}

    # ======================== Records ========================

    def get_all_records(self, table_id: str, page_size: int = 500) -> List[Dict]:
        """Get all records with pagination."""
        all_records = []
        page_token = ""

        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token

            result = self._get(f"/tables/{table_id}/records", params)
            if result.get("code") != 0:
                logger.error(f"读取记录失败: {result.get('msg')}")
                break

            records = result.get("data", {}).get("items", [])
            all_records.extend(records)

            has_more = result.get("data", {}).get("has_more", False)
            page_token = result.get("data", {}).get("page_token", "")
            if not has_more or not page_token:
                break

        return all_records

    def batch_get_records(
        self,
        table_id: str,
        record_ids: List[str],
        *,
        automatic_fields: bool = False,
    ) -> List[Dict]:
        """Fetch records by ID, optionally including system audit timestamps.

        Feishu only returns ``created_time`` and the other automatic audit
        fields when ``automatic_fields`` is explicitly requested.  Keeping
        this as a targeted batch read avoids scanning a production table just
        to verify records that were created moments earlier.
        """
        ids = [record_id for record_id in record_ids if record_id]
        if not ids:
            return []

        records: List[Dict] = []
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            data = {
                "record_ids": batch,
                "automatic_fields": automatic_fields,
            }
            result = self._post(f"/tables/{table_id}/records/batch_get", data)
            if result.get("code") != 0:
                raise RuntimeError(f"批量读取记录失败: {result.get('msg')}")
            records.extend(result.get("data", {}).get("records", []))

            if i + 100 < len(ids):
                time.sleep(0.5)

        return records

    def get_existing_values(self, table_id: str, field_name: str) -> Set[str]:
        """Get all unique values for a specific field (for dedup checks)."""
        records = self.get_all_records(table_id)
        values = set()
        for record in records:
            fields = record.get("fields", {})
            val = fields.get(field_name)
            if val:
                if isinstance(val, list):
                    # Handle Feishu's text field format which can be [{type, text}]
                    for item in val:
                        if isinstance(item, dict):
                            values.add(item.get("text", ""))
                        else:
                            values.add(str(item))
                else:
                    values.add(str(val))
        return values

    def get_record_id_map(
        self,
        table_id: str,
        field_name: str,
        fallback_key_fields: List[str] = None,
    ) -> Dict[str, str]:
        """Get {field_value: record_id} for a specific field."""
        records = self.get_all_records(table_id)
        mapping = {}
        for record in records:
            fields = record.get("fields", {})
            key = _field_to_text(fields.get(field_name))
            if not key and fallback_key_fields:
                values = [_field_to_text(fields.get(name)) for name in fallback_key_fields]
                if all(values):
                    key = "|".join(values)
            record_id = record.get("record_id", "")
            if key and record_id and key not in mapping:
                mapping[key] = record_id
        return mapping

    def get_records_by_field_values(
        self,
        table_id: str,
        field_name: str,
        values: List,
        *,
        automatic_fields: bool = False,
    ) -> Dict[str, Dict]:
        """Return records keyed by normalized values from ``field_name``.

        The lookup first resolves values to record IDs, then uses Feishu's
        batch-get endpoint.  This keeps automatic audit fields opt-in and also
        preserves the original creation time when an upsert updated an
        existing record.  ``batch_get_records`` handles Feishu's 100-record
        per-request limit.
        """
        normalized_values = []
        seen = set()
        for value in values:
            text = _field_to_text(value)
            if text and text not in seen:
                normalized_values.append(text)
                seen.add(text)
        if not normalized_values:
            return {}

        record_id_map = self.get_record_id_map(table_id, field_name)
        value_to_record_id = {
            value: record_id_map[value]
            for value in normalized_values
            if value in record_id_map
        }
        records = self.batch_get_records(
            table_id,
            list(dict.fromkeys(value_to_record_id.values())),
            automatic_fields=automatic_fields,
        )
        records_by_id = {
            record.get("record_id", ""): record
            for record in records
            if record.get("record_id")
        }
        return {
            value: records_by_id[record_id]
            for value, record_id in value_to_record_id.items()
            if record_id in records_by_id
        }

    def batch_create_records(self, table_id: str, records: List[Dict]) -> int:
        """Batch create records (max 500 per call).

        Each record should be: {"fields": {"FieldName": value, ...}}
        Returns count of created records.
        """
        total_created = 0

        for i in range(0, len(records), 500):
            batch = records[i:i + 500]
            data = {"records": batch}
            result = self._post(f"/tables/{table_id}/records/batch_create", data)

            if result.get("code") != 0:
                logger.error(f"批量创建记录失败: {result.get('msg')}")
                logger.debug(f"请求数据样本: {batch[0] if batch else 'empty'}")
                raise RuntimeError(f"批量创建记录失败: {result.get('msg')}")

            created = result.get("data", {}).get("records", [])
            total_created += len(created)
            logger.debug(f"批量创建 batch {i//500+1}: {len(created)} 条")

            # Rate limiting
            if i + 500 < len(records):
                time.sleep(0.5)

        return total_created

    def batch_create_records_with_ids(self, table_id: str, records: List[Dict]) -> List[Dict]:
        """Batch create records and return created record objects."""
        created_records = []

        for i in range(0, len(records), 500):
            batch = records[i:i + 500]
            data = {"records": batch}
            result = self._post(f"/tables/{table_id}/records/batch_create", data)

            if result.get("code") != 0:
                logger.error(f"批量创建记录失败: {result.get('msg')}")
                logger.debug(f"请求数据样本: {batch[0] if batch else 'empty'}")
                raise RuntimeError(f"批量创建记录失败: {result.get('msg')}")

            created = result.get("data", {}).get("records", [])
            created_records.extend(created)
            logger.debug(f"批量创建 batch {i//500+1}: {len(created)} 条")

            if i + 500 < len(records):
                time.sleep(0.5)

        return created_records

    def batch_delete_records(self, table_id: str, record_ids: List[str]) -> int:
        """Batch delete records by record_id (max 500 per call)."""
        total_deleted = 0

        for i in range(0, len(record_ids), 500):
            batch = [rid for rid in record_ids[i:i + 500] if rid]
            if not batch:
                continue
            result = self._post(
                f"/tables/{table_id}/records/batch_delete",
                {"records": batch},
            )

            if result.get("code") != 0:
                logger.error(f"批量删除记录失败: {result.get('msg')}")
                raise RuntimeError(f"批量删除记录失败: {result.get('msg')}")

            total_deleted += len(batch)
            logger.debug(f"批量删除 batch {i//500+1}: {len(batch)} 条")

            if i + 500 < len(record_ids):
                time.sleep(0.5)

        return total_deleted

    def batch_update_records(self, table_id: str, records: List[Dict]) -> int:
        """Batch update records (max 500 per call).

        Each record should be: {"record_id": "xxx", "fields": {"FieldName": value}}
        Returns count of updated records.
        """
        total_updated = 0

        for i in range(0, len(records), 500):
            batch = records[i:i + 500]
            data = {"records": batch}
            # Feishu's batch-update endpoint is POST. PUT is reserved for the
            # single-record endpoint and makes the API validate this payload as
            # if a top-level ``fields`` object were required.
            result = self._post(f"/tables/{table_id}/records/batch_update", data)

            if result.get("code") != 0:
                logger.error(f"批量更新记录失败: {result.get('msg')}")
                raise RuntimeError(f"批量更新记录失败: {result.get('msg')}")

            updated = result.get("data", {}).get("records", [])
            total_updated += len(updated)

            if i + 500 < len(records):
                time.sleep(0.5)

        return total_updated

    def update_record(self, table_id: str, record_id: str, fields: Dict) -> Dict:
        """Update a single record through the configured transport."""
        result = self._put(
            f"/tables/{table_id}/records/{record_id}",
            {"fields": fields},
        )
        if result.get("code") != 0:
            raise RuntimeError(
                f"更新飞书记录失败: {result.get('msg', 'Unknown error')} "
                f"(code={result.get('code')})"
            )
        return result.get("data", {}).get("record", {})

    def upsert_records(
        self,
        table_id: str,
        records: List[Dict],
        key_field: str = "唯一键",
        fallback_key_fields: List[str] = None,
    ) -> Dict[str, int]:
        """Update existing records by key_field, create missing records."""
        if not records:
            return {"updated": 0, "created": 0}

        existing = self.get_record_id_map(table_id, key_field, fallback_key_fields)
        to_update = []
        to_create = []

        for record in records:
            fields = record.get("fields", {})
            key = _field_to_text(fields.get(key_field))
            if not key:
                to_create.append(record)
                continue
            record_id = existing.get(key)
            if record_id:
                to_update.append({
                    "record_id": record_id,
                    "fields": fields,
                })
            else:
                to_create.append(record)

        updated = self.batch_update_records(table_id, to_update) if to_update else 0
        created = self.batch_create_records(table_id, to_create) if to_create else 0
        logger.info(f"幂等写入完成: 更新 {updated} 条, 新增 {created} 条")
        return {"updated": updated, "created": created}


def _field_to_text(value) -> str:
    """Normalize Feishu field values to plain text."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "").strip()
    return str(value).strip()
