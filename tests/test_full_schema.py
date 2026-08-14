from dataclasses import FrozenInstanceError

import pytest

from feishu.cooperation_content import COOPERATION_CONTENT_TABLE
from feishu.full_schema import (
    AUTO_NUMBER,
    COOPERATION_TABLE,
    EMAIL_INDEX_FORMULA,
    FORMULA,
    FULL_SCHEMA,
    LOOKUP,
    OUTREACH_LETTER_FORMULA,
    OUTREACH_LETTER_TEMPLATE,
    SAMPLE_LOOKUP_SPECS,
    TABLE_SPECS,
)
from feishu.sample_logistics import SAMPLE_TRACKING_TABLE
from feishu.schema import (
    INFLUENCERS_FIELDS,
    INFLUENCERS_TABLE,
    INFLUENCER_VIDEOS_FIELDS,
    INFLUENCER_VIDEOS_TABLE,
    SEARCH_TASKS_FIELDS,
    SEARCH_TASKS_TABLE,
    SEARCH_VIDEOS_FIELDS,
    SEARCH_VIDEOS_TABLE,
)


EXPECTED_TABLE_ORDER = (
    SEARCH_TASKS_TABLE,
    SEARCH_VIDEOS_TABLE,
    INFLUENCERS_TABLE,
    INFLUENCER_VIDEOS_TABLE,
    COOPERATION_TABLE,
    SAMPLE_TRACKING_TABLE,
    COOPERATION_CONTENT_TABLE,
)


def test_full_contract_has_seven_tables_141_fields_and_nine_lookups():
    assert FULL_SCHEMA.table_names == EXPECTED_TABLE_ORDER
    assert FULL_SCHEMA.field_count == 141
    assert FULL_SCHEMA.lookup_count == 9
    assert FULL_SCHEMA.view_count == 7
    assert set(TABLE_SPECS) == set(EXPECTED_TABLE_ORDER)


def test_existing_four_table_excel_contract_is_unchanged():
    original_contracts = {
        SEARCH_TASKS_TABLE: SEARCH_TASKS_FIELDS,
        SEARCH_VIDEOS_TABLE: SEARCH_VIDEOS_FIELDS,
        INFLUENCER_VIDEOS_TABLE: INFLUENCER_VIDEOS_FIELDS,
    }
    for table_name, original in original_contracts.items():
        full_pairs = [
            (field.name, field.field_type)
            for field in FULL_SCHEMA.table(table_name).fields
        ]
        assert full_pairs == original

    influencer = FULL_SCHEMA.table(INFLUENCERS_TABLE)
    original_count = len(INFLUENCERS_FIELDS)
    assert [
        (field.name, field.field_type)
        for field in influencer.fields[:original_count]
    ] == INFLUENCERS_FIELDS
    assert tuple(field.name for field in influencer.fields[original_count:]) == (
        "邮箱索引",
        "开发信",
    )
    assert len(INFLUENCERS_FIELDS) == 25


def test_every_table_has_only_one_unfiltered_default_grid_contract():
    for table in FULL_SCHEMA.tables:
        assert table.default_view.name == "表格"
        assert table.default_view.view_type == "grid"
        assert table.default_view.is_default is True
        assert not hasattr(table.default_view, "filter")
        assert not hasattr(table.default_view, "visible_fields")


def test_table_dependencies_are_topologically_ordered():
    positions = {name: index for index, name in enumerate(FULL_SCHEMA.table_names)}
    for table in FULL_SCHEMA.tables:
        for dependency in table.dependencies:
            assert dependency in positions
            assert positions[dependency] < positions[table.name]


def test_all_nine_lookups_have_one_dynamic_equality_and_raw_values():
    lookups = [
        (table.name, field.name, field.lookup)
        for table in FULL_SCHEMA.tables
        for field in table.fields
        if field.lookup is not None
    ]
    assert len(lookups) == 9

    for table_name, field_name, lookup in lookups:
        assert lookup.source_table in FULL_SCHEMA.table_names
        assert FULL_SCHEMA.table(lookup.source_table).field(lookup.select_field)
        assert lookup.logic == "and"
        assert lookup.aggregate == "raw_value"
        assert len(lookup.conditions) == 1
        condition = lookup.conditions[0]
        assert condition.operator == "=="
        assert FULL_SCHEMA.table(lookup.source_table).field(condition.source_field)
        assert FULL_SCHEMA.table(table_name).field(condition.current_field)
        assert FULL_SCHEMA.table(table_name).field(field_name).field_type == LOOKUP


def test_lookup_contracts_follow_email_and_cooperation_auto_number_keys():
    cooperation = FULL_SCHEMA.table(COOPERATION_TABLE)
    for name in ("Channel Name", "KOL Name", "Channel ID"):
        lookup = cooperation.field(name).lookup
        assert lookup.source_table == INFLUENCERS_TABLE
        condition = lookup.conditions[0]
        assert (condition.source_field, condition.current_field) == (
            "邮箱索引",
            "邮箱索引",
        )

    assert set(SAMPLE_LOOKUP_SPECS) == {"联系邮箱", "收件信息"}
    for lookup in SAMPLE_LOOKUP_SPECS.values():
        condition = lookup.conditions[0]
        assert (condition.source_field, condition.current_field) == (
            "自动编号",
            "合作自动编号",
        )

    content = FULL_SCHEMA.table(COOPERATION_CONTENT_TABLE)
    for name in ("合作记录ID", "Channel ID", "KOL Name"):
        condition = content.field(name).lookup.conditions[0]
        assert (condition.source_field, condition.current_field) == (
            "自动编号",
            "合作自动编号",
        )
    sample_condition = content.field("样品记录ID").lookup.conditions[0]
    assert (sample_condition.source_field, sample_condition.current_field) == (
        "合作自动编号",
        "合作自动编号",
    )


def test_formula_and_auto_number_contracts_are_fully_declared():
    influencers = FULL_SCHEMA.table(INFLUENCERS_TABLE)
    cooperation = FULL_SCHEMA.table(COOPERATION_TABLE)
    assert influencers.field("邮箱索引").formula == EMAIL_INDEX_FORMULA
    assert cooperation.field("邮箱索引").formula == EMAIL_INDEX_FORMULA
    assert cooperation.field("合作记录ID").formula == "IFBLANK([手动编号],[自动编号])"
    assert all(
        field.field_type == FORMULA
        for field in (
            influencers.field("邮箱索引"),
            influencers.field("开发信"),
            cooperation.field("邮箱索引"),
            cooperation.field("合作记录ID"),
        )
    )

    auto_numbers = {
        (table.name, field.name): (field.auto_number_prefix, field.auto_number_length)
        for table in FULL_SCHEMA.tables
        for field in table.fields
        if field.field_type == AUTO_NUMBER
    }
    assert auto_numbers == {
        (COOPERATION_TABLE, "自动编号"): ("COOP-", 6),
        (SAMPLE_TRACKING_TABLE, "样品记录ID"): ("SMP-", 6),
        (COOPERATION_CONTENT_TABLE, "内容记录ID"): ("CNT-", 6),
    }


def test_outreach_letter_template_is_verbatim_and_formula_is_dynamic_and_gated():
    assert OUTREACH_LETTER_TEMPLATE == '''Hello “KOL Name”,

This is Sam from Brand. We came across your video "代表视频标题" and found it very helpful.

**Here are a few highlights:**
1.
2.
3.

We'd be glad to send you a free sample. Would you be interested in doing a review video?

Best regards,
Sam | Marketing Team'''
    assert "[KOL Name]" in OUTREACH_LETTER_FORMULA
    assert "[代表视频标题]" in OUTREACH_LETTER_FORMULA
    assert "[Channel ID]" in OUTREACH_LETTER_FORMULA
    assert '"手动确认"' in OUTREACH_LETTER_FORMULA
    assert OUTREACH_LETTER_FORMULA.count("CHAR(10)") >= 8


def test_select_date_and_number_metadata_needed_by_runtime_is_present():
    cooperation = FULL_SCHEMA.table(COOPERATION_TABLE)
    sample = FULL_SCHEMA.table(SAMPLE_TRACKING_TABLE)
    content = FULL_SCHEMA.table(COOPERATION_CONTENT_TABLE)

    assert FULL_SCHEMA.table(SEARCH_TASKS_TABLE).field("排序策略").options == (
        "relevance",
        "viewCount",
        "date",
        "rating",
        "title",
    )
    assert {"成功", "部分成功", "失败", "中断", "异常", "配额中断", "预算不足"}.issubset(
        set(FULL_SCHEMA.table(SEARCH_TASKS_TABLE).field("执行状态").options)
    )
    assert FULL_SCHEMA.table(INFLUENCERS_TABLE).field("邮箱状态").options == (
        "已获取",
        "需手动查找",
    )

    assert cooperation.field("合作状态").options == (
        "待确认方案",
        "已确认",
        "执行中",
        "待发布/验收",
        "已完成",
        "已取消",
    )
    assert sample.field("样品状态").options == (
        "待收件信息",
        "待寄出",
        "已寄出",
        "已签收",
        "异常",
        "已取消",
    )
    assert sample.field("发货日期").date_format == "yyyy-MM-dd"
    assert sample.field("最新物流时间").date_format == "yyyy-MM-dd HH:mm"
    assert content.field("Publish Time").date_format == "yyyy-MM-dd HH:mm"
    assert content.field("Views").number_format == "1,000"
    assert content.field("互动率(%)").number_format == "0.00"


def test_schema_objects_and_exported_table_mapping_are_immutable():
    with pytest.raises(FrozenInstanceError):
        FULL_SCHEMA.tables[0].name = "changed"
    with pytest.raises(TypeError):
        TABLE_SPECS["changed"] = FULL_SCHEMA.tables[0]


def test_cli_json_expresses_complex_field_metadata_without_numeric_payloads():
    influencers = FULL_SCHEMA.table(INFLUENCERS_TABLE)
    cooperation = FULL_SCHEMA.table(COOPERATION_TABLE)
    sample = FULL_SCHEMA.table(SAMPLE_TRACKING_TABLE)

    formula = influencers.field("邮箱索引").cli_json()
    assert formula == {
        "type": "formula",
        "name": "邮箱索引",
        "description": "规范化联系邮箱，供合作表查找引用。",
        "expression": "LOWER(TRIM([联系邮箱]))",
    }

    lookup = cooperation.field("Channel ID").cli_json()
    assert lookup["type"] == "lookup"
    assert lookup["from"] == INFLUENCERS_TABLE
    assert lookup["where"]["conditions"] == [
        ["邮箱索引", "==", {"type": "field_ref", "field": "邮箱索引"}]
    ]

    auto_number = sample.field("样品记录ID").cli_json()
    assert auto_number["style"]["rules"] == [
        {"type": "text", "text": "SMP-"},
        {"type": "incremental_number", "length": 6},
    ]
