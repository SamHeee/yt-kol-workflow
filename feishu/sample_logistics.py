#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime contract for the Feishu sample-logistics operations table.

The table remains an operations-layer table managed through the Feishu CLI;
it is deliberately not part of :mod:`feishu.schema`.  This module describes
the physical field contract and, more importantly, the small set of fields
that automated logistics and reminder jobs are allowed to write.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


SAMPLE_TRACKING_TABLE = "样品跟踪表"

PRIMARY_FIELD_NAME = "多行文本"
SAMPLE_RECORD_ID_FIELD = "样品记录ID"
COOPERATION_AUTO_ID_FIELD = "合作自动编号"
COOPERATION_RECORD_ID_FIELD = "合作记录ID"
CONTACT_EMAIL_FIELD = "联系邮箱"
KOL_NAME_FIELD = "KOL Name"
SAMPLE_DETAIL_FIELD = "样品明细"
SAMPLE_COST_FIELD = "样品成本"
SAMPLE_SHIPPING_COST_FIELD = "样品运费"
RECIPIENT_INFO_FIELD = "收件信息"
SAMPLE_STATUS_FIELD = "样品状态"
CARRIER_FIELD = "物流商"
TRACKING_NUMBER_FIELD = "物流单号"
SHIPPED_DATE_FIELD = "发货日期"
DELIVERED_DATE_FIELD = "签收日期"
NOTES_FIELD = "备注"

CARRIER_CODE_FIELD = "物流商编码"
SYNCED_SHIPMENT_KEY_FIELD = "已同步运单键"
LOGISTICS_STATUS_FIELD = "物流状态"
LATEST_PROGRESS_FIELD = "最新物流进度"
LATEST_EVENT_AT_FIELD = "最新物流时间"
STAGNATION_STARTED_AT_FIELD = "物流停滞起算时间"
SOURCE_SYNCED_AT_FIELD = "物流源同步时间"
LAST_QUERIED_AT_FIELD = "上次查询时间"
QUERY_ERROR_FIELD = "物流查询错误"
REMINDER_TYPE_FIELD = "提醒类型"
REMINDER_STATUS_FIELD = "提醒状态"
REMINDER_EVENT_KEY_FIELD = "提醒事件键"
REMINDER_SENT_AT_FIELD = "提醒发送时间"
REMINDER_ERROR_FIELD = "提醒错误"
PROVIDER_DELIVERED_AT_FIELD = "物流签收时间"
DELIVERED_DATE_REVIEW_FIELD = "签收日期核验状态"
LAST_QUERY_SHIPMENT_KEY_FIELD = "上次查询运单键"


# Feishu field type IDs: text=1, number=2, single-select=3, date=5,
# lookup=19 and auto-number=1005.  Manual sample cost fields follow the
# sample detail; logistics/reminder fields remain appended.
SAMPLE_LOGISTICS_FIELDS: List[Tuple[str, int]] = [
    (PRIMARY_FIELD_NAME, 1),
    (SAMPLE_RECORD_ID_FIELD, 1005),
    (COOPERATION_AUTO_ID_FIELD, 1),
    (COOPERATION_RECORD_ID_FIELD, 1),
    (CONTACT_EMAIL_FIELD, 19),
    (KOL_NAME_FIELD, 1),
    (SAMPLE_DETAIL_FIELD, 1),
    (SAMPLE_COST_FIELD, 2),
    (SAMPLE_SHIPPING_COST_FIELD, 2),
    (RECIPIENT_INFO_FIELD, 19),
    (SAMPLE_STATUS_FIELD, 3),
    (CARRIER_FIELD, 1),
    (TRACKING_NUMBER_FIELD, 1),
    (SHIPPED_DATE_FIELD, 5),
    (DELIVERED_DATE_FIELD, 5),
    (NOTES_FIELD, 1),
    (CARRIER_CODE_FIELD, 1),
    (SYNCED_SHIPMENT_KEY_FIELD, 1),
    (LOGISTICS_STATUS_FIELD, 3),
    (LATEST_PROGRESS_FIELD, 1),
    (LATEST_EVENT_AT_FIELD, 5),
    (STAGNATION_STARTED_AT_FIELD, 5),
    (SOURCE_SYNCED_AT_FIELD, 5),
    (LAST_QUERIED_AT_FIELD, 5),
    (QUERY_ERROR_FIELD, 1),
    (REMINDER_TYPE_FIELD, 3),
    (REMINDER_STATUS_FIELD, 3),
    (REMINDER_EVENT_KEY_FIELD, 1),
    (REMINDER_SENT_AT_FIELD, 5),
    (REMINDER_ERROR_FIELD, 1),
    (PROVIDER_DELIVERED_AT_FIELD, 5),
    (DELIVERED_DATE_REVIEW_FIELD, 1),
    (LAST_QUERY_SHIPMENT_KEY_FIELD, 1),
]

SAMPLE_STATUS_OPTIONS: Tuple[str, ...] = (
    "待收件信息",
    "待寄出",
    "已寄出",
    "已签收",
    "异常",
    "已取消",
)

LOGISTICS_STATUS_OPTIONS: Tuple[str, ...] = (
    "待查询",
    "暂无轨迹",
    "待揽收",
    "运输中",
    "待取件",
    "派送中",
    "投递失败",
    "已签收",
    "退回",
    "已过期",
    "异常",
    "未知",
)

REMINDER_TYPE_OPTIONS: Tuple[str, ...] = ("送达次日", "三天无进度")
REMINDER_STATUS_OPTIONS: Tuple[str, ...] = (
    "待发送",
    "发送中",
    "已发送",
    "发送失败",
    "待人工确认",
)

ELIGIBLE_SAMPLE_STATUSES = frozenset({"已寄出", "异常"})
TERMINAL_SAMPLE_STATUSES = frozenset({"已签收", "已取消"})

# Logistics refresh may update only derived logistics fields, plus the two
# explicitly approved business transitions.  Reminder fields are included so
# a changed tracking number can atomically discard reminders belonging to the
# old parcel.
LOGISTICS_WRITABLE_FIELDS = frozenset({
    LAST_QUERY_SHIPMENT_KEY_FIELD,
    PROVIDER_DELIVERED_AT_FIELD,
    DELIVERED_DATE_REVIEW_FIELD,
    SAMPLE_STATUS_FIELD,
    DELIVERED_DATE_FIELD,
    CARRIER_CODE_FIELD,
    SYNCED_SHIPMENT_KEY_FIELD,
    LOGISTICS_STATUS_FIELD,
    LATEST_PROGRESS_FIELD,
    LATEST_EVENT_AT_FIELD,
    STAGNATION_STARTED_AT_FIELD,
    SOURCE_SYNCED_AT_FIELD,
    LAST_QUERIED_AT_FIELD,
    QUERY_ERROR_FIELD,
    REMINDER_TYPE_FIELD,
    REMINDER_STATUS_FIELD,
    REMINDER_EVENT_KEY_FIELD,
    REMINDER_SENT_AT_FIELD,
    REMINDER_ERROR_FIELD,
})

REMINDER_WRITABLE_FIELDS = frozenset({
    REMINDER_TYPE_FIELD,
    REMINDER_STATUS_FIELD,
    REMINDER_EVENT_KEY_FIELD,
    REMINDER_SENT_AT_FIELD,
    REMINDER_ERROR_FIELD,
})

SYSTEM_WRITABLE_FIELDS = LOGISTICS_WRITABLE_FIELDS | REMINDER_WRITABLE_FIELDS

NEVER_SYSTEM_WRITABLE_FIELDS = frozenset({
    PRIMARY_FIELD_NAME,
    SAMPLE_RECORD_ID_FIELD,
    COOPERATION_AUTO_ID_FIELD,
    COOPERATION_RECORD_ID_FIELD,
    CONTACT_EMAIL_FIELD,
    KOL_NAME_FIELD,
    SAMPLE_DETAIL_FIELD,
    SAMPLE_COST_FIELD,
    SAMPLE_SHIPPING_COST_FIELD,
    RECIPIENT_INFO_FIELD,
    CARRIER_FIELD,
    TRACKING_NUMBER_FIELD,
    SHIPPED_DATE_FIELD,
    NOTES_FIELD,
})


class SampleLogisticsSchemaError(RuntimeError):
    """Raised when the sample table does not match the runtime contract."""


def schema_errors(
    fields: Iterable[Mapping[str, Any]],
    *,
    strict_order: bool = True,
    allow_extra: bool = False,
) -> List[str]:
    """Return human-readable differences from the sample logistics contract."""
    actual = list(fields)
    expected_names = [name for name, _ in SAMPLE_LOGISTICS_FIELDS]
    expected_types = dict(SAMPLE_LOGISTICS_FIELDS)
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
            errors.append("字段顺序与样品物流字段合同不一致")
    return errors


def validate_sample_logistics_schema(
    fields: Iterable[Mapping[str, Any]],
    *,
    strict_order: bool = True,
    allow_extra: bool = False,
) -> None:
    """Raise unless field metadata satisfies the physical field contract."""
    errors = schema_errors(
        fields,
        strict_order=strict_order,
        allow_extra=allow_extra,
    )
    if errors:
        raise SampleLogisticsSchemaError("；".join(errors))


def metadata_errors(
    fields: Iterable[Mapping[str, Any]],
    *,
    strict_order: bool = True,
    allow_extra: bool = False,
) -> List[str]:
    """Validate primary, auto-number, select options and date formatting."""
    actual = list(fields)
    errors = schema_errors(
        actual,
        strict_order=strict_order,
        allow_extra=allow_extra,
    )
    by_name = {str(item.get("field_name") or ""): item for item in actual}

    primary = by_name.get(PRIMARY_FIELD_NAME) or {}
    if primary.get("is_primary") is not True:
        errors.append("第一列多行文本不是主字段")

    auto_number = by_name.get(SAMPLE_RECORD_ID_FIELD) or {}
    rules = (
        ((auto_number.get("property") or {}).get("auto_serial") or {}).get("options")
        or []
    )
    normalized_rules = [
        (str(rule.get("type") or ""), str(rule.get("value") or ""))
        for rule in rules
        if isinstance(rule, Mapping)
    ]
    if normalized_rules != [("fixed_text", "SMP-"), ("system_number", "6")]:
        errors.append("样品记录ID自动编号规则不是 SMP- 加 6 位流水号")

    expected_select_options = {
        SAMPLE_STATUS_FIELD: SAMPLE_STATUS_OPTIONS,
        LOGISTICS_STATUS_FIELD: LOGISTICS_STATUS_OPTIONS,
        REMINDER_TYPE_FIELD: REMINDER_TYPE_OPTIONS,
        REMINDER_STATUS_FIELD: REMINDER_STATUS_OPTIONS,
    }
    for field_name, expected in expected_select_options.items():
        options = ((by_name.get(field_name) or {}).get("property") or {}).get("options") or []
        actual_options = [
            str(option.get("name") or "")
            for option in options
            if isinstance(option, Mapping)
        ]
        if actual_options != list(expected):
            errors.append(f"{field_name}选项错误: {actual_options}")

    for field_name in (SAMPLE_COST_FIELD, SAMPLE_SHIPPING_COST_FIELD):
        formatter = str(((by_name.get(field_name) or {}).get("property") or {}).get("formatter") or "")
        if formatter != "0.00":
            errors.append(
                f"{field_name}数字格式错误: 应为 0.00，实际为 {formatter!r}"
            )

    date_fields = {
        SHIPPED_DATE_FIELD: "yyyy-MM-dd",
        DELIVERED_DATE_FIELD: "yyyy-MM-dd",
        LATEST_EVENT_AT_FIELD: "yyyy-MM-dd HH:mm",
        STAGNATION_STARTED_AT_FIELD: "yyyy-MM-dd HH:mm",
        SOURCE_SYNCED_AT_FIELD: "yyyy-MM-dd HH:mm",
        LAST_QUERIED_AT_FIELD: "yyyy-MM-dd HH:mm",
        REMINDER_SENT_AT_FIELD: "yyyy-MM-dd HH:mm",
        PROVIDER_DELIVERED_AT_FIELD: "yyyy-MM-dd HH:mm",
    }
    for field_name, formatter in date_fields.items():
        property_value = (by_name.get(field_name) or {}).get("property") or {}
        actual_formatter = str(property_value.get("date_formatter") or "")
        if actual_formatter != formatter:
            errors.append(
                f"{field_name}日期格式错误: 应为 {formatter}，实际为 {actual_formatter!r}"
            )
        if property_value.get("auto_fill") is not False:
            errors.append(f"{field_name}禁止自动填充")
    return errors


def validate_sample_logistics_metadata(
    fields: Iterable[Mapping[str, Any]],
    *,
    strict_order: bool = True,
    allow_extra: bool = False,
) -> None:
    """Raise unless detailed field metadata is safe for automation."""
    errors = metadata_errors(
        fields,
        strict_order=strict_order,
        allow_extra=allow_extra,
    )
    if errors:
        raise SampleLogisticsSchemaError("；".join(errors))


def validate_logistics_update_fields(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a copy after enforcing the logistics refresh write whitelist."""
    unexpected = sorted(set(fields) - LOGISTICS_WRITABLE_FIELDS)
    if unexpected:
        raise ValueError(f"样品物流刷新禁止写入字段: {unexpected}")
    return dict(fields)


def validate_reminder_update_fields(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a copy after enforcing the reminder write whitelist."""
    unexpected = sorted(set(fields) - REMINDER_WRITABLE_FIELDS)
    if unexpected:
        raise ValueError(f"样品物流提醒禁止写入字段: {unexpected}")
    return dict(fields)


def field_names() -> Sequence[str]:
    """Return field names in their required physical order."""
    return tuple(name for name, _ in SAMPLE_LOGISTICS_FIELDS)
