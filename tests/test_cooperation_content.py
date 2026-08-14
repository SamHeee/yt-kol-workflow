import pytest

from feishu.cooperation_content import (
    COOPERATION_CONTENT_FIELDS,
    COOPERATION_RECORD_ID_FIELD,
    CONTENT_STATUS_FIELD,
    CONTENT_STATUS_OPTIONS,
    CooperationContentSchemaError,
    CHANNEL_ID_FIELD,
    COMMENTS_FIELD,
    DATA_UPDATED_AT_FIELD,
    ENGAGEMENT_RATE_FIELD,
    KOL_NAME_FIELD,
    LIKES_FIELD,
    LOOKUP_READ_ONLY_FIELDS,
    LOOKUP_SPECS,
    NEVER_SYSTEM_WRITABLE_FIELDS,
    PUBLISH_TIME_FIELD,
    SAMPLE_RECORD_ID_FIELD,
    SYNC_STATUS_FIELD,
    SYNC_STATUS_OPTIONS,
    SYSTEM_WRITABLE_FIELDS,
    VIEWS_FIELD,
    field_names,
    validate_cooperation_content_schema,
    validate_cooperation_content_metadata,
    validate_system_update_fields,
)


def _field_metadata():
    return [
        {"field_name": name, "type": field_type}
        for name, field_type in COOPERATION_CONTENT_FIELDS
    ]


def _full_metadata():
    fields = []
    for index, (name, field_type) in enumerate(COOPERATION_CONTENT_FIELDS):
        item = {
            "field_name": name,
            "field_id": f"fld_content_{index}",
            "type": field_type,
            "is_primary": index == 0,
        }
        fields.append(item)

    by_name = {item["field_name"]: item for item in fields}
    by_name["内容记录ID"]["property"] = {
        "auto_serial": {
            "options": [
                {"type": "fixed_text", "value": "CNT-"},
                {"type": "system_number", "value": "6"},
            ]
        }
    }
    by_name[CONTENT_STATUS_FIELD]["property"] = {
        "options": [{"name": name} for name in CONTENT_STATUS_OPTIONS]
    }
    by_name[SYNC_STATUS_FIELD]["property"] = {
        "options": [{"name": name} for name in SYNC_STATUS_OPTIONS]
    }
    for field_name in (VIEWS_FIELD, LIKES_FIELD, COMMENTS_FIELD):
        by_name[field_name]["property"] = {"formatter": "1,000"}
    by_name[ENGAGEMENT_RATE_FIELD]["property"] = {"formatter": "0.00"}
    for field_name in (PUBLISH_TIME_FIELD, DATA_UPDATED_AT_FIELD):
        by_name[field_name]["property"] = {
            "auto_fill": False,
            "date_formatter": "yyyy-MM-dd HH:mm",
        }

    cooperation_fields = [
        {"field_name": "自动编号", "field_id": "fld_coop_auto"},
        {"field_name": "合作记录ID", "field_id": "fld_coop_record"},
        {"field_name": "Channel ID", "field_id": "fld_coop_channel"},
        {"field_name": "KOL Name", "field_id": "fld_coop_kol"},
    ]
    sample_fields = [
        {"field_name": "合作自动编号", "field_id": "fld_sample_coop"},
        {"field_name": "样品记录ID", "field_id": "fld_sample_record"},
    ]
    expectations = {
        "合作记录ID": ("tbl_coop", "fld_coop_record", "fld_coop_auto"),
        "Channel ID": ("tbl_coop", "fld_coop_channel", "fld_coop_auto"),
        "KOL Name": ("tbl_coop", "fld_coop_kol", "fld_coop_auto"),
        "样品记录ID": ("tbl_sample", "fld_sample_record", "fld_sample_coop"),
    }
    current_key = by_name["合作自动编号"]["field_id"]
    for field_name, (table_id, target_field, source_key) in expectations.items():
        by_name[field_name]["property"] = {
            "filter_info": {
                "target_table": table_id,
                "conditions": {
                    "conjunction": "and",
                    "children": [
                        {
                            "field_id": source_key,
                            "operator": "is",
                            "value": None,
                        }
                    ],
                },
            },
            "formatter": "",
            "target_field": target_field,
            "formula": "|".join((table_id, target_field, source_key, current_key)),
            "roll_up": 0,
        }
    return fields, cooperation_fields, sample_fields


def test_contract_has_exactly_19_fields_and_blank_primary_first():
    assert len(COOPERATION_CONTENT_FIELDS) == 19
    assert COOPERATION_CONTENT_FIELDS[0] == ("多行文本", 1)
    assert COOPERATION_CONTENT_FIELDS[1] == ("内容记录ID", 1005)
    assert dict(COOPERATION_CONTENT_FIELDS)[CHANNEL_ID_FIELD] == 19
    assert dict(COOPERATION_CONTENT_FIELDS)[SAMPLE_RECORD_ID_FIELD] == 19
    assert field_names()[-2:] == ("数据同步状态", "同步错误")


def test_lookup_specs_use_stable_cooperation_number_only():
    cooperation_lookups = {COOPERATION_RECORD_ID_FIELD, CHANNEL_ID_FIELD, KOL_NAME_FIELD}
    assert LOOKUP_READ_ONLY_FIELDS == cooperation_lookups | {SAMPLE_RECORD_ID_FIELD}
    for field_name in cooperation_lookups:
        spec = LOOKUP_SPECS[field_name]
        assert spec.source_table == "合作红人表"
        assert spec.conditions == (("自动编号", "合作自动编号"),)
        assert spec.conjunction == "AND"
        assert spec.aggregation == "原始值"

    sample_spec = LOOKUP_SPECS[SAMPLE_RECORD_ID_FIELD]
    assert sample_spec.source_table == "样品跟踪表"
    assert sample_spec.return_field == SAMPLE_RECORD_ID_FIELD
    assert sample_spec.conditions == (("合作自动编号", "合作自动编号"),)


def test_system_write_whitelist_excludes_keys_url_lookup_and_primary():
    assert NEVER_SYSTEM_WRITABLE_FIELDS.isdisjoint(SYSTEM_WRITABLE_FIELDS)
    assert "视频URL" not in SYSTEM_WRITABLE_FIELDS
    assert "合作自动编号" not in SYSTEM_WRITABLE_FIELDS
    assert "Channel ID" not in SYSTEM_WRITABLE_FIELDS
    assert "内容状态" in SYSTEM_WRITABLE_FIELDS

    assert validate_system_update_fields({"Views": 123}) == {"Views": 123}
    with pytest.raises(ValueError, match="禁止写入字段"):
        validate_system_update_fields({"视频URL": "https://youtu.be/abcdefghijk"})


def test_schema_validator_checks_names_types_and_order():
    validate_cooperation_content_schema(_field_metadata())

    wrong_type = _field_metadata()
    wrong_type[1] = {"field_name": "内容记录ID", "type": 1}
    with pytest.raises(CooperationContentSchemaError, match="字段类型错误"):
        validate_cooperation_content_schema(wrong_type)

    wrong_order = _field_metadata()
    wrong_order[2], wrong_order[3] = wrong_order[3], wrong_order[2]
    with pytest.raises(CooperationContentSchemaError, match="字段顺序"):
        validate_cooperation_content_schema(wrong_order)


def test_schema_validator_can_allow_unmanaged_extra_fields_explicitly():
    fields = _field_metadata() + [{"field_name": "用户扩展字段", "type": 1}]
    with pytest.raises(CooperationContentSchemaError, match="合同外字段"):
        validate_cooperation_content_schema(fields)
    validate_cooperation_content_schema(fields, allow_extra=True)


def test_full_metadata_validator_accepts_only_exact_remote_contract():
    fields, cooperation_fields, sample_fields = _full_metadata()
    validate_cooperation_content_metadata(
        fields,
        cooperation_table_id="tbl_coop",
        cooperation_fields=cooperation_fields,
        sample_table_id="tbl_sample",
        sample_fields=sample_fields,
    )

    fields[3]["property"]["filter_info"]["target_table"] = "tbl_wrong"
    with pytest.raises(CooperationContentSchemaError, match="来源表错误"):
        validate_cooperation_content_metadata(
            fields,
            cooperation_table_id="tbl_coop",
            cooperation_fields=cooperation_fields,
            sample_table_id="tbl_sample",
            sample_fields=sample_fields,
        )
def test_full_metadata_validator_can_allow_operator_column_reordering():
    fields, cooperation_fields, sample_fields = _full_metadata()
    video_url = fields.pop(8)
    fields.insert(3, video_url)

    with pytest.raises(CooperationContentSchemaError, match="字段顺序"):
        validate_cooperation_content_metadata(
            fields,
            cooperation_table_id="tbl_coop",
            cooperation_fields=cooperation_fields,
            sample_table_id="tbl_sample",
            sample_fields=sample_fields,
        )

    validate_cooperation_content_metadata(
        fields,
        cooperation_table_id="tbl_coop",
        cooperation_fields=cooperation_fields,
        sample_table_id="tbl_sample",
        sample_fields=sample_fields,
        strict_order=False,
    )

    by_name = {item["field_name"]: item for item in fields}
    by_name[CHANNEL_ID_FIELD]["property"]["filter_info"]["target_table"] = "tbl_wrong"
    with pytest.raises(CooperationContentSchemaError, match="来源表错误"):
        validate_cooperation_content_metadata(
            fields,
            cooperation_table_id="tbl_coop",
            cooperation_fields=cooperation_fields,
            sample_table_id="tbl_sample",
            sample_fields=sample_fields,
            strict_order=False,
        )


def test_full_metadata_validator_rejects_number_rule_and_option_drift():
    fields, cooperation_fields, sample_fields = _full_metadata()
    fields[1]["property"]["auto_serial"]["options"][1]["value"] = "5"
    fields[7]["property"]["options"] = [{"name": "错误状态"}]

    with pytest.raises(CooperationContentSchemaError, match="自动编号规则"):
        validate_cooperation_content_metadata(
            fields,
            cooperation_table_id="tbl_coop",
            cooperation_fields=cooperation_fields,
            sample_table_id="tbl_sample",
            sample_fields=sample_fields,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda lookup: lookup["filter_info"]["conditions"]["children"][0].update(
                operator="contains"
            ),
            "操作符不是 is",
        ),
        (
            lambda lookup: lookup["filter_info"]["conditions"]["children"][0].update(
                value="unexpected"
            ),
            "value 必须为 null",
        ),
        (lambda lookup: lookup.update(roll_up=1), "roll_up=0/raw_value"),
        (
            lambda lookup: lookup.update(
                formula=lookup["formula"].replace(
                    "fld_coop_auto", "fld_coop_auto_extra"
                )
            ),
            "查找公式未引用正确字段",
        ),
    ],
)
def test_full_metadata_validator_rejects_unsafe_lookup_details(mutation, message):
    fields, cooperation_fields, sample_fields = _full_metadata()
    mutation(fields[3]["property"])

    with pytest.raises(CooperationContentSchemaError, match=message):
        validate_cooperation_content_metadata(
            fields,
            cooperation_table_id="tbl_coop",
            cooperation_fields=cooperation_fields,
            sample_table_id="tbl_sample",
            sample_fields=sample_fields,
        )


@pytest.mark.parametrize(
    ("field_name", "property_update", "message"),
    [
        (VIEWS_FIELD, {"formatter": "0"}, "Views数字格式错误"),
        (LIKES_FIELD, {"formatter": "0"}, "Likes数字格式错误"),
        (COMMENTS_FIELD, {"formatter": "0"}, "Comments数字格式错误"),
        (ENGAGEMENT_RATE_FIELD, {"formatter": "0.0"}, "互动率.*数字格式错误"),
        (
            PUBLISH_TIME_FIELD,
            {"date_formatter": "yyyy-MM-dd"},
            "Publish Time日期格式错误",
        ),
        (DATA_UPDATED_AT_FIELD, {"auto_fill": True}, "数据更新时间禁止自动填充"),
    ],
)
def test_full_metadata_validator_rejects_formatter_drift(
    field_name, property_update, message
):
    fields, cooperation_fields, sample_fields = _full_metadata()
    by_name = {item["field_name"]: item for item in fields}
    by_name[field_name]["property"].update(property_update)

    with pytest.raises(CooperationContentSchemaError, match=message):
        validate_cooperation_content_metadata(
            fields,
            cooperation_table_id="tbl_coop",
            cooperation_fields=cooperation_fields,
            sample_table_id="tbl_sample",
            sample_fields=sample_fields,
        )
