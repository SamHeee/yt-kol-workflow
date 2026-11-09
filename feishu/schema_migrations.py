"""Explicit, narrow upgrade for the recent-video/outreach schema revision.

Normal setup remains strict. This tool never creates a Base/table, changes
field types, rewrites unrelated columns, or enables an automation.
"""

from copy import deepcopy

from .full_schema import FULL_SCHEMA, LEGACY_OUTREACH_LETTER_FORMULA
from .provisioning import (
    ProvisioningConflict, _existing_field_payload, _group_unique, _payload_items,
    _field_name, _table_name, _table_id,
)
from .schema import SEARCH_VIDEOS_TABLE, INFLUENCERS_TABLE

TARGETS = {
    SEARCH_VIDEOS_TABLE: ("Views", "Likes", "Comments"),
    INFLUENCERS_TABLE: ("订阅数", "频道总播放量", "视频总数", "代表视频播放量",
                        "apify邮箱", "最近视频平均播放量", "最近视频中位播放量", "邮件主题", "开发信"),
}


def writable_definition(field):
    # Only these three field types occur in this migration; preserve every
    # supported writable property, excluding server-owned IDs and flags.
    return {key: deepcopy(field[key]) for key in
            ("name", "type", "description", "default_value", "style", "expression") if key in field}


def plan_upgrade(admin):
    tables = _group_unique(_payload_items(admin.list_tables(), "tables"), _table_name)
    plan = {"revision": "recent-views-outreach-v1", "actions": [], "conflicts": []}
    for table_name, names in TARGETS.items():
        if table_name not in tables:
            plan["conflicts"].append(f"缺少表: {table_name}；新 Base 请使用 feishu-setup")
            continue
        table_id = _table_id(tables[table_name])
        fields = _group_unique(_payload_items(admin.list_fields(table_id), "fields"), _field_name)
        for name in names:
            target = FULL_SCHEMA.table(table_name).field(name).cli_json()
            existing = fields.get(name)
            action = {"table_id": table_id, "table": table_name, "field": name}
            if existing is None:
                plan["actions"].append({**action, "kind": "create", "after": target})
                continue
            field_id = existing.get("field_id") or existing.get("id")
            if not field_id:
                plan["conflicts"].append(f"{table_name}.{name}: 缺少字段 ID")
                continue
            current = _existing_field_payload(admin.get_field(table_id, field_id))
            before = writable_definition(current)
            if before.get("type") != target["type"]:
                plan["conflicts"].append(f"{table_name}.{name}: 字段类型不一致")
                continue
            after = deepcopy(before)
            if target["type"] == "number":
                # Format-only migration: preserve defaults/description.
                after["style"] = deepcopy(target["style"])
            elif target["type"] == "formula":
                expression = before.get("expression")
                if expression != target["expression"]:
                    if name != "开发信" or expression != LEGACY_OUTREACH_LETTER_FORMULA:
                        plan["conflicts"].append(f"{table_name}.{name}: 自定义公式，需人工核对")
                        continue
                    after["expression"] = target["expression"]
            elif (before.get("style") or {}).get("type", "plain") != "plain":
                plan["conflicts"].append(f"{table_name}.{name}: 不是普通文本")
                continue
            if before != after:
                plan["actions"].append({**action, "kind": "update", "field_id": field_id,
                                        "before": before, "after": after})
    return plan


def apply_upgrade(admin, plan, *, journal=None):
    if plan["conflicts"]:
        raise ProvisioningConflict("; ".join(plan["conflicts"]))
    # A preview is not a lock. Validate the entire baseline before any writes.
    if plan_upgrade(admin) != plan:
        raise ProvisioningConflict("字段配置在预览后发生变化，请重新生成升级计划")
    for action in plan["actions"]:
        if action["kind"] == "update":
            current = writable_definition(_existing_field_payload(
                admin.get_field(action["table_id"], action["field_id"])))
            if current != action["before"]:
                raise ProvisioningConflict(f"{action['field']}: 写入前配置发生变化")
            admin.update_field(action["table_id"], action["field_id"], action["after"])
        else:
            fields = _group_unique(_payload_items(admin.list_fields(action["table_id"]), "fields"), _field_name)
            if action["field"] in fields:
                raise ProvisioningConflict(f"{action['field']}: 写入前已被创建")
            admin.create_field(action["table_id"], action["after"])
        if journal:
            journal(action)
    # Verify all requested writable properties, including preserved defaults.
    for action in plan["actions"]:
        fields = _group_unique(_payload_items(admin.list_fields(action["table_id"]), "fields"), _field_name)
        item = fields.get(action["field"], {})
        fid = item.get("field_id") or item.get("id")
        if not fid:
            raise ProvisioningConflict(f"{action['field']}: 升级后字段缺失")
        actual = writable_definition(_existing_field_payload(admin.get_field(action["table_id"], fid)))
        if any(actual.get(key) != value for key, value in action["after"].items()):
            raise ProvisioningConflict(f"{action['field']}: 升级后属性读回不一致")
    remaining = plan_upgrade(admin)
    if remaining["conflicts"] or remaining["actions"]:
        raise ProvisioningConflict("升级后元数据尚未一致，请检查报告并重新预览")
    return remaining
