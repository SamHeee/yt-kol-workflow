#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Idempotent provisioning for the complete seven-table Feishu workspace.

Python owns planning and validation.  The authenticated lark-cli profile owns
all metadata writes so formula, lookup, auto-number and native Workflow
definitions use the CLI's documented, typed Base shortcuts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


class ProvisioningError(RuntimeError):
    """Base class for deterministic setup failures."""


class ProvisioningConflict(ProvisioningError):
    """Raised before writes when an existing managed object is unsafe to alter."""


@dataclass(frozen=True)
class PlanAction:
    kind: str
    target: str
    detail: str = ""


@dataclass
class ProvisioningPlan:
    actions: List[PlanAction] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    @property
    def mutation_count(self) -> int:
        return len(self.actions)

    @property
    def safe(self) -> bool:
        return not self.conflicts


@dataclass
class ProvisioningResult:
    plan: ProvisioningPlan
    table_ids: Dict[str, str] = field(default_factory=dict)
    workflow_id: str = ""
    workflow_enabled: bool = False
    initialized_view_tables: List[str] = field(default_factory=list)
    preflight_client: Any = None
    schema_manager: Any = None
    pending_view_tables: List[str] = field(default_factory=list)
    dry_run: bool = False


def _payload_items(payload: Any, *keys: str) -> List[dict]:
    """Extract a list from common lark-cli response envelopes."""

    value = payload
    if isinstance(value, Mapping) and value.get("ok") is True:
        value = value.get("data")
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        raise ProvisioningError("lark-cli returned an unexpected non-object response")
    for key in (*keys, "items"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return [dict(item) for item in candidate if isinstance(item, Mapping)]
    for container in ("data", "result"):
        nested = value.get(container)
        if isinstance(nested, Mapping):
            found = _payload_items(nested, *keys)
            if found or any(key in nested for key in (*keys, "items")):
                return found
    return []


def _table_name(item: Mapping[str, Any]) -> str:
    return str(item.get("table_name") or item.get("name") or "")


def _table_id(item: Mapping[str, Any]) -> str:
    return str(item.get("table_id") or item.get("id") or "")


def _field_name(item: Mapping[str, Any]) -> str:
    return str(item.get("field_name") or item.get("name") or "")


def _workflow_title(item: Mapping[str, Any]) -> str:
    return str(item.get("title") or item.get("name") or "")


def _workflow_id(item: Mapping[str, Any]) -> str:
    return str(item.get("workflow_id") or item.get("id") or "")


def _view_id(item: Mapping[str, Any]) -> str:
    return str(item.get("view_id") or item.get("id") or "")


def _visible_field_names(payload: Any) -> List[str]:
    value = payload
    if isinstance(value, Mapping):
        for key in ("visible_fields", "fields"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        return []
    names: List[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, Mapping):
            name = str(item.get("field_name") or item.get("name") or "")
            if name:
                names.append(name)
    return names


def _group_unique(items: Iterable[Mapping[str, Any]], name_getter) -> Dict[str, dict]:
    grouped: Dict[str, List[dict]] = {}
    for item in items:
        name = name_getter(item)
        if name:
            grouped.setdefault(name, []).append(dict(item))
    duplicates = sorted(name for name, matches in grouped.items() if len(matches) > 1)
    if duplicates:
        raise ProvisioningConflict("存在重复同名对象: " + ", ".join(duplicates))
    return {name: matches[0] for name, matches in grouped.items()}


def _expected_numeric_type(field_spec: Any) -> Optional[int]:
    return getattr(field_spec, "numeric_type", None)


def _actual_numeric_type(item: Mapping[str, Any]) -> Optional[int]:
    value = item.get("type")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_cli_field(item: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize high-level or raw field metadata for managed comparisons."""

    result = dict(item)
    if "name" not in result and result.get("field_name"):
        result["name"] = result.get("field_name")
    return result


def _managed_field_conflicts(field_spec: Any, existing: Mapping[str, Any]) -> List[str]:
    """Return deterministic drift for properties safe to compare locally."""

    errors: List[str] = []
    target = field_spec.cli_json()
    actual = _normalize_cli_field(existing)
    actual_type = actual.get("type")
    actual_numeric_type = _actual_numeric_type(actual)
    expected_numeric_type = _expected_numeric_type(field_spec)
    if (
        actual_numeric_type is not None
        and (isinstance(actual_type, int) or str(actual_type).strip().isdigit())
    ):
        if (
            expected_numeric_type is not None
            and actual_numeric_type != expected_numeric_type
        ):
            errors.append(
                f"类型期望 {expected_numeric_type}，实际 {actual_numeric_type}"
            )
    elif isinstance(actual_type, str) and actual_type and actual_type != target.get("type"):
        errors.append(f"类型期望 {target.get('type')}，实际 {actual_type}")
    elif not isinstance(actual_type, (str, int)):
        errors.append("字段定义缺少类型")

    target_style = target.get("style")
    if target_style is not None:
        if not isinstance(actual.get("style"), Mapping):
            errors.append("字段定义缺少 style")
        elif dict(actual["style"]) != target_style:
            errors.append("字段 style 不一致")

    if "multiple" in target and actual.get("multiple") != target.get("multiple"):
        errors.append(
            f"multiple 期望 {target.get('multiple')}，实际 {actual.get('multiple')}"
        )

    target_type = target.get("type")
    if target_type == "select":
        _, option_errors = _audit_select_options(field_spec, actual)
        errors.extend(option_errors)
    elif target_type == "formula":
        expression = actual.get("expression")
        if expression is None and isinstance(actual.get("property"), Mapping):
            expression = actual["property"].get("formula")
        if expression is None:
            errors.append("字段定义缺少公式 expression")
        elif str(expression) != str(target.get("expression")):
            errors.append("公式定义不一致")
    elif target_type == "lookup":
        for key in ("from", "select", "aggregate", "where"):
            if key not in actual:
                errors.append(f"Lookup 缺少 {key}")
            elif actual.get(key) != target.get(key):
                errors.append(f"Lookup {key} 不一致")
    elif target_type == "auto_number":
        # style is handled generically above.
        pass
    return errors


def _select_options(existing: Mapping[str, Any]) -> Any:
    """Return static select options without pretending an absent list is empty."""

    if "options" in existing:
        return existing.get("options")
    property_data = existing.get("property")
    if isinstance(property_data, Mapping) and "options" in property_data:
        return property_data.get("options")
    return None


def _audit_select_options(
    field_spec: Any,
    existing: Mapping[str, Any],
) -> tuple[List[str], List[str]]:
    """Return additive gaps and unsafe static-option metadata errors.

    Missing required names are the sole field drift that provisioning repairs.
    A malformed list, duplicate name, or dynamic source remains fail-closed so
    a full PUT can never silently reinterpret the column.
    """

    target = field_spec.cli_json()
    if target.get("type") != "select":
        return [], []

    errors: List[str] = []
    property_data = existing.get("property")
    dynamic_source = existing.get("dynamic_options_source")
    if dynamic_source is None and isinstance(property_data, Mapping):
        dynamic_source = property_data.get("dynamic_options_source")
    if dynamic_source:
        errors.append("字段使用动态选项来源，不能自动补齐静态单选项")

    options = _select_options(existing)
    if not isinstance(options, list):
        errors.append("字段定义缺少单选项")
        return [], errors

    actual_names: List[str] = []
    seen: set[str] = set()
    for index, option in enumerate(options):
        if not isinstance(option, Mapping):
            errors.append(f"单选项[{index}] 不是对象")
            continue
        name = option.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"单选项[{index}] 缺少有效 name")
            continue
        if name in seen:
            errors.append(f"单选项名称重复: {name}")
            continue
        seen.add(name)
        actual_names.append(name)
        for key in ("hue", "lightness"):
            if key in option and (
                not isinstance(option.get(key), str) or not str(option.get(key)).strip()
            ):
                errors.append(f"单选项 {name} 的 {key} 无效")
        if "color" in option and not isinstance(option.get("color"), (str, int)):
            errors.append(f"单选项 {name} 的 color 无效")

    target_names: List[str] = []
    for index, option in enumerate(target.get("options", [])):
        name = option.get("name") if isinstance(option, Mapping) else None
        if not isinstance(name, str) or not name.strip():
            errors.append(f"契约单选项[{index}] 缺少有效 name")
            continue
        if name in target_names:
            errors.append(f"契约单选项名称重复: {name}")
            continue
        target_names.append(name)

    missing = [name for name in target_names if name not in seen]
    return missing, errors


def _select_update_payload(
    field_spec: Any,
    existing: Mapping[str, Any],
    missing: Sequence[str],
) -> Dict[str, Any]:
    """Build the typed full-PUT definition from a freshly read baseline."""

    options = _select_options(existing)
    if not isinstance(options, list):
        raise ProvisioningConflict("字段定义缺少单选项")
    payload_options: List[Dict[str, Any]] = []
    for option in options:
        # IDs and field/default metadata are read-only in the typed CLI JSON.
        # Matching names let Feishu retain existing option identities while
        # visual properties and unmanaged options remain unchanged.
        item: Dict[str, Any] = {"name": str(option["name"])}
        for key in ("hue", "lightness", "color"):
            if key in option:
                item[key] = option[key]
        payload_options.append(item)
    payload_options.extend({"name": name} for name in missing)

    payload: Dict[str, Any] = {
        "name": field_spec.name,
        "type": "select",
        "multiple": bool(existing.get("multiple", False)),
        "options": payload_options,
    }
    description = existing.get("description")
    if description is not None:
        payload["description"] = description
    return payload


def _existing_field_payload(item: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the best available full field payload from CLI get envelopes."""

    value: Any = item
    for key in ("field", "data"):
        if isinstance(value, Mapping) and isinstance(value.get(key), Mapping):
            value = value[key]
    return dict(value) if isinstance(value, Mapping) else dict(item)


class SevenTableProvisioner:
    """Plan and apply the complete managed workspace without destructive repair."""

    def __init__(self, admin: Any, *, schema: Any = None) -> None:
        if schema is None:
            from .full_schema import FULL_SCHEMA

            schema = FULL_SCHEMA
        self.admin = admin
        self.schema = schema

    def plan(
        self,
        *,
        include_workflow: bool = True,
        enable_workflow: bool = True,
    ) -> ProvisioningPlan:
        plan = ProvisioningPlan()
        try:
            tables = _group_unique(
                _payload_items(self.admin.list_tables(), "tables"),
                _table_name,
            )
        except (ProvisioningError, ProvisioningConflict) as exc:
            plan.conflicts.append(str(exc))
            return plan

        for table_spec in self.schema.tables:
            table = tables.get(table_spec.name)
            if table is None:
                plan.actions.append(
                    PlanAction("create_table", table_spec.name, f"{len(table_spec.fields)} 个字段")
                )
                continue
            table_id = _table_id(table)
            if not table_id:
                plan.conflicts.append(f"数据表 {table_spec.name} 缺少 table_id")
                continue
            try:
                fields = _group_unique(
                    _payload_items(self.admin.list_fields(table_id), "fields"),
                    _field_name,
                )
            except (ProvisioningError, ProvisioningConflict) as exc:
                plan.conflicts.append(f"{table_spec.name}: {exc}")
                continue
            primary_spec = table_spec.fields[0]
            primary = fields.get(primary_spec.name)
            if primary is None:
                plan.conflicts.append(
                    f"{table_spec.name} 主字段必须为 {primary_spec.name}；"
                    "现有表不能通过追加普通字段修复主字段"
                )
            else:
                primary_marker = primary.get("is_primary")
                if primary_marker is False:
                    plan.conflicts.append(
                        f"{table_spec.name}.{primary_spec.name} 不是主字段"
                    )
            for field_spec in table_spec.fields:
                existing = fields.get(field_spec.name)
                if existing is None:
                    plan.actions.append(
                        PlanAction("create_field", f"{table_spec.name}.{field_spec.name}")
                    )
                    continue
                expected_type = _expected_numeric_type(field_spec)
                actual_type = _actual_numeric_type(existing)
                if expected_type is not None and actual_type is not None and actual_type != expected_type:
                    plan.conflicts.append(
                        f"{table_spec.name}.{field_spec.name} 类型冲突: "
                        f"期望 {expected_type}，实际 {actual_type}"
                    )
                    continue
                # field-list is an inventory only.  Always retrieve the full
                # typed definition so ordinary number/date/user/url fields
                # are checked as strictly as formula/lookup fields.
                field_id = str(existing.get("field_id") or existing.get("id") or field_spec.name)
                try:
                    full_field = self.admin.get_field(table_id, field_id)
                except Exception as exc:
                    plan.conflicts.append(
                        f"{table_spec.name}.{field_spec.name} 无法读取完整字段定义: {exc}"
                    )
                    continue
                full_field = _existing_field_payload(full_field)
                missing_options, option_errors = _audit_select_options(
                    field_spec,
                    full_field,
                )
                field_conflicts = _managed_field_conflicts(field_spec, full_field)
                if missing_options and not option_errors and not field_conflicts:
                    plan.actions.append(
                        PlanAction(
                            "update_select_options",
                            f"{table_spec.name}.{field_spec.name}",
                            "补齐: " + ", ".join(missing_options),
                        )
                    )
                for conflict in field_conflicts:
                    plan.conflicts.append(
                        f"{table_spec.name}.{field_spec.name} 元数据冲突: {conflict}"
                    )

        if include_workflow:
            try:
                workflows = _group_unique(
                    _payload_items(self.admin.list_workflows(), "workflows"),
                    _workflow_title,
                )
                existing = workflows.get(self.schema.workflow_title)
                if existing is None:
                    plan.actions.append(
                        PlanAction("create_workflow", self.schema.workflow_title, "创建后先保持禁用")
                    )
                else:
                    definition: Mapping[str, Any] = {}
                    workflow_id = _workflow_id(existing)
                    if not workflow_id:
                        plan.conflicts.append("同名 Workflow 缺少 workflow_id")
                    else:
                        # A same-title workflow is never overwritten.  When
                        # its dependencies already exist, validate its full
                        # five-step semantics before any additive table or
                        # field write begins.
                        try:
                            from .cooperation_workflow import (
                                validate_first_cooperation_workflow,
                            )

                            workflow_args = self._workflow_runtime_args()
                            definition = self.admin.get_workflow(workflow_id)
                            validate_first_cooperation_workflow(
                                definition,
                                **workflow_args,
                            )
                        except Exception as exc:
                            plan.conflicts.append(
                                f"同名 Workflow 无法安全复用: {exc}"
                            )
                    status = str(
                        existing.get("status")
                        or definition.get("status")
                        or ""
                    ).lower()
                    if enable_workflow and status != "enabled":
                        plan.actions.append(
                            PlanAction(
                                "enable_workflow",
                                self.schema.workflow_title,
                                "在数据写删预检通过后启用",
                            )
                        )
            except (ProvisioningError, ProvisioningConflict) as exc:
                plan.conflicts.append(f"Workflow: {exc}")
        return plan

    def apply(
        self,
        *,
        dry_run: bool = False,
        include_workflow: bool = True,
        enable_workflow: bool = True,
        initialize_view_tables: Sequence[str] = (),
        journal_pending_views: Optional[Callable[[Sequence[str]], None]] = None,
    ) -> ProvisioningResult:
        initial_plan = self.plan(
            include_workflow=include_workflow,
            # ``apply(enable_workflow=False)`` is the first half of the main
            # setup transaction; it must not treat the intentionally deferred
            # enable as an incomplete metadata mutation.
            enable_workflow=enable_workflow,
        )
        if not initial_plan.safe:
            raise ProvisioningConflict("；".join(initial_plan.conflicts))
        if dry_run:
            return ProvisioningResult(
                plan=initial_plan,
                pending_view_tables=[
                    action.target
                    for action in initial_plan.actions
                    if action.kind == "create_table"
                ],
                dry_run=True,
            )

        table_ids: Dict[str, str] = {}
        planned_table_names = {
            action.target
            for action in initial_plan.actions
            if action.kind == "create_table"
        }
        if planned_table_names and journal_pending_views is not None:
            # Persist recovery intent before the first remote mutation.  A
            # crash after table creation can then safely finish default-view
            # ordering on the next process run.
            journal_pending_views(sorted(planned_table_names))
        created_table_names: set[str] = set(initialize_view_tables) | planned_table_names
        tables = _group_unique(
            _payload_items(self.admin.list_tables(), "tables"),
            _table_name,
        )

        # First create missing tables with only their primary field.  Creating
        # all table shells first makes every cross-table dependency resolvable.
        for table_spec in self.schema.tables:
            existing = tables.get(table_spec.name)
            if existing is None:
                response = self.admin.create_table(
                    table_spec.name,
                    [table_spec.fields[0].cli_json()],
                )
                created_table_names.add(table_spec.name)
                created = self._extract_created_table(response, table_spec.name)
                tables[table_spec.name] = created
            table_id = _table_id(tables[table_spec.name])
            if not table_id:
                # Some CLI versions return a minimal creation payload.  A
                # strict re-list resolves it and also detects duplicate races.
                tables = _group_unique(
                    _payload_items(self.admin.list_tables(), "tables"),
                    _table_name,
                )
                table_id = _table_id(tables.get(table_spec.name, {}))
            if not table_id:
                raise ProvisioningError(f"创建数据表后无法解析 table_id: {table_spec.name}")
            table_ids[table_spec.name] = table_id

        # Field phases are declared by the schema.  Ordinary source fields are
        # therefore always present before formula and lookup fields.
        for phase in self.schema.field_phases:
            for table_spec in self.schema.tables:
                table_id = table_ids[table_spec.name]
                fields = _group_unique(
                    _payload_items(self.admin.list_fields(table_id), "fields"),
                    _field_name,
                )
                for field_spec in table_spec.fields:
                    if field_spec.primary or field_spec.phase != phase:
                        continue
                    if field_spec.name in fields:
                        continue
                    self.admin.create_field(table_id, field_spec.cli_json())
                    fields = _group_unique(
                        _payload_items(self.admin.list_fields(table_id), "fields"),
                        _field_name,
                    )
                    if field_spec.name not in fields:
                        raise ProvisioningError(
                            f"字段创建后回读缺失: {table_spec.name}.{field_spec.name}"
                        )

        # Missing static select options are the only safe in-place metadata
        # repair.  Re-read immediately before each full PUT, preserve all
        # existing/custom choices, append only the current delta, and verify.
        self._apply_select_option_updates(initial_plan, table_ids)

        initialized_view_tables = self._initialize_created_table_views(
            created_table_names,
            table_ids,
        )

        workflow_id = ""
        workflow_enabled = False
        if include_workflow:
            workflow_id, workflow_enabled = self._ensure_workflow(enable=enable_workflow)

        final_plan = self.plan(
            include_workflow=include_workflow,
            enable_workflow=enable_workflow,
        )
        if not final_plan.safe:
            raise ProvisioningConflict("；".join(final_plan.conflicts))
        if final_plan.mutation_count:
            raise ProvisioningError(
                f"完整部署后仍有 {final_plan.mutation_count} 项未完成"
            )
        return ProvisioningResult(
            plan=final_plan,
            table_ids=table_ids,
            workflow_id=workflow_id,
            workflow_enabled=workflow_enabled,
            initialized_view_tables=initialized_view_tables,
            pending_view_tables=sorted(created_table_names),
        )

    def _initialize_created_table_views(
        self,
        table_names: set[str],
        table_ids: Mapping[str, str],
    ) -> List[str]:
        """Order the one platform-created grid view on newly created tables.

        Existing tables are deliberately excluded so setup never overwrites a
        user's view layout.  No views, filters, sorts, or groups are created.
        """

        initialized: List[str] = []
        for table_spec in self.schema.tables:
            if table_spec.name not in table_names:
                continue
            table_id = table_ids[table_spec.name]
            views = _payload_items(self.admin.list_views(table_id), "views")
            if len(views) != 1:
                raise ProvisioningConflict(
                    f"新建表 {table_spec.name} 预期恰好一个默认视图，实际 {len(views)} 个"
                )
            view = views[0]
            view_type = str(view.get("view_type") or view.get("type") or "grid").lower()
            if view_type not in {"grid", "1"}:
                raise ProvisioningConflict(
                    f"新建表 {table_spec.name} 的默认视图不是 grid"
                )
            view_id = _view_id(view)
            if not view_id:
                raise ProvisioningError(f"新建表 {table_spec.name} 的默认视图缺少 view_id")
            expected = [
                field_spec.name
                for field_spec in table_spec.fields
                if not getattr(field_spec, "hidden_in_default_view", False)
            ]
            self.admin.set_view_visible_fields(table_id, view_id, expected)
            actual = _visible_field_names(
                self.admin.get_view_visible_fields(table_id, view_id)
            )
            if actual != expected:
                raise ProvisioningError(
                    f"新建表 {table_spec.name} 默认视图字段顺序回读不一致"
                )
            initialized.append(table_spec.name)
        return initialized

    def _apply_select_option_updates(
        self,
        initial_plan: ProvisioningPlan,
        table_ids: Mapping[str, str],
    ) -> None:
        targets = {
            action.target
            for action in initial_plan.actions
            if action.kind == "update_select_options"
        }
        if not targets:
            return
        for table_spec in self.schema.tables:
            table_id = table_ids[table_spec.name]
            fields = _group_unique(
                _payload_items(self.admin.list_fields(table_id), "fields"),
                _field_name,
            )
            for field_spec in table_spec.fields:
                target = f"{table_spec.name}.{field_spec.name}"
                if target not in targets:
                    continue
                item = fields.get(field_spec.name)
                if item is None:
                    raise ProvisioningError(
                        f"补齐单选项前字段缺失: {table_spec.name}.{field_spec.name}"
                    )
                field_id = self._require_field_id(item)
                current = _existing_field_payload(
                    self.admin.get_field(table_id, field_id)
                )
                missing, errors = _audit_select_options(field_spec, current)
                non_option_errors = _managed_field_conflicts(field_spec, current)
                if errors or non_option_errors:
                    detail = errors + non_option_errors
                    raise ProvisioningConflict(
                        f"{table_spec.name}.{field_spec.name} 无法安全补齐: "
                        + "; ".join(detail)
                    )
                if not missing:
                    continue
                payload = _select_update_payload(field_spec, current, missing)
                self.admin.update_field(table_id, field_id, payload)
                updated = _existing_field_payload(
                    self.admin.get_field(table_id, field_id)
                )
                remaining, errors = _audit_select_options(field_spec, updated)
                if errors or remaining or _managed_field_conflicts(field_spec, updated):
                    raise ProvisioningError(
                        f"单选项补齐后回读不一致: {table_spec.name}.{field_spec.name}"
                    )

    def ensure_workflow_enabled(self) -> tuple[str, bool]:
        """Strictly validate the managed Workflow and enable it if needed."""

        return self._ensure_workflow(enable=True)

    def _ensure_workflow(self, *, enable: bool) -> tuple[str, bool]:
        from .cooperation_workflow import (
            build_first_cooperation_workflow,
            validate_first_cooperation_workflow,
        )

        workflows = _group_unique(
            _payload_items(self.admin.list_workflows(), "workflows"),
            _workflow_title,
        )
        existing = workflows.get(self.schema.workflow_title)
        if existing is not None:
            workflow_id = _workflow_id(existing)
            if not workflow_id:
                raise ProvisioningConflict("同名 Workflow 缺少 workflow_id")
            definition = self.admin.get_workflow(workflow_id)
            workflow_args = self._workflow_runtime_args()
            validate_first_cooperation_workflow(definition, **workflow_args)
            status = str(existing.get("status") or definition.get("status") or "").lower()
            if enable and status != "enabled":
                self._enable_workflow_with_readback(workflow_id)
                return workflow_id, True
            return workflow_id, status == "enabled"

        workflow_args = self._workflow_runtime_args()
        definition = build_first_cooperation_workflow(
            client_token=str(uuid.uuid4()),
            **workflow_args,
        )
        creator = getattr(self.admin, "create_workflow_idempotent", None)
        response = (
            creator(definition)
            if callable(creator)
            else self.admin.create_workflow(definition)
        )
        workflow_id = _workflow_id(response)
        if not workflow_id:
            workflows = _group_unique(
                _payload_items(self.admin.list_workflows(), "workflows"),
                _workflow_title,
            )
            workflow_id = _workflow_id(workflows.get(self.schema.workflow_title, {}))
        if not workflow_id:
            raise ProvisioningError("Workflow 创建后无法解析 workflow_id")
        readback = self.admin.get_workflow(workflow_id)
        validate_first_cooperation_workflow(readback, **workflow_args)
        if enable:
            self._enable_workflow_with_readback(workflow_id)
            return workflow_id, True
        return workflow_id, False

    def _enable_workflow_with_readback(self, workflow_id: str) -> None:
        """Enable and compensate only when this exact call issued the write."""

        self.admin.enable_workflow(workflow_id)
        try:
            readback = self.admin.get_workflow(workflow_id)
            status = str(readback.get("status") or "").lower()
            if status != "enabled":
                raise ProvisioningError("Workflow 启用后状态回读不是 enabled")
        except Exception:
            try:
                self.admin.disable_workflow(workflow_id)
            except Exception:
                # Preserve the original verification failure.  A rerun will
                # read and validate the remote state before making new writes.
                pass
            raise

    def _field_map(self, table_name: str) -> Dict[str, str]:
        tables = _group_unique(
            _payload_items(self.admin.list_tables(), "tables"),
            _table_name,
        )
        table_id = _table_id(tables.get(table_name, {}))
        if not table_id:
            raise ProvisioningError(f"Workflow 来源表不存在: {table_name}")
        fields = _group_unique(
            _payload_items(self.admin.list_fields(table_id), "fields"),
            _field_name,
        )
        result = {
            name: str(item.get("field_id") or item.get("id") or "")
            for name, item in fields.items()
        }
        missing_ids = sorted(name for name, field_id in result.items() if not field_id)
        if missing_ids:
            raise ProvisioningError(f"字段缺少 field_id: {table_name}.{missing_ids}")
        return result

    def _workflow_runtime_args(self) -> Dict[str, Any]:
        source_table_id, source_fields = self._field_metadata(self.schema.influencer_table)
        target_table_id, target_fields = self._field_metadata(self.schema.cooperation_table)
        try:
            source_email = source_fields["联系邮箱"]
            source_channel = source_fields["Channel ID"]
            development = source_fields["开发状态"]
            cooperation_status = target_fields["合作状态"]
        except KeyError as exc:
            raise ProvisioningError(f"Workflow 依赖字段缺失: {exc.args[0]}") from exc
        # field-list is enough for IDs but some CLI releases omit option
        # metadata there.  field-get is the authoritative full definition.
        development = _existing_field_payload(
            self.admin.get_field(source_table_id, self._require_field_id(development))
        )
        cooperation_status = _existing_field_payload(
            self.admin.get_field(
                target_table_id, self._require_field_id(cooperation_status)
            )
        )
        return {
            "influencer_contact_email_field_id": self._require_field_id(source_email),
            "influencer_channel_id_field_id": self._require_field_id(source_channel),
            "enter_cooperation_option": self._find_option(development, "进入合作"),
            "initial_cooperation_status_option": self._find_option(
                cooperation_status, "待确认方案"
            ),
        }

    def _field_metadata(self, table_name: str) -> tuple[str, Dict[str, dict]]:
        tables = _group_unique(
            _payload_items(self.admin.list_tables(), "tables"),
            _table_name,
        )
        table_id = _table_id(tables.get(table_name, {}))
        if not table_id:
            raise ProvisioningError(f"Workflow 来源表不存在: {table_name}")
        return table_id, _group_unique(
            _payload_items(self.admin.list_fields(table_id), "fields"),
            _field_name,
        )

    @staticmethod
    def _require_field_id(item: Mapping[str, Any]) -> str:
        field_id = str(item.get("field_id") or item.get("id") or "")
        if not field_id:
            raise ProvisioningError(f"字段缺少 field_id: {_field_name(item)}")
        return field_id

    @staticmethod
    def _find_option(field_item: Mapping[str, Any], option_name: str) -> Dict[str, str]:
        containers: Sequence[Any] = (
            field_item.get("options"),
            (field_item.get("property") or {}).get("options")
            if isinstance(field_item.get("property"), Mapping)
            else None,
        )
        for options in containers:
            if not isinstance(options, list):
                continue
            for option in options:
                if not isinstance(option, Mapping) or str(option.get("name") or "") != option_name:
                    continue
                option_id = str(option.get("id") or option.get("option_id") or "")
                result = {"name": option_name}
                if option_id:
                    result["id"] = option_id
                return result
        raise ProvisioningError(
            f"字段 {_field_name(field_item)} 缺少选项: {option_name}"
        )

    @staticmethod
    def _extract_created_table(payload: Any, expected_name: str) -> dict:
        value = payload.get("data") if isinstance(payload, Mapping) and payload.get("ok") else payload
        if isinstance(value, Mapping):
            for candidate in (value, value.get("table"), value.get("data")):
                if isinstance(candidate, Mapping):
                    item = dict(candidate)
                    if not _table_name(item):
                        item["name"] = expected_name
                    if _table_id(item):
                        return item
        return {"name": expected_name}


def cleanup_new_base_default_tables(client: Any, *, managed_table_ids: set[str]) -> List[str]:
    """Delete only confirmed-empty unmanaged tables from a newly created Base.

    This helper is intentionally invoked only after the complete workspace and
    Workflow have passed verification.  It never runs for an existing Base and
    refuses to delete a table whose emptiness cannot be established.
    """

    deleted: List[str] = []
    for table in client.list_tables():
        table_id = str(table.get("table_id") or "")
        if not table_id or table_id in managed_table_ids:
            continue
        records = client.get_all_records(table_id, page_size=1)
        if records:
            continue
        client.delete_table(table_id)
        deleted.append(table_id)
    return deleted
