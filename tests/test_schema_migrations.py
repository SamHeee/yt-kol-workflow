from copy import deepcopy
import pytest

from feishu.full_schema import FULL_SCHEMA, LEGACY_OUTREACH_LETTER_FORMULA
from feishu.schema_migrations import TARGETS, plan_upgrade, apply_upgrade
from feishu.provisioning import ProvisioningConflict


class Admin:
    def __init__(self):
        self.tables = [{"table_id": name, "name": name} for name in TARGETS]
        self.fields = {}
        self.writes = []
        for table, names in TARGETS.items():
            self.fields[table] = {}
            for name in names:
                field = FULL_SCHEMA.table(table).field(name).cli_json()
                if name in {"apify邮箱", "最近视频平均播放量", "最近视频中位播放量", "邮件主题"}:
                    continue
                if field["type"] == "number":
                    field["style"] = {"type": "plain", "precision": 2, "percentage": False, "thousands_separator": False}
                    field["default_value"] = 7
                    field["description"] = "Keep this description"
                if name == "开发信":
                    field["expression"] = LEGACY_OUTREACH_LETTER_FORMULA
                self.fields[table][name] = {**field, "field_id": name}

    def list_tables(self):
        return deepcopy(self.tables)

    def list_fields(self, tid):
        return deepcopy(list(self.fields[tid].values()))

    def get_field(self, tid, fid):
        return deepcopy(self.fields[tid][fid])

    def create_field(self, tid, payload):
        self.writes.append(("create", tid, deepcopy(payload)))
        self.fields[tid][payload["name"]] = {**deepcopy(payload), "field_id": payload["name"]}

    def update_field(self, tid, fid, payload):
        self.writes.append(("update", tid, deepcopy(payload)))
        self.fields[tid][fid] = {**deepcopy(payload), "field_id": fid}


def test_upgrade_preview_apply_readback_and_second_run_noop():
    admin = Admin()
    original_ids = {tid: set(fields) for tid, fields in admin.fields.items()}
    plan = plan_upgrade(admin)
    assert len(plan["actions"]) == 12 and not plan["conflicts"]
    assert not admin.writes
    apply_upgrade(admin, plan)
    assert plan_upgrade(admin)["actions"] == []
    for tid, ids in original_ids.items():
        assert ids <= set(admin.fields[tid])
    number = admin.fields["视频数据表"]["Views"]
    assert number["style"]["thousands_separator"] is True
    assert number["default_value"] == 7 and number["description"] == "Keep this description"
    assert admin.fields["网红详情表"]["最近视频平均播放量"]["style"]["precision"] == 2
    count = len(admin.writes)
    apply_upgrade(admin, plan_upgrade(admin))
    assert len(admin.writes) == count


@pytest.mark.parametrize("change", ["formula", "type", "missing-table", "duplicate-table"])
def test_custom_configuration_conflicts_prevent_any_write(change):
    admin = Admin()
    if change == "formula":
        admin.fields["网红详情表"]["开发信"]["expression"] = '"Custom outreach"'
    elif change == "type":
        admin.fields["视频数据表"]["Views"]["type"] = "text"
    elif change == "missing-table":
        admin.tables.pop()
    else:
        admin.tables.append(admin.tables[0])
    with pytest.raises(ProvisioningConflict):
        apply_upgrade(admin, plan_upgrade(admin))
    assert not admin.writes


def test_concurrent_edit_between_preview_and_apply_is_preserved():
    admin = Admin()
    plan = plan_upgrade(admin)
    admin.fields["视频数据表"]["Views"]["description"] = "Changed after preview"
    with pytest.raises(ProvisioningConflict, match="预览"):
        apply_upgrade(admin, plan)
    assert not admin.writes


def test_failed_readback_is_reported_and_rerun_resumes_remaining_fields():
    admin = Admin()
    plan = plan_upgrade(admin)
    original = admin.create_field
    def fail(tid, payload):
        if payload["name"] == "邮件主题":
            raise RuntimeError("injected failure")
        return original(tid, payload)
    admin.create_field = fail
    with pytest.raises(RuntimeError, match="injected"):
        apply_upgrade(admin, plan)
    admin.create_field = original
    retry = plan_upgrade(admin)
    assert 0 < len(retry["actions"]) < len(plan["actions"])
    apply_upgrade(admin, retry)
    assert plan_upgrade(admin)["actions"] == []
