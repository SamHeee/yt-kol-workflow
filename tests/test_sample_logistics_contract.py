from copy import deepcopy

import pytest

from feishu.sample_logistics import (
    LOGISTICS_STATUS_FIELD,
    LOGISTICS_STATUS_OPTIONS,
    LOGISTICS_WRITABLE_FIELDS,
    NEVER_SYSTEM_WRITABLE_FIELDS,
    REMINDER_STATUS_FIELD,
    REMINDER_STATUS_OPTIONS,
    REMINDER_TYPE_FIELD,
    REMINDER_TYPE_OPTIONS,
    REMINDER_WRITABLE_FIELDS,
    SAMPLE_LOGISTICS_FIELDS,
    SAMPLE_STATUS_FIELD,
    SAMPLE_STATUS_OPTIONS,
    SampleLogisticsSchemaError,
    field_names,
    metadata_errors,
    schema_errors,
    validate_logistics_update_fields,
    validate_reminder_update_fields,
    validate_sample_logistics_schema,
)


ORIGINAL_FIELDS = (
    "多行文本",
    "样品记录ID",
    "合作自动编号",
    "合作记录ID",
    "联系邮箱",
    "KOL Name",
    "样品明细",
    "收件信息",
    "样品状态",
    "物流商",
    "物流单号",
    "发货日期",
    "签收日期",
    "备注",
)

ADDED_FIELDS = (
    "物流商编码",
    "已同步运单键",
    "物流状态",
    "最新物流进度",
    "最新物流时间",
    "物流停滞起算时间",
    "物流源同步时间",
    "上次查询时间",
    "物流查询错误",
    "提醒类型",
    "提醒状态",
    "提醒事件键",
    "提醒发送时间",
    "提醒错误",
    "物流签收时间",
    "签收日期核验状态",
    "上次查询运单键",
)


def _field_metadata():
    select_options = {
        SAMPLE_STATUS_FIELD: SAMPLE_STATUS_OPTIONS,
        LOGISTICS_STATUS_FIELD: LOGISTICS_STATUS_OPTIONS,
        REMINDER_TYPE_FIELD: REMINDER_TYPE_OPTIONS,
        REMINDER_STATUS_FIELD: REMINDER_STATUS_OPTIONS,
    }
    date_formatters = {
        "发货日期": "yyyy-MM-dd",
        "签收日期": "yyyy-MM-dd",
        "最新物流时间": "yyyy-MM-dd HH:mm",
        "物流停滞起算时间": "yyyy-MM-dd HH:mm",
        "物流源同步时间": "yyyy-MM-dd HH:mm",
        "上次查询时间": "yyyy-MM-dd HH:mm",
        "提醒发送时间": "yyyy-MM-dd HH:mm",
        "物流签收时间": "yyyy-MM-dd HH:mm",
    }
    fields = []
    for index, (name, field_type) in enumerate(SAMPLE_LOGISTICS_FIELDS):
        item = {
            "field_name": name,
            "field_id": f"fld{index}",
            "type": field_type,
            "is_primary": index == 0,
        }
        if name == "样品记录ID":
            item["property"] = {
                "auto_serial": {
                    "options": [
                        {"type": "fixed_text", "value": "SMP-"},
                        {"type": "system_number", "value": "6"},
                    ]
                }
            }
        elif name in select_options:
            item["property"] = {
                "options": [{"name": option} for option in select_options[name]]
            }
        elif name in date_formatters:
            item["property"] = {
                "date_formatter": date_formatters[name],
                "auto_fill": False,
            }
        elif name in ("样品成本", "样品运费"):
            item["property"] = {"formatter": "0.00"}
        fields.append(item)
    return fields


def test_contract_inserts_cost_columns_after_sample_detail():
    names = tuple(field_names())

    assert len(SAMPLE_LOGISTICS_FIELDS) == 33
    assert names == (
        ORIGINAL_FIELDS[:7]
        + ("样品成本", "样品运费")
        + ORIGINAL_FIELDS[7:]
        + ADDED_FIELDS
    )
    assert dict(SAMPLE_LOGISTICS_FIELDS)["样品成本"] == 2
    assert dict(SAMPLE_LOGISTICS_FIELDS)["样品运费"] == 2
    assert names[0] == "多行文本"


def test_schema_validation_detects_order_type_missing_and_extra_fields():
    valid = _field_metadata()
    validate_sample_logistics_schema(valid)

    invalid = deepcopy(valid)
    names = tuple(field_names())
    code_index = names.index("物流商编码")
    synced_index = names.index("已同步运单键")
    invalid[code_index], invalid[synced_index] = invalid[synced_index], invalid[code_index]
    invalid[names.index("物流状态")]["type"] = 1
    invalid.pop(names.index("最新物流进度"))
    invalid.append({"field_name": "意外字段", "type": 1})

    errors = schema_errors(invalid)
    assert any("字段顺序" in error for error in errors)
    assert any("物流状态" in error and "类型错误" in error for error in errors)
    assert any("最新物流进度" in error and "缺少字段" in error for error in errors)
    assert any("意外字段" in error and "合同外" in error for error in errors)
    with pytest.raises(SampleLogisticsSchemaError):
        validate_sample_logistics_schema(invalid)


def test_metadata_validation_covers_primary_auto_number_selects_and_dates():
    valid = _field_metadata()
    assert metadata_errors(valid) == []

    invalid = deepcopy(valid)
    invalid[0]["is_primary"] = False
    invalid[1]["property"]["auto_serial"]["options"][0]["value"] = "BAD-"
    by_name = {item["field_name"]: item for item in invalid}
    by_name[LOGISTICS_STATUS_FIELD]["property"]["options"].reverse()
    by_name["上次查询时间"]["property"]["date_formatter"] = "yyyy-MM-dd"
    by_name["样品成本"]["property"]["formatter"] = "0"

    errors = metadata_errors(invalid)
    assert any("不是主字段" in error for error in errors)
    assert any("SMP-" in error for error in errors)
    assert any(LOGISTICS_STATUS_FIELD in error and "选项错误" in error for error in errors)
    assert any("上次查询时间" in error and "日期格式错误" in error for error in errors)
    assert any("样品成本" in error and "数字格式错误" in error for error in errors)


def test_metadata_validation_can_ignore_unstable_cli_field_list_order():
    fields = _field_metadata()
    shuffled = [fields[0], *reversed(fields[1:])]

    assert any("字段顺序" in error for error in metadata_errors(shuffled))
    assert metadata_errors(shuffled, strict_order=False) == []


def test_refresh_and_reminder_write_whitelists_are_separate_and_strict():
    assert {"样品成本", "样品运费"} <= NEVER_SYSTEM_WRITABLE_FIELDS
    assert {"样品成本", "样品运费"}.isdisjoint(LOGISTICS_WRITABLE_FIELDS)
    assert {"样品成本", "样品运费"}.isdisjoint(REMINDER_WRITABLE_FIELDS)

    refresh_fields = validate_logistics_update_fields({
        "物流状态": "运输中",
        "最新物流进度": "Shanghai｜已离开处理中心",
        "样品状态": "已签收",
        "签收日期": 123,
    })
    assert set(refresh_fields) <= LOGISTICS_WRITABLE_FIELDS

    reminder_fields = validate_reminder_update_fields({
        "提醒类型": "送达次日",
        "提醒状态": "已发送",
    })
    assert set(reminder_fields) <= REMINDER_WRITABLE_FIELDS

    with pytest.raises(ValueError, match="禁止写入"):
        validate_logistics_update_fields({"物流单号": "operator-owned"})
    with pytest.raises(ValueError, match="禁止写入"):
        validate_reminder_update_fields({"物流状态": "运输中"})
