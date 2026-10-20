import copy
import json

import pytest

from feishu.cooperation_workflow import (
    BRANCH_STEP_ID,
    CREATE_STEP_ID,
    END_EXISTING_STEP_ID,
    FIND_EXISTING_STEP_ID,
    TRIGGER_STEP_ID,
    CooperationWorkflowValidationError,
    SelectOptionRef,
    build_first_cooperation_workflow,
    first_cooperation_workflow_errors,
    validate_first_cooperation_workflow,
)


EMAIL_FIELD_ID = "fld_test_contact_email"
CHANNEL_FIELD_ID = "fld_test_channel_id"
ENTER_OPTION = {"id": "opt_test_enter", "name": "进入合作"}
INITIAL_STATUS_OPTION = SelectOptionRef("opt_test_pending", "待确认方案")


def _build():
    return build_first_cooperation_workflow(
        client_token="test-client-token",
        influencer_contact_email_field_id=EMAIL_FIELD_ID,
        influencer_channel_id_field_id=CHANNEL_FIELD_ID,
        enter_cooperation_option=ENTER_OPTION,
        initial_cooperation_status_option=INITIAL_STATUS_OPTION,
    )


def _validate(definition):
    return validate_first_cooperation_workflow(
        definition,
        influencer_contact_email_field_id=EMAIL_FIELD_ID,
        influencer_channel_id_field_id=CHANNEL_FIELD_ID,
        enter_cooperation_option=ENTER_OPTION,
        initial_cooperation_status_option=INITIAL_STATUS_OPTION,
    )


def test_builder_injects_runtime_ids_without_production_ids():
    workflow = _build()
    serialized = json.dumps(workflow, ensure_ascii=False)

    assert workflow["client_token"] == "test-client-token"
    assert workflow["title"] == "首次进入合作自动建档"
    assert len(workflow["steps"]) == 5
    assert f"$.{TRIGGER_STEP_ID}.{EMAIL_FIELD_ID}" in serialized
    assert f"$.{TRIGGER_STEP_ID}.{CHANNEL_FIELD_ID}" in serialized
    assert "opt_test_enter" in serialized
    assert "opt_test_pending" in serialized
    assert "fldD9I3b3S" not in serialized
    assert "fldzztO2RC" not in serialized
    assert "optUZiptQJ" not in serialized
    assert "optLma1xOg" not in serialized
    assert "status" not in workflow


def test_builder_has_exact_five_step_topology_and_terminal_false_find():
    workflow = _build()
    steps = {step["id"]: step for step in workflow["steps"]}

    assert steps[TRIGGER_STEP_ID]["next"] == FIND_EXISTING_STEP_ID
    assert steps[FIND_EXISTING_STEP_ID]["next"] == BRANCH_STEP_ID
    assert steps[BRANCH_STEP_ID]["children"]["links"] == [
        {"kind": "if_true", "to": CREATE_STEP_ID},
        {"kind": "if_false", "to": END_EXISTING_STEP_ID},
    ]
    assert steps[CREATE_STEP_ID]["type"] == "AddRecordAction"
    assert steps[CREATE_STEP_ID]["next"] is None

    false_terminal = steps[END_EXISTING_STEP_ID]
    assert false_terminal["type"] == "FindRecordAction"
    assert false_terminal["next"] is None
    assert false_terminal["data"]["should_proceed_when_no_results"] is False
    assert false_terminal["data"]["filter_info"] == steps[FIND_EXISTING_STEP_ID][
        "data"
    ]["filter_info"]


def test_builder_only_creates_email_and_initial_status_fields():
    steps = {step["id"]: step for step in _build()["steps"]}
    create_fields = steps[CREATE_STEP_ID]["data"]["field_values"]

    assert [item["field_name"] for item in create_fields] == ["联系邮箱", "合作状态"]
    assert create_fields[0]["value"] == [
        {
            "value": f"$.{TRIGGER_STEP_ID}.{EMAIL_FIELD_ID}",
            "value_type": "ref",
        }
    ]
    assert create_fields[1]["value"] == [
        {
            "value": {"id": "opt_test_pending", "name": "待确认方案"},
            "value_type": "option",
        }
    ]


def test_semantic_validator_accepts_builder_and_remote_normalization():
    workflow = _build()
    _validate(workflow)

    remote = copy.deepcopy(workflow)
    remote.pop("client_token")
    remote["status"] = "enabled"
    remote["workflow_id"] = "wfl_server_generated"
    remote["steps"] = list(reversed(remote["steps"]))
    for step in remote["steps"]:
        if step["id"] != BRANCH_STEP_ID:
            step["children"] = {"links": []}
        if step.get("next") is None:
            step.pop("next", None)

    envelope = {"code": 0, "data": {"workflow": remote}}
    assert first_cooperation_workflow_errors(
        envelope,
        influencer_contact_email_field_id=EMAIL_FIELD_ID,
        influencer_channel_id_field_id=CHANNEL_FIELD_ID,
        enter_cooperation_option=ENTER_OPTION,
        initial_cooperation_status_option=INITIAL_STATUS_OPTION,
    ) == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda steps: steps[FIND_EXISTING_STEP_ID]["data"]["filter_info"][
            "conditions"
        ][0]["value"][0].update(value="$.trigger_enter_cooperation.fld_wrong"),
        lambda steps: steps[BRANCH_STEP_ID]["children"]["links"].pop(),
        lambda steps: steps[CREATE_STEP_ID]["data"]["field_values"].append(
            {"field_name": "KOL Name", "value": [{"value": "unexpected"}]}
        ),
        lambda steps: steps[END_EXISTING_STEP_ID]["data"].update(
            should_proceed_when_no_results=True
        ),
    ],
)
def test_semantic_validator_rejects_reference_branch_write_and_terminal_drift(
    mutation,
):
    workflow = _build()
    steps = {step["id"]: step for step in workflow["steps"]}
    mutation(steps)

    with pytest.raises(CooperationWorkflowValidationError):
        _validate(workflow)


def test_semantic_validator_rejects_missing_or_extra_steps():
    missing = _build()
    missing["steps"] = [
        step for step in missing["steps"] if step["id"] != END_EXISTING_STEP_ID
    ]
    errors = first_cooperation_workflow_errors(
        missing,
        influencer_contact_email_field_id=EMAIL_FIELD_ID,
        influencer_channel_id_field_id=CHANNEL_FIELD_ID,
        enter_cooperation_option=ENTER_OPTION,
        initial_cooperation_status_option=INITIAL_STATUS_OPTION,
    )
    assert any("缺少步骤" in error for error in errors)

    extra = _build()
    extra["steps"].append(
        {"id": "unexpected_step", "type": "FindRecordAction", "data": {}}
    )
    errors = first_cooperation_workflow_errors(
        extra,
        influencer_contact_email_field_id=EMAIL_FIELD_ID,
        influencer_channel_id_field_id=CHANNEL_FIELD_ID,
        enter_cooperation_option=ENTER_OPTION,
        initial_cooperation_status_option=INITIAL_STATUS_OPTION,
    )
    assert any("合同外步骤" in error for error in errors)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"client_token": ""}, "client_token"),
        ({"influencer_contact_email_field_id": "fld.bad"}, "Workflow 引用"),
        (
            {"enter_cooperation_option": {"id": "opt_test", "name": "已合作"}},
            "进入合作",
        ),
        (
            {
                "initial_cooperation_status_option": {
                    "name": "",
                }
            },
            "initial_cooperation_status_option.name",
        ),
    ],
)
def test_builder_rejects_unresolved_or_semantically_wrong_runtime_metadata(
    overrides,
    message,
):
    arguments = {
        "client_token": "test-client-token",
        "influencer_contact_email_field_id": EMAIL_FIELD_ID,
        "influencer_channel_id_field_id": CHANNEL_FIELD_ID,
        "enter_cooperation_option": ENTER_OPTION,
        "initial_cooperation_status_option": INITIAL_STATUS_OPTION,
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=message):
        build_first_cooperation_workflow(**arguments)


def test_builder_accepts_portable_name_only_options_and_validator_ignores_server_ids():
    workflow = build_first_cooperation_workflow(
        client_token="portable",
        influencer_contact_email_field_id=EMAIL_FIELD_ID,
        influencer_channel_id_field_id=CHANNEL_FIELD_ID,
        enter_cooperation_option={"name": "进入合作"},
        initial_cooperation_status_option={"name": "待确认方案"},
    )
    serialized = json.dumps(workflow, ensure_ascii=False)
    assert "opt_" not in serialized

    remote = copy.deepcopy(workflow)
    remote["steps"][0]["data"]["field_watch_info"][0]["value"][0]["value"]["id"] = "opt_server_enter"
    remote["steps"][3]["data"]["field_values"][1]["value"][0]["value"]["id"] = "opt_server_pending"
    validate_first_cooperation_workflow(
        remote,
        influencer_contact_email_field_id=EMAIL_FIELD_ID,
        influencer_channel_id_field_id=CHANNEL_FIELD_ID,
        enter_cooperation_option={"name": "进入合作"},
        initial_cooperation_status_option={"name": "待确认方案"},
    )


def _trigger_field_refs(workflow):
    """The five ValueInfo objects annotated in a real workflow-get response."""
    steps = {step["id"]: step for step in workflow["steps"]}
    branch_conditions = steps[BRANCH_STEP_ID]["data"]["condition"]["conditions"][0]["conditions"]
    return [
        steps[FIND_EXISTING_STEP_ID]["data"]["filter_info"]["conditions"][0]["value"][0],
        branch_conditions[1]["left_value"],
        branch_conditions[2]["left_value"],
        steps[CREATE_STEP_ID]["data"]["field_values"][0]["value"][0],
        steps[END_EXISTING_STEP_ID]["data"]["filter_info"]["conditions"][0]["value"][0],
    ]


def test_remote_trigger_field_ref_table_annotations_are_verified_and_normalized():
    remote = _build()
    for reference in _trigger_field_refs(remote):
        reference["extra_info"] = {"table_name": "网红详情表"}
    before = copy.deepcopy(remote)
    _validate({"ok": True, "data": remote})
    assert remote == before


@pytest.mark.parametrize(
    "extra_info",
    [
        {"table_name": "合作红人表"},
        {"table_name": "网红详情表", "unknown_semantics": True},
        {"table_name": "网红详情表", "field_name": "different field"},
        {"table_name": None},
        {},
        None,
        "网红详情表",
    ],
)
def test_ref_table_normalization_rejects_unknown_or_conflicting_annotations(extra_info):
    remote = _build()
    _trigger_field_refs(remote)[0]["extra_info"] = extra_info
    with pytest.raises(CooperationWorkflowValidationError, match="extra_info"):
        _validate(remote)


def test_correct_table_annotation_cannot_hide_a_changed_field_reference():
    remote = _build()
    reference = _trigger_field_refs(remote)[0]
    reference.update(value="$.trigger_enter_cooperation.fld_wrong", extra_info={"table_name": "网红详情表"})
    with pytest.raises(CooperationWorkflowValidationError):
        _validate(remote)


def test_source_table_annotations_on_other_refs_and_literals_remain_contract_errors():
    for literal in (False, True):
        remote = _build()
        branch = next(step for step in remote["steps"] if step["id"] == BRANCH_STEP_ID)
        value = branch["data"]["condition"]["conditions"][0]["conditions"][0]["left_value"]
        if literal:
            value.update(value=0, value_type="number")
        value["extra_info"] = {"table_name": "网红详情表"}
        with pytest.raises(CooperationWorkflowValidationError, match="extra_info"):
            _validate(remote)
