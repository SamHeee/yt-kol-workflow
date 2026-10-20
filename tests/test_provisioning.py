from dataclasses import dataclass

import pytest

from feishu.provisioning import (
    ProvisioningConflict,
    SevenTableProvisioner,
    cleanup_new_base_default_tables,
)


@dataclass(frozen=True)
class FakeField:
    name: str
    numeric_type: int = 1
    primary: bool = False
    phase: str = "ordinary"
    hidden_in_default_view: bool = False

    def cli_json(self):
        return {"name": self.name, "type": "text"}


@dataclass(frozen=True)
class FakeSelectField:
    name: str
    options: tuple
    numeric_type: int = 3
    primary: bool = False
    phase: str = "ordinary"
    hidden_in_default_view: bool = False

    def cli_json(self):
        return {
            "name": self.name,
            "type": "select",
            "multiple": False,
            "options": [{"name": name} for name in self.options],
        }


@dataclass(frozen=True)
class FakeTable:
    name: str
    fields: tuple


@dataclass(frozen=True)
class FakeSchema:
    tables: tuple
    field_phases: tuple = ("ordinary",)
    workflow_title: str = "首次进入合作自动建档"
    influencer_table: str = "网红详情表"
    cooperation_table: str = "合作红人表"


SCHEMA = FakeSchema(
    tables=(
        FakeTable(
            "搜索任务表",
            (
                FakeField("多行文本", primary=True),
                FakeField("搜索关键词"),
            ),
        ),
    )
)


class FakeAdmin:
    def __init__(self, tables=None, fields=None, workflows=None):
        self.tables = list(tables or [])
        self.fields = dict(fields or {})
        self.workflows = list(workflows or [])
        self.calls = []

    def list_tables(self):
        return {"items": self.tables}

    def list_fields(self, table_id):
        return {"items": self.fields.get(table_id, [])}

    def list_workflows(self):
        return {"items": self.workflows}

    def get_field(self, table_id, field_id):
        for item in self.fields.get(table_id, []):
            if item.get("field_id") == field_id or item.get("field_name") == field_id:
                return item
        raise KeyError(field_id)

    def create_table(self, name, fields):
        self.calls.append(("create_table", name, fields))
        item = {"name": name, "table_id": "tbl-created"}
        self.tables.append(item)
        self.fields["tbl-created"] = [
            {"field_name": fields[0]["name"], "field_id": "fld-primary", "type": 1}
        ]
        return {"data": {"table": item}}

    def create_field(self, table_id, payload):
        self.calls.append(("create_field", table_id, payload))
        self.fields[table_id].append(
            {"field_name": payload["name"], "field_id": "fld-new", "type": 1}
        )
        return {"ok": True}

    def update_field(self, table_id, field_id, payload):
        self.calls.append(("update_field", table_id, field_id, payload))
        for index, item in enumerate(self.fields.get(table_id, [])):
            if item.get("field_id") != field_id:
                continue
            preserved_ids = {
                option.get("name"): option.get("id")
                for option in item.get("options", [])
                if isinstance(option, dict) and option.get("id")
            }
            updated = dict(item)
            updated.update(payload)
            updated["options"] = [
                {
                    **option,
                    **(
                        {"id": preserved_ids[option["name"]]}
                        if option.get("name") in preserved_ids
                        else {}
                    ),
                }
                for option in payload["options"]
            ]
            self.fields[table_id][index] = updated
            return {"ok": True}
        raise KeyError(field_id)

    def list_views(self, table_id):
        return {"items": [{"id": f"vew-{table_id}", "type": "grid"}]}

    def set_view_visible_fields(self, table_id, view_id, field_names):
        self.calls.append(("set_view_visible_fields", table_id, view_id, list(field_names)))
        self.visible_fields = list(field_names)
        return {"visible_fields": list(field_names)}

    def get_view_visible_fields(self, table_id, view_id):
        return {"visible_fields": list(self.visible_fields)}


def test_dry_run_is_read_only_and_reports_missing_table():
    admin = FakeAdmin()

    result = SevenTableProvisioner(admin, schema=SCHEMA).apply(
        dry_run=True,
        include_workflow=False,
    )

    assert result.plan.mutation_count == 1
    assert result.plan.actions[0].kind == "create_table"
    assert admin.calls == []


def test_apply_creates_shell_then_fields_and_second_plan_is_zero_mutation():
    admin = FakeAdmin()
    provisioner = SevenTableProvisioner(admin, schema=SCHEMA)

    result = provisioner.apply(include_workflow=False)

    assert result.table_ids == {"搜索任务表": "tbl-created"}
    assert [call[0] for call in admin.calls] == [
        "create_table",
        "create_field",
        "set_view_visible_fields",
    ]
    assert provisioner.plan(include_workflow=False).mutation_count == 0


def test_missing_table_view_recovery_is_journaled_before_first_write():
    admin = FakeAdmin()
    journal = []
    original_create = admin.create_table

    def create_table(name, fields):
        assert journal == [["搜索任务表"]]
        return original_create(name, fields)

    admin.create_table = create_table

    result = SevenTableProvisioner(admin, schema=SCHEMA).apply(
        include_workflow=False,
        journal_pending_views=lambda names: journal.append(list(names)),
    )

    assert journal == [["搜索任务表"]]
    assert result.pending_view_tables == ["搜索任务表"]
    assert result.initialized_view_tables == ["搜索任务表"]


def test_new_table_initializes_only_its_one_default_view_in_contract_order():
    admin = FakeAdmin()
    result = SevenTableProvisioner(admin, schema=SCHEMA).apply(
        include_workflow=False,
    )

    assert result.initialized_view_tables == ["搜索任务表"]
    assert admin.calls[-1] == (
        "set_view_visible_fields",
        "tbl-created",
        "vew-tbl-created",
        ["多行文本", "搜索关键词"],
    )


def test_duplicate_table_name_is_fail_closed_before_writes():
    admin = FakeAdmin(
        tables=[
            {"name": "搜索任务表", "table_id": "tbl-1"},
            {"name": "搜索任务表", "table_id": "tbl-2"},
        ]
    )

    with pytest.raises(ProvisioningConflict, match="重复同名"):
        SevenTableProvisioner(admin, schema=SCHEMA).apply(include_workflow=False)

    assert admin.calls == []


def test_wrong_existing_field_type_is_conflict_not_auto_conversion():
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "多行文本", "field_id": "fld-1", "type": 1},
                {"field_name": "搜索关键词", "field_id": "fld-2", "type": 2},
            ]
        },
    )

    with pytest.raises(ProvisioningConflict, match="类型冲突"):
        SevenTableProvisioner(admin, schema=SCHEMA).apply(include_workflow=False)

    assert admin.calls == []


def test_new_base_cleanup_deletes_only_confirmed_empty_unmanaged_tables():
    class Client:
        def __init__(self):
            self.deleted = []

        def list_tables(self):
            return [
                {"table_id": "tbl-managed", "name": "搜索任务表"},
                {"table_id": "tbl-default", "name": "数据表"},
                {"table_id": "tbl-user", "name": "用户数据"},
            ]

        def get_all_records(self, table_id, page_size=500):
            assert page_size == 1
            return [] if table_id == "tbl-default" else [{"record_id": "rec1"}]

        def delete_table(self, table_id):
            self.deleted.append(table_id)

    client = Client()
    deleted = cleanup_new_base_default_tables(
        client,
        managed_table_ids={"tbl-managed"},
    )

    assert deleted == ["tbl-default"]
    assert client.deleted == ["tbl-default"]


def test_existing_formula_drift_is_conflict_before_writes():
    schema = FakeSchema(
        tables=(
            FakeTable(
                "网红详情表",
                (
                    FakeField("多行文本", primary=True),
                    type(
                        "FormulaField",
                        (),
                        {
                            "name": "邮箱索引",
                            "numeric_type": 20,
                            "primary": False,
                            "phase": "derived",
                            "options": (),
                            "cli_json": lambda self: {
                                "name": "邮箱索引",
                                "type": "formula",
                                "expression": "LOWER(TRIM([联系邮箱]))",
                            },
                        },
                    )(),
                ),
            ),
        )
    )
    admin = FakeAdmin(
        tables=[{"name": "网红详情表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "多行文本", "field_id": "fld-1", "type": 1},
                {
                    "field_name": "邮箱索引",
                    "field_id": "fld-2",
                    "type": 20,
                    "expression": "[联系邮箱]",
                },
            ]
        },
    )

    with pytest.raises(ProvisioningConflict, match="公式定义不一致"):
        SevenTableProvisioner(admin, schema=schema).apply(include_workflow=False)

    assert admin.calls == []


def test_missing_expected_primary_field_is_conflict_before_writes():
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "标题", "field_id": "fld-primary", "type": 1},
                {"field_name": "搜索关键词", "field_id": "fld-2", "type": 1},
            ]
        },
    )

    with pytest.raises(ProvisioningConflict, match="主字段必须为"):
        SevenTableProvisioner(admin, schema=SCHEMA).apply(include_workflow=False)

    assert admin.calls == []


def test_existing_ordinary_field_string_type_drift_is_conflict():
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "多行文本", "field_id": "fld-1", "type": "text"},
                {"field_name": "搜索关键词", "field_id": "fld-2", "type": "number"},
            ]
        },
    )

    with pytest.raises(ProvisioningConflict, match="类型期望 text"):
        SevenTableProvisioner(admin, schema=SCHEMA).apply(include_workflow=False)

    assert admin.calls == []


def _select_schema():
    select_field = type(
        "SelectField",
        (),
        {
            "name": "执行状态",
            "numeric_type": 3,
            "primary": False,
            "phase": "ordinary",
            "cli_json": lambda self: {
                "name": "执行状态",
                "type": "select",
                "multiple": False,
                "options": [
                    {"name": "进行中"},
                    {"name": "成功"},
                    {"name": "失败"},
                ],
            },
        },
    )()
    return FakeSchema(
        tables=(
            FakeTable(
                "搜索任务表",
                (FakeField("多行文本", primary=True), select_field),
            ),
        )
    )


def test_missing_required_select_options_are_planned_and_added_safely():
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "多行文本", "field_id": "fld-1", "type": 1},
                {
                    "field_name": "执行状态",
                    "field_id": "fld-select",
                    "type": 3,
                    "multiple": False,
                    "default_value": ["自定义"],
                    "options": [
                        {
                            "id": "opt-ok",
                            "name": "成功",
                            "hue": "Green",
                            "lightness": "Light",
                        },
                        {"id": "opt-extra", "name": "自定义", "color": 7},
                    ],
                },
            ]
        },
    )
    provisioner = SevenTableProvisioner(admin, schema=_select_schema())

    plan = provisioner.plan(include_workflow=False)

    assert plan.safe
    assert [(action.kind, action.target) for action in plan.actions] == [
        ("update_select_options", "搜索任务表.执行状态")
    ]

    provisioner.apply(include_workflow=False)

    update = next(call for call in admin.calls if call[0] == "update_field")
    payload = update[3]
    assert "id" not in payload
    assert payload["default_value"] == ["自定义"]
    assert payload["options"] == [
        {"name": "成功", "hue": "Green", "lightness": "Light"},
        {"name": "自定义", "color": 7},
        {"name": "进行中"},
        {"name": "失败"},
    ]
    current = admin.get_field("tbl-1", "fld-select")
    by_name = {option["name"]: option for option in current["options"]}
    assert by_name["成功"]["id"] == "opt-ok"
    assert by_name["自定义"]["id"] == "opt-extra"
    assert provisioner.plan(include_workflow=False).mutation_count == 0


@pytest.mark.parametrize("default", [None, [], ["成功"]])
def test_select_option_update_preserves_legal_default_in_full_put(default):
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={"tbl-1": [
            {"field_name": "多行文本", "field_id": "fld-1", "type": 1},
            {"field_name": "执行状态", "field_id": "fld-select", "type": "select",
             "multiple": False, "description": "保留描述", "default_value": default,
             "options": [{"name": "成功", "hue": "Green", "lightness": "Light"}]},
        ]},
    )
    SevenTableProvisioner(admin, schema=_select_schema()).apply(include_workflow=False)
    payload = next(call[3] for call in admin.calls if call[0] == "update_field")
    assert "default_value" in payload and payload["default_value"] == default
    assert payload["description"] == "保留描述"
    assert payload["options"][0] == {"name": "成功", "hue": "Green", "lightness": "Light"}


@pytest.mark.parametrize("default", ["成功", ["不存在"], ["成功", "成功"], ["成功", "自定义"], [1]])
def test_invalid_select_default_blocks_metadata_changes(default):
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={"tbl-1": [
            {"field_name": "多行文本", "field_id": "fld-1", "type": 1},
            {"field_name": "执行状态", "field_id": "fld-select", "type": "select",
             "multiple": False, "default_value": default,
             "options": [{"name": "成功"}, {"name": "自定义"}]},
        ]},
    )
    with pytest.raises(ProvisioningConflict, match="默认值"):
        SevenTableProvisioner(admin, schema=_select_schema()).apply(include_workflow=False)
    assert admin.calls == []


def test_select_update_detects_default_cleared_by_server():
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={"tbl-1": [
            {"field_name": "多行文本", "field_id": "fld-1", "type": 1},
            {"field_name": "执行状态", "field_id": "fld-select", "type": "select",
             "multiple": False, "default_value": ["成功"], "options": [{"name": "成功"}]},
        ]},
    )
    original = admin.update_field

    def update(table_id, field_id, payload):
        result = original(table_id, field_id, payload)
        admin.fields[table_id][1].pop("default_value", None)
        return result

    admin.update_field = update
    with pytest.raises(RuntimeError, match="default_value 未保留"):
        SevenTableProvisioner(admin, schema=_select_schema()).apply(include_workflow=False)


@pytest.mark.parametrize("payload", [
    {}, {"unknown": []}, {"items": "invalid"}, {"items": [None]},
    {"ok": False, "error": {"code": "denied"}},
])
def test_unknown_table_inventory_blocks_all_provisioning_writes(payload):
    admin = FakeAdmin()
    admin.list_tables = lambda: payload
    with pytest.raises(ProvisioningConflict):
        SevenTableProvisioner(admin, schema=SCHEMA).apply(include_workflow=False)
    assert admin.calls == []


def test_confirmed_null_workflow_list_allows_initial_provisioning_plan():
    admin = FakeAdmin()
    admin.list_workflows = lambda: {"ok": True, "data": {"items": None, "total": 0}}
    plan = SevenTableProvisioner(admin, schema=SCHEMA).plan()
    assert plan.safe
    assert [(action.kind, action.target) for action in plan.actions] == [
        ("create_table", "搜索任务表"), ("create_workflow", SCHEMA.workflow_title),
    ]


def test_duplicate_existing_select_options_are_conflict_before_writes():
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "多行文本", "field_id": "fld-1", "type": 1},
                {
                    "field_name": "执行状态",
                    "field_id": "fld-select",
                    "type": 3,
                    "multiple": False,
                    "options": [{"name": "成功"}, {"name": "成功"}],
                },
            ]
        },
    )

    with pytest.raises(ProvisioningConflict, match="单选项名称重复"):
        SevenTableProvisioner(admin, schema=_select_schema()).apply(
            include_workflow=False
        )

    assert admin.calls == []


def test_full_field_numeric_type_drift_is_conflict_even_if_inventory_is_untyped():
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "多行文本", "field_id": "fld-1", "type": 1},
                {"field_name": "搜索关键词", "field_id": "fld-2", "type": None},
            ]
        },
    )
    original_get_field = admin.get_field

    def get_field(table_id, field_id):
        if field_id == "fld-2":
            return {"field_name": "搜索关键词", "field_id": "fld-2", "type": 2}
        return original_get_field(table_id, field_id)

    admin.get_field = get_field

    with pytest.raises(ProvisioningConflict, match="类型期望 1，实际 2"):
        SevenTableProvisioner(admin, schema=SCHEMA).apply(include_workflow=False)

    assert admin.calls == []


def test_missing_select_options_are_additive_and_preserve_existing_metadata():
    schema = FakeSchema(
        tables=(
            FakeTable(
                "搜索任务表",
                (
                    FakeField("多行文本", primary=True),
                    FakeSelectField("执行状态", ("成功", "失败", "中断")),
                ),
            ),
        )
    )
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "多行文本", "field_id": "fld-1", "type": "text"},
                {
                    "field_name": "执行状态",
                    "name": "执行状态",
                    "field_id": "fld-2",
                    "type": "select",
                    "multiple": False,
                    "description": "运营状态",
                    "options": [
                        {"id": "opt-success", "name": "成功", "color": 2},
                        {"id": "opt-custom", "name": "自定义", "hue": "blue"},
                    ],
                },
            ]
        },
    )
    provisioner = SevenTableProvisioner(admin, schema=schema)

    plan = provisioner.plan(include_workflow=False)
    assert plan.conflicts == []
    assert [(action.kind, action.target) for action in plan.actions] == [
        ("update_select_options", "搜索任务表.执行状态")
    ]

    result = provisioner.apply(include_workflow=False)

    update = next(call for call in admin.calls if call[0] == "update_field")
    payload = update[3]
    assert payload["description"] == "运营状态"
    assert payload["options"] == [
        {"name": "成功", "color": 2},
        {"name": "自定义", "hue": "blue"},
        {"name": "失败"},
        {"name": "中断"},
    ]
    assert all("id" not in option for option in payload["options"])
    assert result.plan.mutation_count == 0
    assert provisioner.plan(include_workflow=False).mutation_count == 0


def test_duplicate_existing_select_option_names_fail_before_update():
    schema = FakeSchema(
        tables=(
            FakeTable(
                "搜索任务表",
                (
                    FakeField("多行文本", primary=True),
                    FakeSelectField("执行状态", ("成功", "失败")),
                ),
            ),
        )
    )
    admin = FakeAdmin(
        tables=[{"name": "搜索任务表", "table_id": "tbl-1"}],
        fields={
            "tbl-1": [
                {"field_name": "多行文本", "field_id": "fld-1", "type": "text"},
                {
                    "field_name": "执行状态",
                    "field_id": "fld-2",
                    "type": "select",
                    "multiple": False,
                    "options": [{"name": "成功"}, {"name": "成功"}],
                },
            ]
        },
    )

    with pytest.raises(ProvisioningConflict, match="单选项名称重复"):
        SevenTableProvisioner(admin, schema=schema).apply(include_workflow=False)

    assert admin.calls == []


def test_workflow_enable_readback_failure_compensates_only_after_enable_write():
    admin = type(
        "WorkflowAdmin",
        (),
        {
            "calls": [],
            "enable_workflow": lambda self, workflow_id: self.calls.append(
                ("enable", workflow_id)
            ),
            "get_workflow": lambda self, workflow_id: {
                "workflow_id": workflow_id,
                "status": "disabled",
            },
            "disable_workflow": lambda self, workflow_id: self.calls.append(
                ("disable", workflow_id)
            ),
        },
    )()
    provisioner = SevenTableProvisioner(admin, schema=SCHEMA)

    with pytest.raises(RuntimeError, match="状态回读不是 enabled"):
        provisioner._enable_workflow_with_readback("wkf-test")

    assert admin.calls == [("enable", "wkf-test"), ("disable", "wkf-test")]


def test_read_only_plan_reports_deferred_enable_for_disabled_workflow(monkeypatch):
    admin = FakeAdmin(
        tables=[],
        workflows=[
            {
                "workflow_id": "wkf-test",
                "title": SCHEMA.workflow_title,
                "status": "disabled",
            }
        ],
    )
    # This focused test isolates Workflow planning from table dependencies.
    schema = FakeSchema(tables=())
    provisioner = SevenTableProvisioner(admin, schema=schema)
    monkeypatch.setattr(provisioner, "_workflow_runtime_args", lambda: {})
    admin.get_workflow = lambda _workflow_id: {"status": "disabled"}
    monkeypatch.setattr(
        "feishu.cooperation_workflow.validate_first_cooperation_workflow",
        lambda _definition, **_kwargs: None,
    )

    plan = provisioner.plan(include_workflow=True, enable_workflow=True)

    assert plan.conflicts == []
    assert [(action.kind, action.target) for action in plan.actions] == [
        ("enable_workflow", SCHEMA.workflow_title)
    ]
