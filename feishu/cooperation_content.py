#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime contract for the Feishu cooperation-content operations table.

The table is intentionally outside :mod:`feishu.schema`: it is not part of
the four-table discovery/export contract and is created and maintained through
the Feishu CLI.  This module only describes what the dedicated refresh job is
allowed to read and write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


COOPERATION_CONTENT_TABLE = "合作内容数据表"
COOPERATION_TABLE = "合作红人表"
SAMPLE_TRACKING_TABLE = "样品跟踪表"

PRIMARY_FIELD_NAME = "多行文本"
CONTENT_ID_FIELD = "内容记录ID"
COOPERATION_AUTO_ID_FIELD = "合作自动编号"
COOPERATION_RECORD_ID_FIELD = "合作记录ID"
CHANNEL_ID_FIELD = "Channel ID"
KOL_NAME_FIELD = "KOL Name"
SAMPLE_RECORD_ID_FIELD = "样品记录ID"
CONTENT_STATUS_FIELD = "内容状态"
VIDEO_URL_FIELD = "视频URL"
VIDEO_ID_FIELD = "Video ID"
VIDEO_TITLE_FIELD = "Video Title"
PUBLISH_TIME_FIELD = "Publish Time"
VIEWS_FIELD = "Views"
LIKES_FIELD = "Likes"
COMMENTS_FIELD = "Comments"
ENGAGEMENT_RATE_FIELD = "互动率(%)"
ATTRIBUTED_CLICKS_FIELD = "归因点击"
ATTRIBUTED_GMV_FIELD = "归因GMV"
ATTRIBUTED_ORDERS_FIELD = "归因订单量"
ATTRIBUTED_SALES_FIELD = "归因销量"
DATA_UPDATED_AT_FIELD = "数据更新时间"
SYNC_STATUS_FIELD = "数据同步状态"
SYNC_ERROR_FIELD = "同步错误"


# Feishu field type IDs: text=1, number=2, single-select=3, date=5,
# URL=15, lookup=19 and auto-number=1005.
COOPERATION_CONTENT_FIELDS: List[Tuple[str, int]] = [
    (PRIMARY_FIELD_NAME, 1),
    (CONTENT_ID_FIELD, 1005),
    (COOPERATION_AUTO_ID_FIELD, 1),
    (COOPERATION_RECORD_ID_FIELD, 19),
    (CHANNEL_ID_FIELD, 19),
    (KOL_NAME_FIELD, 19),
    (SAMPLE_RECORD_ID_FIELD, 19),
    (CONTENT_STATUS_FIELD, 3),
    (VIDEO_URL_FIELD, 15),
    (VIDEO_ID_FIELD, 1),
    (VIDEO_TITLE_FIELD, 1),
    (PUBLISH_TIME_FIELD, 5),
    (VIEWS_FIELD, 2),
    (LIKES_FIELD, 2),
    (COMMENTS_FIELD, 2),
    (ENGAGEMENT_RATE_FIELD, 2),
    (ATTRIBUTED_CLICKS_FIELD, 2),
    (ATTRIBUTED_GMV_FIELD, 2),
    (ATTRIBUTED_ORDERS_FIELD, 2),
    (ATTRIBUTED_SALES_FIELD, 2),
    (DATA_UPDATED_AT_FIELD, 5),
    (SYNC_STATUS_FIELD, 3),
    (SYNC_ERROR_FIELD, 1),
]

CONTENT_STATUS_OPTIONS: Tuple[str, ...] = (
    "待发布",
    "已发布",
    "已下架",
    "已取消",
)

SYNC_STATUS_OPTIONS: Tuple[str, ...] = (
    "待同步",
    "同步成功",
    "部分指标不可用",
    "URL无效",
    "合作匹配失败",
    "重复视频",
    "频道不匹配",
    "视频不可用",
    "API失败",
)

TERMINAL_CONTENT_STATUSES = frozenset({"已下架", "已取消"})
SUCCESS_SYNC_STATUSES = frozenset({"同步成功", "部分指标不可用"})


@dataclass(frozen=True)
class LookupSpec:
    """Declarative lookup metadata used by setup documentation and tests."""

    source_table: str
    return_field: str
    conditions: Tuple[Tuple[str, str], ...]
    conjunction: str = "AND"
    aggregation: str = "原始值"


_COOPERATION_LOOKUP_CONDITIONS = (("自动编号", COOPERATION_AUTO_ID_FIELD),)

LOOKUP_SPECS: Mapping[str, LookupSpec] = {
    COOPERATION_RECORD_ID_FIELD: LookupSpec(
        source_table=COOPERATION_TABLE,
        return_field=COOPERATION_RECORD_ID_FIELD,
        conditions=_COOPERATION_LOOKUP_CONDITIONS,
    ),
    CHANNEL_ID_FIELD: LookupSpec(
        source_table=COOPERATION_TABLE,
        return_field=CHANNEL_ID_FIELD,
        conditions=_COOPERATION_LOOKUP_CONDITIONS,
    ),
    KOL_NAME_FIELD: LookupSpec(
        source_table=COOPERATION_TABLE,
        return_field=KOL_NAME_FIELD,
        conditions=_COOPERATION_LOOKUP_CONDITIONS,
    ),
    SAMPLE_RECORD_ID_FIELD: LookupSpec(
        source_table=SAMPLE_TRACKING_TABLE,
        return_field=SAMPLE_RECORD_ID_FIELD,
        conditions=((COOPERATION_AUTO_ID_FIELD, COOPERATION_AUTO_ID_FIELD),),
    ),
}


# The refresh workflow may never write the blank primary field, Feishu's auto
# number, lookup results, cooperation keys, sample reference or source URL.
SYSTEM_WRITABLE_FIELDS = frozenset({
    CONTENT_STATUS_FIELD,
    VIDEO_ID_FIELD,
    VIDEO_TITLE_FIELD,
    PUBLISH_TIME_FIELD,
    VIEWS_FIELD,
    LIKES_FIELD,
    COMMENTS_FIELD,
    ENGAGEMENT_RATE_FIELD,
    DATA_UPDATED_AT_FIELD,
    SYNC_STATUS_FIELD,
    SYNC_ERROR_FIELD,
})

LOOKUP_READ_ONLY_FIELDS = frozenset(LOOKUP_SPECS)
NEVER_SYSTEM_WRITABLE_FIELDS = frozenset({
    PRIMARY_FIELD_NAME,
    CONTENT_ID_FIELD,
    COOPERATION_AUTO_ID_FIELD,
    COOPERATION_RECORD_ID_FIELD,
    CHANNEL_ID_FIELD,
    KOL_NAME_FIELD,
    SAMPLE_RECORD_ID_FIELD,
    VIDEO_URL_FIELD,
    ATTRIBUTED_CLICKS_FIELD,
    ATTRIBUTED_GMV_FIELD,
    ATTRIBUTED_ORDERS_FIELD,
    ATTRIBUTED_SALES_FIELD,
})


class CooperationContentSchemaError(RuntimeError):
    """Raised when the operations table does not match the runtime contract."""


def schema_errors(
    fields: Iterable[Mapping[str, Any]],
    *,
    strict_order: bool = True,
    allow_extra: bool = False,
) -> List[str]:
    """Return human-readable differences from the physical field contract."""
    actual = list(fields)
    expected_names = [name for name, _ in COOPERATION_CONTENT_FIELDS]
    expected_types = dict(COOPERATION_CONTENT_FIELDS)
    actual_names = [str(field.get("field_name") or "") for field in actual]

    errors: List[str] = []
    duplicates = sorted({name for name in actual_names if name and actual_names.count(name) > 1})
    if duplicates:
        errors.append(f"存在重复字段: {duplicates}")

    missing = [name for name in expected_names if name not in actual_names]
    if missing:
        errors.append(f"缺少字段: {missing}")

    if not allow_extra:
        extra = [name for name in actual_names if name not in expected_types]
        if extra:
            errors.append(f"存在合同外字段: {extra}")

    for field in actual:
        name = str(field.get("field_name") or "")
        if name not in expected_types:
            continue
        try:
            actual_type = int(field.get("type"))
        except (TypeError, ValueError):
            actual_type = field.get("type")
        if actual_type != expected_types[name]:
            errors.append(
                f"字段类型错误: {name} 应为 {expected_types[name]}，实际为 {actual_type}"
            )

    if strict_order:
        comparable_actual = [name for name in actual_names if name in expected_types]
        if comparable_actual != expected_names:
            errors.append("字段顺序与合作内容字段合同不一致")

    return errors


def validate_cooperation_content_schema(
    fields: Iterable[Mapping[str, Any]],
    *,
    strict_order: bool = True,
    allow_extra: bool = False,
) -> None:
    """Raise when field metadata does not satisfy the runtime contract."""
    errors = schema_errors(fields, strict_order=strict_order, allow_extra=allow_extra)
    if errors:
        raise CooperationContentSchemaError("；".join(errors))


def metadata_errors(
    fields: Iterable[Mapping[str, Any]],
    *,
    cooperation_table_id: str,
    cooperation_fields: Iterable[Mapping[str, Any]],
    sample_table_id: str,
    sample_fields: Iterable[Mapping[str, Any]],
    strict_order: bool = True,
) -> List[str]:
    """Validate lookup targets, auto-number rules and select options.

    The regular schema check protects record payloads from column drift.  This
    deeper check uses the raw v1 field metadata returned by the CLI-backed
    client so a same-named but incorrectly wired lookup cannot pass startup.
    """
    actual = list(fields)
    errors = schema_errors(actual, strict_order=strict_order)
    by_name = {str(item.get("field_name") or ""): item for item in actual}
    cooperation_by_name = {
        str(item.get("field_name") or ""): item for item in cooperation_fields
    }
    sample_by_name = {
        str(item.get("field_name") or ""): item for item in sample_fields
    }

    primary = by_name.get(PRIMARY_FIELD_NAME) or {}
    if primary.get("is_primary") is not True:
        errors.append("第一列多行文本不是主字段")

    auto_number = by_name.get(CONTENT_ID_FIELD) or {}
    auto_property = auto_number.get("property") or {}
    auto_serial = auto_property.get("auto_serial") or {}
    rules = auto_serial.get("options") or []
    normalized_rules = [
        (str(rule.get("type") or ""), str(rule.get("value") or ""))
        for rule in rules
        if isinstance(rule, Mapping)
    ]
    if normalized_rules != [("fixed_text", "CNT-"), ("system_number", "6")]:
        errors.append("内容记录ID自动编号规则不是 CNT- 加 6 位流水号")

    expected_select_options = {
        CONTENT_STATUS_FIELD: list(CONTENT_STATUS_OPTIONS),
        SYNC_STATUS_FIELD: list(SYNC_STATUS_OPTIONS),
    }
    for field_name, expected in expected_select_options.items():
        field = by_name.get(field_name) or {}
        options = (field.get("property") or {}).get("options") or []
        actual_options = [
            str(option.get("name") or "")
            for option in options
            if isinstance(option, Mapping)
        ]
        if actual_options != expected:
            errors.append(f"{field_name}选项错误: {actual_options}")

    expected_number_formatters = {
        VIEWS_FIELD: "1,000",
        LIKES_FIELD: "1,000",
        COMMENTS_FIELD: "1,000",
        ENGAGEMENT_RATE_FIELD: "0.00",
        ATTRIBUTED_CLICKS_FIELD: "1,000",
        ATTRIBUTED_GMV_FIELD: "0.00",
        ATTRIBUTED_ORDERS_FIELD: "1,000",
        ATTRIBUTED_SALES_FIELD: "1,000",
    }
    for field_name, expected_formatter in expected_number_formatters.items():
        field = by_name.get(field_name) or {}
        formatter = str((field.get("property") or {}).get("formatter") or "")
        if formatter != expected_formatter:
            errors.append(
                f"{field_name}数字格式错误: 应为 {expected_formatter}，实际为 {formatter!r}"
            )

    for field_name in (PUBLISH_TIME_FIELD, DATA_UPDATED_AT_FIELD):
        field = by_name.get(field_name) or {}
        property_value = field.get("property") or {}
        date_formatter = str(property_value.get("date_formatter") or "")
        if date_formatter != "yyyy-MM-dd HH:mm":
            errors.append(
                f"{field_name}日期格式错误: 应为 yyyy-MM-dd HH:mm，实际为 {date_formatter!r}"
            )
        if property_value.get("auto_fill") is not False:
            errors.append(f"{field_name}禁止自动填充")

    current_key_id = _field_id(by_name.get(COOPERATION_AUTO_ID_FIELD) or {})
    lookup_expectations = {
        COOPERATION_RECORD_ID_FIELD: (
            cooperation_table_id,
            _field_id(cooperation_by_name.get(COOPERATION_RECORD_ID_FIELD) or {}),
            _field_id(cooperation_by_name.get("自动编号") or {}),
        ),
        CHANNEL_ID_FIELD: (
            cooperation_table_id,
            _field_id(cooperation_by_name.get(CHANNEL_ID_FIELD) or {}),
            _field_id(cooperation_by_name.get("自动编号") or {}),
        ),
        KOL_NAME_FIELD: (
            cooperation_table_id,
            _field_id(cooperation_by_name.get(KOL_NAME_FIELD) or {}),
            _field_id(cooperation_by_name.get("自动编号") or {}),
        ),
        SAMPLE_RECORD_ID_FIELD: (
            sample_table_id,
            _field_id(sample_by_name.get(SAMPLE_RECORD_ID_FIELD) or {}),
            _field_id(sample_by_name.get(COOPERATION_AUTO_ID_FIELD) or {}),
        ),
    }
    if not current_key_id:
        errors.append("合作自动编号字段缺少 field_id")

    for field_name, (target_table, target_field, source_key) in lookup_expectations.items():
        item = by_name.get(field_name) or {}
        property_value = item.get("property") or {}
        filter_info = property_value.get("filter_info") or {}
        conditions = filter_info.get("conditions") or {}
        children = conditions.get("children") or []
        formula = str(property_value.get("formula") or "")
        if not target_table or not target_field or not source_key:
            errors.append(f"{field_name}来源表元数据不完整")
            continue
        if str(filter_info.get("target_table") or "") != target_table:
            errors.append(f"{field_name}来源表错误")
        if str(property_value.get("target_field") or "") != target_field:
            errors.append(f"{field_name}返回字段错误")
        if str(conditions.get("conjunction") or "").lower() != "and":
            errors.append(f"{field_name}查找条件不是 AND")
        child = children[0] if len(children) == 1 and isinstance(children[0], Mapping) else {}
        if len(children) != 1 or str(child.get("field_id") or "") != source_key:
            errors.append(f"{field_name}查找条件字段错误")
        if str(child.get("operator") or "").lower() != "is":
            errors.append(f"{field_name}查找条件操作符不是 is")
        if "value" not in child or child.get("value") is not None:
            errors.append(f"{field_name}查找条件 value 必须为 null")
        roll_up = property_value.get("roll_up")
        if isinstance(roll_up, bool) or roll_up != 0:
            errors.append(f"{field_name}聚合方式不是原始值（roll_up=0/raw_value）")
        required_formula_ids = (target_table, target_field, source_key, current_key_id)
        if any(
            not _formula_references_identifier(formula, required_id)
            for required_id in required_formula_ids
        ):
            errors.append(f"{field_name}查找公式未引用正确字段")

    return errors


def validate_cooperation_content_metadata(
    fields: Iterable[Mapping[str, Any]],
    *,
    cooperation_table_id: str,
    cooperation_fields: Iterable[Mapping[str, Any]],
    sample_table_id: str,
    sample_fields: Iterable[Mapping[str, Any]],
    strict_order: bool = True,
) -> None:
    """Raise unless the full remote content-table contract is safe to run."""
    errors = metadata_errors(
        fields,
        cooperation_table_id=cooperation_table_id,
        cooperation_fields=cooperation_fields,
        sample_table_id=sample_table_id,
        sample_fields=sample_fields,
        strict_order=strict_order,
    )
    if errors:
        raise CooperationContentSchemaError("；".join(errors))


def validate_system_update_fields(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a copy after enforcing the refresh workflow's write whitelist."""
    unexpected = sorted(set(fields) - SYSTEM_WRITABLE_FIELDS)
    if unexpected:
        raise ValueError(f"合作内容同步禁止写入字段: {unexpected}")
    return dict(fields)


def field_names() -> Sequence[str]:
    """Return field names in their required physical order."""
    return tuple(name for name, _ in COOPERATION_CONTENT_FIELDS)


def _field_id(field: Mapping[str, Any]) -> str:
    return str(field.get("field_id") or field.get("id") or "")


def _formula_references_identifier(formula: str, identifier: str) -> bool:
    """Return whether a formula contains an exact table/field identifier token."""
    if not formula or not identifier:
        return False
    token_chars = r"A-Za-z0-9_-"
    return re.search(
        rf"(?<![{token_chars}]){re.escape(identifier)}(?![{token_chars}])",
        formula,
    ) is not None
