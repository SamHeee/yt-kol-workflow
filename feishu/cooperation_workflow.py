#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure builder and semantic validator for first-cooperation automation.

This module deliberately contains no Feishu client or CLI calls.  Runtime
field IDs and select-option IDs must be resolved from the target Base and
passed in by the caller; keeping those IDs out of source makes the workflow
portable across newly provisioned Bases.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


WORKFLOW_TITLE = "首次进入合作自动建档"
INFLUENCER_TABLE = "网红详情表"
COOPERATION_TABLE = "合作红人表"

CONTACT_EMAIL_FIELD = "联系邮箱"
CHANNEL_ID_FIELD = "Channel ID"
DEVELOPMENT_STATUS_FIELD = "开发状态"
COOPERATION_STATUS_FIELD = "合作状态"

ENTER_COOPERATION_OPTION_NAME = "进入合作"
INITIAL_COOPERATION_STATUS_NAME = "待确认方案"

TRIGGER_STEP_ID = "trigger_enter_cooperation"
FIND_EXISTING_STEP_ID = "find_existing_cooperation"
BRANCH_STEP_ID = "branch_no_existing"
CREATE_STEP_ID = "create_first_cooperation"
END_EXISTING_STEP_ID = "end_existing_record"

STEP_IDS: Tuple[str, ...] = (
    TRIGGER_STEP_ID,
    FIND_EXISTING_STEP_ID,
    BRANCH_STEP_ID,
    CREATE_STEP_ID,
    END_EXISTING_STEP_ID,
)

TRIGGER_CONTROLS: Tuple[str, ...] = (
    "pasteUpdate",
    "automationBatchUpdate",
    "appendImport",
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class SelectOptionRef:
    """A resolved Feishu single-select option used in a workflow definition."""

    option_id: str = ""
    name: str = ""


OptionInput = Union[SelectOptionRef, Mapping[str, Any]]


class CooperationWorkflowValidationError(ValueError):
    """Raised when a workflow does not implement the five-step contract."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("；".join(self.errors))


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value.strip()


def _required_id(value: Any, label: str) -> str:
    identifier = _required_text(value, label)
    if not _SAFE_ID_RE.fullmatch(identifier):
        raise ValueError(f"{label} 包含不能用于 Workflow 引用的字符: {identifier!r}")
    return identifier


def _option_value(
    value: OptionInput,
    *,
    label: str,
    expected_name: str,
) -> Dict[str, str]:
    if isinstance(value, SelectOptionRef):
        option_id = value.option_id
        name = value.name
    elif isinstance(value, Mapping):
        option_id = value.get("id") or value.get("option_id")
        name = value.get("name")
    else:
        raise ValueError(f"{label} 必须是 SelectOptionRef 或包含 name 的对象")

    normalized_name = _required_text(name, f"{label}.name")
    if normalized_name != expected_name:
        raise ValueError(
            f"{label}.name 应为 {expected_name!r}，实际为 {normalized_name!r}"
        )
    result = {"name": normalized_name}
    # The typed Base field metadata exposed by lark-cli does not include
    # option IDs.  Workflow payloads explicitly support name-only option
    # references; when an ID is available (for example in a remote readback),
    # keep it, but never make it a portability requirement.
    if option_id:
        result["id"] = _required_id(option_id, f"{label}.id")
    return result


def _field_ref(step_id: str, field_id: str) -> str:
    return f"$.{step_id}.{field_id}"


def _email_filter(email_ref: str) -> Dict[str, Any]:
    return {
        "conditions": [
            {
                "field_name": CONTACT_EMAIL_FIELD,
                "operator": "is",
                "value": [{"value": email_ref, "value_type": "ref"}],
            }
        ],
        "conjunction": "and",
    }


def build_first_cooperation_workflow(
    *,
    client_token: str,
    influencer_contact_email_field_id: str,
    influencer_channel_id_field_id: str,
    enter_cooperation_option: OptionInput,
    initial_cooperation_status_option: OptionInput,
) -> Dict[str, Any]:
    """Build the portable five-step first-cooperation workflow definition.

    The returned object is suitable as the semantic payload for Workflow
    create/update.  It intentionally omits ``status``: provisioning should
    create or update the definition, validate the remote result, and only then
    enable it as a separate final step.
    """

    normalized_client_token = _required_text(client_token, "client_token")
    email_field_id = _required_id(
        influencer_contact_email_field_id,
        "influencer_contact_email_field_id",
    )
    channel_field_id = _required_id(
        influencer_channel_id_field_id,
        "influencer_channel_id_field_id",
    )
    enter_option = _option_value(
        enter_cooperation_option,
        label="enter_cooperation_option",
        expected_name=ENTER_COOPERATION_OPTION_NAME,
    )
    initial_status_option = _option_value(
        initial_cooperation_status_option,
        label="initial_cooperation_status_option",
        expected_name=INITIAL_COOPERATION_STATUS_NAME,
    )

    email_ref = _field_ref(TRIGGER_STEP_ID, email_field_id)
    channel_ref = _field_ref(TRIGGER_STEP_ID, channel_field_id)

    return {
        "client_token": normalized_client_token,
        "title": WORKFLOW_TITLE,
        "steps": [
            {
                "id": TRIGGER_STEP_ID,
                "type": "SetRecordTrigger",
                "title": "邮箱和 Channel ID 非空且开发状态进入合作",
                "next": FIND_EXISTING_STEP_ID,
                "data": {
                    "table_name": INFLUENCER_TABLE,
                    "field_watch_info": [
                        {
                            "field_name": DEVELOPMENT_STATUS_FIELD,
                            "operator": "is",
                            "value": [
                                {
                                    "value": enter_option,
                                    "value_type": "option",
                                }
                            ],
                        }
                    ],
                    "condition_list": [
                        {
                            "conditions": [
                                {
                                    "field_name": CONTACT_EMAIL_FIELD,
                                    "operator": "isNotEmpty",
                                    "value": [],
                                },
                                {
                                    "field_name": CHANNEL_ID_FIELD,
                                    "operator": "isNotEmpty",
                                    "value": [],
                                },
                            ],
                            "conjunction": "and",
                        }
                    ],
                    "trigger_control_list": list(TRIGGER_CONTROLS),
                },
            },
            {
                "id": FIND_EXISTING_STEP_ID,
                "type": "FindRecordAction",
                "title": "按联系邮箱查找已有合作",
                "next": BRANCH_STEP_ID,
                "data": {
                    "table_name": COOPERATION_TABLE,
                    "field_names": [CONTACT_EMAIL_FIELD],
                    "filter_info": _email_filter(email_ref),
                    "ref_info": None,
                    "should_proceed_when_no_results": True,
                },
            },
            {
                "id": BRANCH_STEP_ID,
                "type": "IfElseBranch",
                "title": "仅邮箱未建档时创建",
                "next": None,
                "children": {
                    "links": [
                        {"kind": "if_true", "to": CREATE_STEP_ID},
                        {"kind": "if_false", "to": END_EXISTING_STEP_ID},
                    ]
                },
                "data": {
                    "condition": {
                        "conditions": [
                            {
                                "conditions": [
                                    {
                                        "left_value": {
                                            "value": (
                                                f"$.{FIND_EXISTING_STEP_ID}.recordNum"
                                            ),
                                            "value_type": "ref",
                                        },
                                        "operator": "is",
                                        "right_value": [
                                            {"value": 0, "value_type": "number"}
                                        ],
                                    },
                                    {
                                        "left_value": {
                                            "value": channel_ref,
                                            "value_type": "ref",
                                        },
                                        "operator": "isNotEmpty",
                                    },
                                    {
                                        "left_value": {
                                            "value": email_ref,
                                            "value_type": "ref",
                                        },
                                        "operator": "isNotEmpty",
                                    },
                                ],
                                "conjunction": "and",
                            }
                        ],
                        "conjunction": "or",
                    }
                },
            },
            {
                "id": CREATE_STEP_ID,
                "type": "AddRecordAction",
                "title": "仅写入合作邮箱和初始状态",
                "next": None,
                "data": {
                    "table_name": COOPERATION_TABLE,
                    "field_values": [
                        {
                            "field_name": CONTACT_EMAIL_FIELD,
                            "value": [{"value": email_ref, "value_type": "ref"}],
                        },
                        {
                            "field_name": COOPERATION_STATUS_FIELD,
                            "value": [
                                {
                                    "value": initial_status_option,
                                    "value_type": "option",
                                }
                            ],
                        },
                    ],
                },
            },
            {
                "id": END_EXISTING_STEP_ID,
                "type": "FindRecordAction",
                "title": "联系邮箱已有合作记录时结束",
                "next": None,
                "data": {
                    "table_name": COOPERATION_TABLE,
                    "field_names": [CONTACT_EMAIL_FIELD],
                    "filter_info": _email_filter(email_ref),
                    "ref_info": None,
                    "should_proceed_when_no_results": False,
                },
            },
        ],
    }


def _unwrap_workflow(definition: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(definition, Mapping):
        return None
    if "steps" in definition:
        return definition

    workflow = definition.get("workflow")
    if isinstance(workflow, Mapping) and "steps" in workflow:
        return workflow

    data = definition.get("data")
    if isinstance(data, Mapping):
        if "steps" in data:
            return data
        workflow = data.get("workflow")
        if isinstance(workflow, Mapping) and "steps" in workflow:
            return workflow
    return None


def _canonical_links(children: Any) -> Any:
    if not isinstance(children, Mapping):
        return copy.deepcopy(children)
    links = children.get("links")
    if not isinstance(links, list):
        return copy.deepcopy(children)
    canonical = []
    for link in links:
        if isinstance(link, Mapping):
            canonical.append({"kind": link.get("kind"), "to": link.get("to")})
        else:
            canonical.append(copy.deepcopy(link))
    canonical.sort(
        key=lambda item: (
            str(item.get("kind")) if isinstance(item, Mapping) else str(item),
            str(item.get("to")) if isinstance(item, Mapping) else "",
        )
    )
    return {"links": canonical}


def _canonical_step(step: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "id": step.get("id"),
        "type": step.get("type"),
        "title": step.get("title"),
        "next": step.get("next"),
        "data": copy.deepcopy(step.get("data")),
    }

    children = step.get("children")
    links = children.get("links") if isinstance(children, Mapping) else None
    if step.get("id") == BRANCH_STEP_ID or links not in (None, []):
        result["children"] = _canonical_links(children)
    return result


def _strip_server_option_ids(value: Any) -> Any:
    """Normalize server-added IDs from otherwise name-equivalent options."""

    if isinstance(value, list):
        return [_strip_server_option_ids(item) for item in value]
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    result = {
        key: _strip_server_option_ids(item)
        for key, item in value.items()
    }
    if (
        isinstance(result.get("name"), str)
        and set(result).issubset({"id", "option_id", "name"})
    ):
        result.pop("id", None)
        result.pop("option_id", None)
    return result


def _strip_verified_ref_table_info(
    value: Any, reference_tables: Mapping[str, str],
) -> Any:
    """Remove only a redundant, verified source-table annotation on field refs.

    Workflow-get adds ``extra_info: {table_name: ...}`` to the two trigger
    field references. Their source is already determined by the exact
    step/field path. Unknown annotations, conflicting tables and annotations
    on other value types remain visible to the strict semantic diff.
    """
    if isinstance(value, list):
        return [_strip_verified_ref_table_info(item, reference_tables) for item in value]
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    result = {
        key: _strip_verified_ref_table_info(item, reference_tables)
        for key, item in value.items()
    }
    reference = result.get("value")
    if result.get("value_type") == "ref" and isinstance(reference, str):
        source_table = reference_tables.get(reference)
        if source_table is not None and result.get("extra_info") == {"table_name": source_table}:
            result.pop("extra_info")
    return result


def _canonical_workflow(
    body: Mapping[str, Any],
    reference_tables: Mapping[str, str],
) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    raw_steps = body.get("steps")
    if not isinstance(raw_steps, list):
        return {"title": body.get("title"), "steps": []}, ["steps 必须是数组"]

    by_id: Dict[str, Mapping[str, Any]] = {}
    duplicate_ids: List[str] = []
    anonymous_steps = 0
    for step in raw_steps:
        if not isinstance(step, Mapping):
            anonymous_steps += 1
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            anonymous_steps += 1
            continue
        if step_id in by_id:
            duplicate_ids.append(step_id)
        by_id[step_id] = step

    if anonymous_steps:
        errors.append(f"存在 {anonymous_steps} 个没有有效 id 的步骤")
    if duplicate_ids:
        errors.append(f"存在重复步骤 id: {sorted(set(duplicate_ids))}")

    missing = [step_id for step_id in STEP_IDS if step_id not in by_id]
    if missing:
        errors.append(f"缺少步骤: {missing}")
    extra = [step_id for step_id in by_id if step_id not in STEP_IDS]
    if extra:
        errors.append(f"存在合同外步骤: {sorted(extra)}")

    ordered_steps = [
        _strip_verified_ref_table_info(
            _strip_server_option_ids(_canonical_step(by_id[step_id])),
            reference_tables,
        )
        for step_id in STEP_IDS
        if step_id in by_id
    ]
    return {"title": body.get("title"), "steps": ordered_steps}, errors


def _semantic_diff(expected: Any, actual: Any, path: str = "$") -> List[str]:
    errors: List[str] = []
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [f"{path} 应为对象，实际为 {type(actual).__name__}"]
        for key in expected:
            if key not in actual:
                errors.append(f"{path}.{key} 缺失")
            else:
                errors.extend(
                    _semantic_diff(expected[key], actual[key], f"{path}.{key}")
                )
        for key in actual:
            if key not in expected:
                errors.append(f"{path}.{key} 为合同外配置")
        return errors

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path} 应为数组，实际为 {type(actual).__name__}"]
        if len(expected) != len(actual):
            errors.append(
                f"{path} 长度应为 {len(expected)}，实际为 {len(actual)}"
            )
        for index, (expected_item, actual_item) in enumerate(
            zip(expected, actual)
        ):
            errors.extend(
                _semantic_diff(expected_item, actual_item, f"{path}[{index}]")
            )
        return errors

    if type(expected) is not type(actual) or expected != actual:
        errors.append(f"{path} 应为 {expected!r}，实际为 {actual!r}")
    return errors


def first_cooperation_workflow_errors(
    definition: Any,
    *,
    influencer_contact_email_field_id: str,
    influencer_channel_id_field_id: str,
    enter_cooperation_option: OptionInput,
    initial_cooperation_status_option: OptionInput,
) -> List[str]:
    """Return semantic drift from the portable five-step workflow contract.

    Workflow-get envelopes, server-added ``status`` fields, missing/null
    terminal ``next`` values, empty ``children.links`` on action steps, and
    verified source-table annotations on the two trigger field refs are
    normalized away. Step order and branch-link order are also non-semantic;
    topology, conditions, references, action fields and option IDs are strict.
    """

    body = _unwrap_workflow(definition)
    if body is None:
        return ["找不到包含 steps 的 Workflow 定义"]

    expected = build_first_cooperation_workflow(
        client_token="semantic-validator",
        influencer_contact_email_field_id=influencer_contact_email_field_id,
        influencer_channel_id_field_id=influencer_channel_id_field_id,
        enter_cooperation_option=enter_cooperation_option,
        initial_cooperation_status_option=initial_cooperation_status_option,
    )
    reference_tables = {
        _field_ref(TRIGGER_STEP_ID, influencer_contact_email_field_id): INFLUENCER_TABLE,
        _field_ref(TRIGGER_STEP_ID, influencer_channel_id_field_id): INFLUENCER_TABLE,
    }
    expected_canonical, expected_errors = _canonical_workflow(expected, reference_tables)
    if expected_errors:  # Defensive: the module's own template must be valid.
        raise AssertionError("内置 Workflow 模板无效: " + "；".join(expected_errors))

    actual_canonical, structural_errors = _canonical_workflow(body, reference_tables)
    return structural_errors + _semantic_diff(
        expected_canonical,
        actual_canonical,
    )


def validate_first_cooperation_workflow(
    definition: Any,
    *,
    influencer_contact_email_field_id: str,
    influencer_channel_id_field_id: str,
    enter_cooperation_option: OptionInput,
    initial_cooperation_status_option: OptionInput,
) -> None:
    """Raise if ``definition`` does not satisfy the five-step contract."""

    errors = first_cooperation_workflow_errors(
        definition,
        influencer_contact_email_field_id=influencer_contact_email_field_id,
        influencer_channel_id_field_id=influencer_channel_id_field_id,
        enter_cooperation_option=enter_cooperation_option,
        initial_cooperation_status_option=initial_cooperation_status_option,
    )
    if errors:
        raise CooperationWorkflowValidationError(errors)


# Concise aliases for callers that already operate in a first-use context.
build_cooperation_workflow = build_first_cooperation_workflow
cooperation_workflow_errors = first_cooperation_workflow_errors
validate_cooperation_workflow = validate_first_cooperation_workflow
