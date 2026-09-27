#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Declarative contract for a complete seven-table Feishu deployment.

This module is deliberately separate from :mod:`feishu.schema`.  The latter is
the stable four-table discovery/Excel contract; adding operational fields to it
would change generated workbook columns.  ``FULL_SCHEMA`` layers the two
operational fields on ``网红详情表`` and the three operations tables on top of
that contract so a new Base can be provisioned without changing Excel output.

Only metadata lives here.  Provisioning, transport, repair policy and workflow
enablement belong to the setup layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from .cooperation_content import (
    CONTENT_STATUS_FIELD,
    CONTENT_STATUS_OPTIONS,
    COOPERATION_CONTENT_FIELDS,
    COOPERATION_CONTENT_TABLE,
    LOOKUP_SPECS as CONTENT_LOOKUP_SPECS,
    SYNC_STATUS_FIELD,
    SYNC_STATUS_OPTIONS,
)
from .sample_logistics import (
    LOGISTICS_STATUS_FIELD,
    LOGISTICS_STATUS_OPTIONS,
    REMINDER_STATUS_FIELD,
    REMINDER_STATUS_OPTIONS,
    REMINDER_TYPE_FIELD,
    REMINDER_TYPE_OPTIONS,
    SAMPLE_LOGISTICS_FIELDS,
    SAMPLE_STATUS_FIELD,
    SAMPLE_STATUS_OPTIONS,
    SAMPLE_TRACKING_TABLE,
)
from .schema import (
    INFLUENCERS_FIELDS,
    INFLUENCERS_FIELD_OPTIONS,
    INFLUENCERS_TABLE,
    INFLUENCER_VIDEOS_FIELDS,
    INFLUENCER_VIDEOS_TABLE,
    PRIMARY_FIELD_NAME,
    SEARCH_TASKS_FIELDS,
    SEARCH_TASKS_TABLE,
    SEARCH_VIDEOS_FIELDS,
    SEARCH_VIDEOS_TABLE,
)


# Feishu field type IDs used by the existing runtime clients and validators.
TEXT = 1
NUMBER = 2
SINGLE_SELECT = 3
DATE = 5
CHECKBOX = 7
URL = 15
LOOKUP = 19
FORMULA = 20
CREATED_TIME = 1001
PERSON = 11
AUTO_NUMBER = 1005


@dataclass(frozen=True)
class LookupConditionSpec:
    """One source-field/current-field equality in a lookup definition."""

    source_field: str
    current_field: str
    operator: str = "=="


@dataclass(frozen=True)
class LookupSpec:
    """Name-based lookup contract, resolved to remote IDs at provision time."""

    source_table: str
    select_field: str
    conditions: Tuple[LookupConditionSpec, ...]
    logic: str = "and"
    aggregate: str = "raw_value"


@dataclass(frozen=True)
class FieldSpec:
    """Complete logical definition for one physical Base field."""

    name: str
    field_type: int
    options: Tuple[str, ...] = ()
    date_format: Optional[str] = None
    number_format: Optional[str] = None
    text_style: Optional[str] = None
    multiple: Optional[bool] = None
    auto_number_prefix: Optional[str] = None
    auto_number_length: Optional[int] = None
    formula: Optional[str] = None
    lookup: Optional[LookupSpec] = None
    description: str = ""
    is_primary: bool = False
    is_operations_extension: bool = False
    hidden_in_default_view: bool = False

    @property
    def is_lookup(self) -> bool:
        return self.lookup is not None

    @property
    def numeric_type(self) -> int:
        """Compatibility name used by the strict provisioning planner."""

        return self.field_type

    @property
    def primary(self) -> bool:
        return self.is_primary

    @property
    def phase(self) -> str:
        if self.lookup is not None:
            return "lookup"
        if self.field_type == AUTO_NUMBER:
            return "auto_number"
        if self.formula is not None:
            return "formula"
        return "ordinary"

    def cli_json(self) -> dict[str, Any]:
        """Return the lark-cli typed field JSON for this contract field."""

        type_names = {
            TEXT: "text",
            NUMBER: "number",
            SINGLE_SELECT: "select",
            DATE: "datetime",
            CHECKBOX: "checkbox",
            URL: "text",
            CREATED_TIME: "created_at",
            PERSON: "user",
            AUTO_NUMBER: "auto_number",
            FORMULA: "formula",
            LOOKUP: "lookup",
        }
        try:
            field_type = type_names[self.field_type]
        except KeyError as exc:
            raise ValueError(f"unsupported field type: {self.field_type}") from exc

        payload: dict[str, Any] = {"type": field_type, "name": self.name}
        if self.description:
            payload["description"] = self.description
        if field_type == "text" and (self.text_style or self.field_type == URL):
            payload["style"] = {
                "type": self.text_style or "url",
            }
        elif field_type == "number" and self.number_format:
            if self.number_format == "1,000":
                payload["style"] = {
                    "type": "plain",
                    "precision": 0,
                    "percentage": False,
                    "thousands_separator": True,
                }
            elif self.number_format == "0.00":
                payload["style"] = {
                    "type": "plain",
                    "precision": 2,
                    "percentage": False,
                    "thousands_separator": False,
                }
        elif field_type == "select":
            payload["multiple"] = bool(self.multiple)
            payload["options"] = [{"name": option} for option in self.options]
        elif field_type == "datetime" and self.date_format:
            payload["style"] = {"format": self.date_format}
        elif field_type == "user":
            payload["multiple"] = bool(self.multiple)
        elif field_type == "auto_number":
            payload["style"] = {
                "rules": [
                    {"type": "text", "text": self.auto_number_prefix or ""},
                    {
                        "type": "incremental_number",
                        "length": int(self.auto_number_length or 6),
                    },
                ]
            }
        elif field_type == "formula":
            payload["expression"] = self.formula
        elif field_type == "lookup":
            if self.lookup is None:
                raise ValueError(f"lookup field {self.name} is missing lookup metadata")
            payload.update(
                {
                    "from": self.lookup.source_table,
                    "select": self.lookup.select_field,
                    "where": {
                        "logic": self.lookup.logic,
                        "conditions": [
                            [
                                condition.source_field,
                                condition.operator,
                                {
                                    "type": "field_ref",
                                    "field": condition.current_field,
                                },
                            ]
                            for condition in self.lookup.conditions
                        ],
                    },
                    "aggregate": self.lookup.aggregate,
                }
            )
        return payload


@dataclass(frozen=True)
class ViewSpec:
    """MVP view contract: one unfiltered default grid per table."""

    name: str = "表格"
    view_type: str = "grid"
    is_default: bool = True


@dataclass(frozen=True)
class TableSpec:
    name: str
    fields: Tuple[FieldSpec, ...]
    dependencies: Tuple[str, ...] = ()
    default_view: ViewSpec = field(default_factory=ViewSpec)
    excel_contract: bool = False

    def field(self, name: str) -> FieldSpec:
        for spec in self.fields:
            if spec.name == name:
                return spec
        raise KeyError(f"{self.name} has no field named {name!r}")


@dataclass(frozen=True)
class FullSchema:
    tables: Tuple[TableSpec, ...]

    def table(self, name: str) -> TableSpec:
        for spec in self.tables:
            if spec.name == name:
                return spec
        raise KeyError(f"unknown table: {name!r}")

    @property
    def table_names(self) -> Tuple[str, ...]:
        return tuple(table.name for table in self.tables)

    @property
    def field_count(self) -> int:
        return sum(len(table.fields) for table in self.tables)

    @property
    def lookup_count(self) -> int:
        return sum(field.is_lookup for table in self.tables for field in table.fields)

    @property
    def view_count(self) -> int:
        return sum(1 for table in self.tables if table.default_view)

    @property
    def field_phases(self) -> Tuple[str, ...]:
        return ("ordinary", "auto_number", "formula", "lookup")

    @property
    def workflow_title(self) -> str:
        return "首次进入合作自动建档"

    @property
    def influencer_table(self) -> str:
        return INFLUENCERS_TABLE

    @property
    def cooperation_table(self) -> str:
        return COOPERATION_TABLE


def _freeze_properties(properties: Optional[Mapping[str, Mapping[str, Any]]]) -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType({name: MappingProxyType(dict(value)) for name, value in (properties or {}).items()})


def _fields_from_pairs(
    pairs: Tuple[Tuple[str, int], ...] | list[Tuple[str, int]],
    *,
    select_options: Optional[Mapping[str, Tuple[str, ...] | list[str]]] = None,
    properties: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[FieldSpec, ...]:
    """Turn an existing field-pair SSOT into richer immutable field specs."""

    option_map = select_options or {}
    property_map = _freeze_properties(properties)
    result = []
    for index, (name, field_type) in enumerate(pairs):
        extra = dict(property_map.get(name, {}))
        options = tuple(option_map.get(name, ()))
        result.append(
            FieldSpec(
                name=name,
                field_type=field_type,
                options=options,
                is_primary=index == 0 and name == PRIMARY_FIELD_NAME,
                **extra,
            )
        )
    return tuple(result)


OUTREACH_LETTER_TEMPLATE = '''Hello “KOL Name”,

This is Sam from Brand. We came across your video "代表视频标题" and found it very helpful.

**Here are a few highlights:**
1.
2.
3.

We'd be glad to send you a free sample. Would you be interested in doing a review video?

Best regards,
Sam | Marketing Team'''

# The user-provided text above is the durable content SSOT.  The expression
# preserves its two placeholder locations as dynamic field references and gates
# output until all required enrichment data is available.
OUTREACH_LETTER_FORMULA = (
    'IF(AND(NOT(ISBLANK([Channel ID])),NOT(ISBLANK([KOL Name])),'
    '[KOL Name]!="手动确认",NOT(ISBLANK([代表视频标题]))),'
    '"Hello “"&[KOL Name]&"”,"&CHAR(10)&CHAR(10)&'
    '"This is Sam from Brand. We came across your video "&CHAR(34)&[代表视频标题]&'
    'CHAR(34)&" and found it very helpful."&CHAR(10)&CHAR(10)&'
    '"**Here are a few highlights:**"&CHAR(10)&"1."&CHAR(10)&"2."&CHAR(10)&"3."&CHAR(10)&CHAR(10)&'
    '"We\'d be glad to send you a free sample. Would you be interested in doing a review video?"&'
    'CHAR(10)&CHAR(10)&"Best regards,"&CHAR(10)&"Sam | Marketing Team","")'
)

EMAIL_INDEX_FORMULA = "LOWER(TRIM([联系邮箱]))"
COOPERATION_RECORD_ID_FORMULA = "IFBLANK([手动编号],[自动编号])"


INFLUENCER_OPERATIONS_FIELDS = (
    FieldSpec(
        "邮箱索引",
        FORMULA,
        formula=EMAIL_INDEX_FORMULA,
        description="规范化联系邮箱，供合作表查找引用。",
        is_operations_extension=True,
        hidden_in_default_view=True,
    ),
    FieldSpec(
        "开发信",
        FORMULA,
        formula=OUTREACH_LETTER_FORMULA,
        description="仅生成草稿，不自动发送。",
        is_operations_extension=True,
    ),
)

CORE_SELECT_OPTIONS = MappingProxyType(
    {
        "排序策略": ("relevance", "viewCount", "date", "rating", "title"),
        "执行状态": (
            "进行中",
            "成功",
            "部分成功",
            "失败",
            "中断",
            "异常",
            "配额中断",
            "预算不足",
        ),
        "邮箱状态": ("已获取", "需手动查找"),
    }
)


COOPERATION_TABLE = "合作红人表"
COOPERATION_STATUS_OPTIONS = (
    "待确认方案",
    "已确认",
    "执行中",
    "待发布/验收",
    "已完成",
    "已取消",
)
COOPERATION_TYPE_OPTIONS = (
    "免费置换",
    "纯佣",
    "固定费用",
    "固定费用+纯佣",
    "其他",
)

COOPERATION_FIELDS = (
    FieldSpec(PRIMARY_FIELD_NAME, TEXT, is_primary=True),
    FieldSpec("合作记录ID", FORMULA, formula=COOPERATION_RECORD_ID_FORMULA),
    FieldSpec("联系邮箱", TEXT, text_style="email"),
    FieldSpec(
        "Channel Name",
        LOOKUP,
        lookup=LookupSpec(
            INFLUENCERS_TABLE,
            "Channel Name",
            (LookupConditionSpec("邮箱索引", "邮箱索引"),),
        ),
    ),
    FieldSpec(
        "KOL Name",
        LOOKUP,
        lookup=LookupSpec(
            INFLUENCERS_TABLE,
            "KOL Name",
            (LookupConditionSpec("邮箱索引", "邮箱索引"),),
        ),
    ),
    FieldSpec(
        "Channel ID",
        LOOKUP,
        lookup=LookupSpec(
            INFLUENCERS_TABLE,
            "Channel ID",
            (LookupConditionSpec("邮箱索引", "邮箱索引"),),
        ),
    ),
    FieldSpec("合作项目", TEXT),
    FieldSpec("合作形式", SINGLE_SELECT, options=COOPERATION_TYPE_OPTIONS),
    FieldSpec("合作费用", NUMBER, number_format="0.00"),
    FieldSpec("专属code", TEXT),
    FieldSpec("收件信息", TEXT),
    FieldSpec("合作状态", SINGLE_SELECT, options=COOPERATION_STATUS_OPTIONS),
    FieldSpec("合作负责人", PERSON, multiple=False),
    FieldSpec("下一节点", TEXT),
    FieldSpec("下一节点日期", DATE, date_format="yyyy-MM-dd"),
    FieldSpec("交付/发布链接", URL, text_style="url"),
    FieldSpec("备注", TEXT),
    FieldSpec(
        "自动编号",
        AUTO_NUMBER,
        auto_number_prefix="COOP-",
        auto_number_length=6,
    ),
    FieldSpec("手动编号", TEXT),
    FieldSpec(
        "邮箱索引",
        FORMULA,
        formula=EMAIL_INDEX_FORMULA,
        hidden_in_default_view=True,
    ),
)


SAMPLE_LOOKUP_SPECS = MappingProxyType(
    {
        "联系邮箱": LookupSpec(
            COOPERATION_TABLE,
            "联系邮箱",
            (LookupConditionSpec("自动编号", "合作自动编号"),),
        ),
        "收件信息": LookupSpec(
            COOPERATION_TABLE,
            "收件信息",
            (LookupConditionSpec("自动编号", "合作自动编号"),),
        ),
    }
)

SAMPLE_FIELD_PROPERTIES = {
    "样品记录ID": {"auto_number_prefix": "SMP-", "auto_number_length": 6},
    "样品成本": {"number_format": "0.00"},
    "样品运费": {"number_format": "0.00"},
    "联系邮箱": {"lookup": SAMPLE_LOOKUP_SPECS["联系邮箱"]},
    "收件信息": {"lookup": SAMPLE_LOOKUP_SPECS["收件信息"]},
    "发货日期": {"date_format": "yyyy-MM-dd"},
    "签收日期": {"date_format": "yyyy-MM-dd"},
    "最新物流时间": {"date_format": "yyyy-MM-dd HH:mm"},
    "物流停滞起算时间": {"date_format": "yyyy-MM-dd HH:mm"},
    "物流源同步时间": {"date_format": "yyyy-MM-dd HH:mm"},
    "上次查询时间": {"date_format": "yyyy-MM-dd HH:mm"},
    "提醒发送时间": {"date_format": "yyyy-MM-dd HH:mm"},
}

SAMPLE_SELECT_OPTIONS = {
    SAMPLE_STATUS_FIELD: SAMPLE_STATUS_OPTIONS,
    LOGISTICS_STATUS_FIELD: LOGISTICS_STATUS_OPTIONS,
    REMINDER_TYPE_FIELD: REMINDER_TYPE_OPTIONS,
    REMINDER_STATUS_FIELD: REMINDER_STATUS_OPTIONS,
}


CONTENT_FIELD_PROPERTIES = {
    "内容记录ID": {"auto_number_prefix": "CNT-", "auto_number_length": 6},
    "Publish Time": {"date_format": "yyyy-MM-dd HH:mm"},
    "Views": {"number_format": "1,000"},
    "Likes": {"number_format": "1,000"},
    "Comments": {"number_format": "1,000"},
    "互动率(%)": {"number_format": "0.00"},
    "归因点击": {"number_format": "1,000"},
    "归因GMV": {"number_format": "0.00"},
    "归因订单量": {"number_format": "1,000"},
    "归因销量": {"number_format": "1,000"},
    "数据更新时间": {"date_format": "yyyy-MM-dd HH:mm"},
}

CONTENT_SELECT_OPTIONS = {
    CONTENT_STATUS_FIELD: CONTENT_STATUS_OPTIONS,
    SYNC_STATUS_FIELD: SYNC_STATUS_OPTIONS,
}

for _field_name, _lookup in CONTENT_LOOKUP_SPECS.items():
    CONTENT_FIELD_PROPERTIES[_field_name] = {
        "lookup": LookupSpec(
            source_table=_lookup.source_table,
            select_field=_lookup.return_field,
            conditions=tuple(
                LookupConditionSpec(source, current)
                for source, current in _lookup.conditions
            ),
            logic=_lookup.conjunction.lower(),
            aggregate="raw_value",
        )
    }


SEARCH_TASK_TABLE_SPEC = TableSpec(
    SEARCH_TASKS_TABLE,
    _fields_from_pairs(
        tuple(SEARCH_TASKS_FIELDS),
        select_options=CORE_SELECT_OPTIONS,
    ),
    excel_contract=True,
)
SEARCH_VIDEO_TABLE_SPEC = TableSpec(
    SEARCH_VIDEOS_TABLE,
    _fields_from_pairs(tuple(SEARCH_VIDEOS_FIELDS)),
    dependencies=(SEARCH_TASKS_TABLE,),
    excel_contract=True,
)
INFLUENCER_TABLE_SPEC = TableSpec(
    INFLUENCERS_TABLE,
    _fields_from_pairs(
        tuple(INFLUENCERS_FIELDS),
        select_options={**CORE_SELECT_OPTIONS, **INFLUENCERS_FIELD_OPTIONS},
    )
    + INFLUENCER_OPERATIONS_FIELDS,
    dependencies=(SEARCH_VIDEO_TABLE_SPEC.name,),
    excel_contract=True,
)
INFLUENCER_VIDEO_TABLE_SPEC = TableSpec(
    INFLUENCER_VIDEOS_TABLE,
    _fields_from_pairs(tuple(INFLUENCER_VIDEOS_FIELDS)),
    dependencies=(INFLUENCERS_TABLE,),
    excel_contract=True,
)
COOPERATION_TABLE_SPEC = TableSpec(
    COOPERATION_TABLE,
    COOPERATION_FIELDS,
    dependencies=(INFLUENCERS_TABLE,),
)
SAMPLE_TABLE_SPEC = TableSpec(
    SAMPLE_TRACKING_TABLE,
    _fields_from_pairs(
        tuple(SAMPLE_LOGISTICS_FIELDS),
        select_options=SAMPLE_SELECT_OPTIONS,
        properties=SAMPLE_FIELD_PROPERTIES,
    ),
    dependencies=(COOPERATION_TABLE,),
)
CONTENT_TABLE_SPEC = TableSpec(
    COOPERATION_CONTENT_TABLE,
    _fields_from_pairs(
        tuple(COOPERATION_CONTENT_FIELDS),
        select_options=CONTENT_SELECT_OPTIONS,
        properties=CONTENT_FIELD_PROPERTIES,
    ),
    dependencies=(COOPERATION_TABLE, SAMPLE_TRACKING_TABLE),
)


# Topological order is intentional: a provisioner can iterate this tuple and
# create every lookup only after the referenced table and ordinary key fields
# exist.  The MVP keeps a single default grid view for every table.
FULL_SCHEMA = FullSchema(
    tables=(
        SEARCH_TASK_TABLE_SPEC,
        SEARCH_VIDEO_TABLE_SPEC,
        INFLUENCER_TABLE_SPEC,
        INFLUENCER_VIDEO_TABLE_SPEC,
        COOPERATION_TABLE_SPEC,
        SAMPLE_TABLE_SPEC,
        CONTENT_TABLE_SPEC,
    )
)

TABLE_SPECS = MappingProxyType({table.name: table for table in FULL_SCHEMA.tables})

__all__ = [
    "AUTO_NUMBER",
    "CONTENT_TABLE_SPEC",
    "COOPERATION_FIELDS",
    "COOPERATION_TABLE",
    "COOPERATION_TABLE_SPEC",
    "EMAIL_INDEX_FORMULA",
    "FORMULA",
    "FULL_SCHEMA",
    "FieldSpec",
    "FullSchema",
    "INFLUENCER_OPERATIONS_FIELDS",
    "INFLUENCER_TABLE_SPEC",
    "LOOKUP",
    "LookupConditionSpec",
    "LookupSpec",
    "OUTREACH_LETTER_FORMULA",
    "OUTREACH_LETTER_TEMPLATE",
    "SAMPLE_LOOKUP_SPECS",
    "SAMPLE_TABLE_SPEC",
    "TABLE_SPECS",
    "TableSpec",
    "ViewSpec",
]
