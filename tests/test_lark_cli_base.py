import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu.lark_cli_base import (
    REQUIRED_BASE_COMMANDS,
    LarkCliBaseCommandError,
    LarkCliBaseResponseError,
    LarkCliBaseRunner,
    LarkCliBaseTimeoutError,
)


class RecordingRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(command, kwargs)
        return response


def completed(payload, *, returncode=0, stderr=""):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr=stderr,
    )


def text_completed(stdout="", *, returncode=0, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def make_runner(
    runner, *, token="bascn_sensitive_base_token", temp_dir=None, dry_run=False
):
    return LarkCliBaseRunner(
        base_token=token,
        profile="kol-test",
        cli_path=r"C:\tools\lark-cli.exe",
        dry_run=dry_run,
        timeout=19,
        runner=runner,
        temp_dir=temp_dir,
    )


def expected_prefix(shortcut):
    return [
        r"C:\tools\lark-cli.exe",
        "--profile",
        "kol-test",
        "base",
        shortcut,
        "--base-token",
        "bascn_sensitive_base_token",
        "--as",
        "user",
        "--format",
        "json",
    ]


def test_constructor_requires_explicit_valid_target_and_identity():
    with pytest.raises(TypeError):
        LarkCliBaseRunner(profile="kol-test")
    with pytest.raises(TypeError):
        LarkCliBaseRunner(base_token="bascn_token")
    with pytest.raises(ValueError, match="base_token"):
        LarkCliBaseRunner(base_token=" ", profile="kol-test")
    with pytest.raises(ValueError, match="profile"):
        LarkCliBaseRunner(base_token="bascn_token", profile=" ")
    with pytest.raises(ValueError, match="identity"):
        LarkCliBaseRunner(
            base_token="bascn_token", profile="kol-test", identity="tenant"
        )
    with pytest.raises(TypeError, match="dry_run"):
        LarkCliBaseRunner(
            base_token="bascn_token", profile="kol-test", dry_run=1
        )


def test_list_tables_uses_explicit_profile_base_and_never_uses_a_shell():
    process = RecordingRunner(
        [completed({"ok": True, "data": {"items": [{"table_id": "tbl1"}]}})]
    )
    cli = make_runner(process)

    assert cli.list_tables() == [{"table_id": "tbl1"}]

    command, kwargs = process.calls[0]
    assert command == expected_prefix("+table-list")
    assert kwargs == {
        "input": None,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "capture_output": True,
        "timeout": 19,
        "check": False,
        "shell": False,
    }


@pytest.mark.parametrize(
    ("invoke", "shortcut", "tail", "resource", "payload"),
    [
        (
            lambda cli: cli.get_table("tbl1"),
            "+table-get",
            ["--table-id", "tbl1"],
            "table",
            {"table_id": "tbl1"},
        ),
        (
            lambda cli: cli.get_field("tbl1", "fld1"),
            "+field-get",
            ["--table-id", "tbl1", "--field-id", "fld1"],
            "field",
            {"field_id": "fld1"},
        ),
        (
            lambda cli: cli.get_view("tbl1", "vew1"),
            "+view-get",
            ["--table-id", "tbl1", "--view-id", "vew1"],
            "view",
            {"view_id": "vew1"},
        ),
        (
            lambda cli: cli.get_view_visible_fields("tbl1", "vew1"),
            "+view-get-visible-fields",
            ["--table-id", "tbl1", "--view-id", "vew1"],
            "visible_fields",
            {"visible_fields": ["多行文本", "名称"]},
        ),
        (
            lambda cli: cli.get_workflow("wkf1"),
            "+workflow-get",
            ["--workflow-id", "wkf1"],
            "workflow",
            {"workflow_id": "wkf1"},
        ),
    ],
)
def test_typed_get_helpers(invoke, shortcut, tail, resource, payload):
    process = RecordingRunner(
        [completed({"ok": True, "data": {resource: payload}})]
    )
    cli = make_runner(process)

    assert invoke(cli) == payload
    assert process.calls[0][0] == expected_prefix(shortcut) + tail


@pytest.mark.parametrize(
    ("invoke", "shortcut", "tail", "resource"),
    [
        (lambda cli: cli.list_fields("tbl1"), "+field-list", ["--table-id", "tbl1"], "fields"),
        (lambda cli: cli.list_views("tbl1"), "+view-list", ["--table-id", "tbl1"], "views"),
        (
            lambda cli: cli.list_workflows(status="disabled"),
            "+workflow-list",
            ["--status", "disabled"],
            "workflows",
        ),
    ],
)
def test_typed_list_helpers(invoke, shortcut, tail, resource):
    item = {"name": resource}
    process = RecordingRunner(
        [completed({"ok": True, "data": {resource: [item]}})]
    )
    cli = make_runner(process)

    assert invoke(cli) == [item]
    assert process.calls[0][0] == expected_prefix(shortcut) + tail


def test_mvp_exposes_only_the_single_view_order_write_method():
    cli = make_runner(RecordingRunner([]))

    assert callable(cli.list_views)
    assert callable(cli.get_view)
    assert callable(cli.set_view_visible_fields)
    assert not hasattr(cli, "create_view")
    assert not hasattr(cli, "update_view")


def test_set_view_visible_fields_uses_json_file_without_creating_a_view(tmp_path):
    observed = {}

    def inspect(command, kwargs):
        path = Path(kwargs["cwd"]) / command[command.index("--json") + 1].removeprefix("@")
        observed["payload"] = json.loads(path.read_text(encoding="utf-8"))
        return completed({"ok": True, "data": {"visible_fields": ["多行文本", "名称"]}})

    process = RecordingRunner([inspect])
    cli = make_runner(process, temp_dir=tmp_path)

    cli.set_view_visible_fields("tbl1", "vew1", ["多行文本", "名称"])

    command = process.calls[0][0]
    assert command[: len(expected_prefix("+view-set-visible-fields"))] == expected_prefix(
        "+view-set-visible-fields"
    )
    assert observed["payload"] == {"visible_fields": ["多行文本", "名称"]}


def test_create_table_can_pass_fields_through_a_removed_json_temp_file(tmp_path):
    observed = {}

    def inspect(command, kwargs):
        argument = command[command.index("--fields") + 1]
        assert argument.startswith("@")
        path = Path(kwargs["cwd"]) / argument[1:]
        assert path.exists()
        observed["path"] = path
        observed["payload"] = json.loads(path.read_text(encoding="utf-8"))
        return completed({"ok": True, "data": {"table_id": "tbl-new"}})

    process = RecordingRunner([inspect])
    cli = make_runner(process, temp_dir=tmp_path)
    fields = [{"name": "名称", "type": "text"}]

    assert cli.create_table("合作档案", fields=fields, dry_run=True) == {
        "table_id": "tbl-new"
    }

    command = process.calls[0][0]
    assert command[: len(expected_prefix("+table-create"))] == expected_prefix(
        "+table-create"
    )
    assert command[len(expected_prefix("+table-create")) : len(expected_prefix("+table-create")) + 2] == [
        "--name",
        "合作档案",
    ]
    assert command[-1] == "--dry-run"
    assert observed["payload"] == fields
    assert not observed["path"].exists()
    assert json.dumps(fields, ensure_ascii=False) not in command


def test_create_field_uses_json_file_and_inherits_runner_dry_run(tmp_path):
    observed = {}

    def inspect(command, kwargs):
        argument = command[command.index("--json") + 1]
        path = Path(kwargs["cwd"]) / argument.removeprefix("@")
        observed["path"] = path
        observed["payload"] = json.loads(path.read_text(encoding="utf-8"))
        return completed({"ok": True, "data": {"field_id": "fld-new"}})

    process = RecordingRunner([inspect])
    cli = make_runner(process, temp_dir=tmp_path, dry_run=True)
    definition = {
        "name": "负责人",
        "type": "user",
        "multiple": False,
    }

    assert cli.create_field("tbl1", definition) == {
        "field_id": "fld-new"
    }
    command = process.calls[0][0]
    assert command[: len(expected_prefix("+field-create"))] == expected_prefix(
        "+field-create"
    )
    assert command[-1] == "--dry-run"
    assert observed["payload"] == definition
    assert not observed["path"].exists()
    assert "负责人" not in command


@pytest.mark.parametrize("field_type", ["formula", "lookup", "FORMULA"])
def test_formula_and_lookup_field_create_acknowledges_read_guide(
    tmp_path, field_type
):
    process = RecordingRunner(
        [completed({"ok": True, "data": {"field_id": "fld-derived"}})]
    )
    cli = make_runner(process, temp_dir=tmp_path)

    cli.create_field(
        "tbl1",
        {"name": "派生字段", "type": field_type, "expression": "[金额]"},
    )

    assert "--i-have-read-guide" in process.calls[0][0]


def test_simple_field_create_does_not_claim_complex_field_guide(tmp_path):
    process = RecordingRunner(
        [completed({"ok": True, "data": {"field_id": "fld-text"}})]
    )
    cli = make_runner(process, temp_dir=tmp_path)

    cli.create_field("tbl1", {"name": "备注", "type": "text"})

    assert "--i-have-read-guide" not in process.calls[0][0]


@pytest.mark.parametrize(
    ("dry_run", "expected_tail"),
    [(True, ["--dry-run"]), (False, ["--yes"])],
)
def test_field_update_distinguishes_validation_from_confirmed_write(
    tmp_path, dry_run, expected_tail
):
    process = RecordingRunner(
        [completed({"ok": True, "data": {"field_id": "fld1"}})]
    )
    cli = make_runner(process, temp_dir=tmp_path)

    cli.update_field(
        "tbl1",
        "fld1",
        {"name": "金额", "type": "number", "formatter": "0.00"},
        dry_run=dry_run,
    )

    command = process.calls[0][0]
    assert command[-1:] == expected_tail
    assert ("--dry-run" in command) is dry_run
    assert ("--yes" in command) is (not dry_run)


def test_lookup_field_update_acknowledges_guide_before_confirmation(tmp_path):
    process = RecordingRunner(
        [completed({"ok": True, "data": {"field_id": "fld1"}})]
    )
    cli = make_runner(process, temp_dir=tmp_path)

    cli.update_field(
        "tbl1",
        "fld1",
        {"name": "来源", "type": "lookup", "from": "来源表"},
    )

    command = process.calls[0][0]
    assert "--i-have-read-guide" in command
    assert command[-1] == "--yes"


@pytest.mark.parametrize(
    ("invoke", "shortcut", "id_tail", "dry_run"),
    [
        (
            lambda cli: cli.create_workflow(
                {"title": "首次进入合作自动建档", "client_token": "unique"},
                dry_run=True,
            ),
            "+workflow-create",
            [],
            True,
        ),
        (
            lambda cli: cli.update_workflow(
                "wkf1", {"title": "首次进入合作自动建档", "steps": []}, dry_run=False
            ),
            "+workflow-update",
            ["--workflow-id", "wkf1"],
            False,
        ),
    ],
)
def test_workflow_definitions_use_temporary_json_files(
    tmp_path, invoke, shortcut, id_tail, dry_run
):
    observed = {}

    def inspect(command, kwargs):
        path = Path(kwargs["cwd"]) / command[command.index("--json") + 1].removeprefix("@")
        observed["path"] = path
        observed["payload"] = json.loads(path.read_text(encoding="utf-8"))
        return completed({"ok": True, "data": {"workflow_id": "wkf1"}})

    process = RecordingRunner([inspect])
    cli = make_runner(process, temp_dir=tmp_path)

    assert invoke(cli) == {"workflow_id": "wkf1"}

    command = process.calls[0][0]
    assert command[: len(expected_prefix(shortcut))] == expected_prefix(shortcut)
    for expected in id_tail:
        assert expected in command
    assert ("--dry-run" in command) is dry_run
    assert observed["payload"]["title"] == "首次进入合作自动建档"
    assert not observed["path"].exists()
    assert "首次进入合作自动建档" not in command


def test_idempotent_workflow_create_reuses_existing_title_without_write():
    process = RecordingRunner(
        [
            completed(
                {
                    "ok": True,
                    "data": {
                        "items": [
                            {
                                "workflow_id": "wkf-existing",
                                "title": "首次进入合作自动建档",
                            }
                        ]
                    },
                }
            )
        ]
    )
    cli = make_runner(process)

    result = cli.create_workflow_idempotent(
        {"title": "首次进入合作自动建档", "client_token": "stable-token"}
    )

    assert result["workflow_id"] == "wkf-existing"
    assert [call[0][4] for call in process.calls] == ["+workflow-list"]


def test_idempotent_workflow_create_recovers_timeout_by_relisting(tmp_path):
    observed_tokens = []

    def inspect_create(command, kwargs):
        path = Path(kwargs["cwd"]) / command[command.index("--json") + 1].removeprefix("@")
        observed_tokens.append(json.loads(path.read_text(encoding="utf-8"))["client_token"])
        raise subprocess.TimeoutExpired(command, 19)

    process = RecordingRunner(
        [
            completed({"ok": True, "data": {"items": []}}),
            inspect_create,
            completed(
                {
                    "ok": True,
                    "data": {
                        "items": [
                            {
                                "workflow_id": "wkf-created",
                                "title": "首次进入合作自动建档",
                            }
                        ]
                    },
                }
            ),
        ]
    )
    cli = make_runner(process, temp_dir=tmp_path)

    result = cli.create_workflow_idempotent(
        {"title": "首次进入合作自动建档", "client_token": "stable-token"}
    )

    assert result["workflow_id"] == "wkf-created"
    assert observed_tokens == ["stable-token"]
    assert [call[0][4] for call in process.calls] == [
        "+workflow-list",
        "+workflow-create",
        "+workflow-list",
    ]


@pytest.mark.parametrize("state", ["enable", "disable"])
def test_workflow_state_change_is_an_explicit_write_or_dry_run(state):
    process = RecordingRunner([completed({"ok": True, "data": {"status": state}})])
    cli = make_runner(process)

    method = getattr(cli, f"{state}_workflow")
    assert method("wkf1", dry_run=True) == {"status": state}
    assert process.calls[0][0] == expected_prefix(f"+workflow-{state}") + [
        "--workflow-id",
        "wkf1",
        "--dry-run",
    ]


def test_provisioner_compatible_write_signatures_default_to_real_write(tmp_path):
    process = RecordingRunner(
        [
            completed({"ok": True, "data": {"table": {"table_id": "tbl1"}}}),
            completed({"ok": True, "data": {"field_id": "fld1"}}),
            completed({"ok": True, "data": {"workflow_id": "wkf1"}}),
            completed({"ok": True, "data": {"status": "enabled"}}),
            completed({"ok": True, "data": {"status": "disabled"}}),
        ]
    )
    cli = make_runner(process, temp_dir=tmp_path)

    cli.create_table("合作档案", [{"name": "多行文本", "type": "text"}])
    cli.create_field("tbl1", {"name": "负责人", "type": "user"})
    cli.create_workflow({"title": "首次进入合作自动建档", "client_token": "unique"})
    cli.enable_workflow("wkf1")
    cli.disable_workflow("wkf1")

    assert all("--dry-run" not in command for command, _kwargs in process.calls)


def test_capability_probe_uses_only_local_version_and_help_commands():
    help_text = "Available commands:\n" + "\n".join(REQUIRED_BASE_COMMANDS[:-1])
    process = RecordingRunner(
        [
            text_completed("lark-cli version 1.0.70\n"),
            text_completed(help_text),
        ]
    )
    cli = make_runner(process)

    probe = cli.probe_capabilities()

    assert probe.version == "1.0.70"
    assert probe.available_commands == REQUIRED_BASE_COMMANDS[:-1]
    assert probe.missing_commands == (REQUIRED_BASE_COMMANDS[-1],)
    assert probe.supported is False
    assert [call[0] for call in process.calls] == [
        [r"C:\tools\lark-cli.exe", "--version"],
        [r"C:\tools\lark-cli.exe", "base", "--help"],
    ]
    assert all("bascn_sensitive_base_token" not in call[0] for call in process.calls)


def test_cli_errors_redact_base_tokens_credentials_and_personal_data():
    token = "bascn_do_not_echo_123456"
    process = RecordingRunner(
        [
            completed(
                {
                    "ok": False,
                    "error": {
                        "code": "AUTH_FAILED",
                        "message": (
                            f"base {token}; access_token=very-secret; "
                            "Bearer bearer-secret; owner@example.com"
                        ),
                    },
                },
                returncode=1,
            )
        ]
    )
    cli = make_runner(process, token=token)

    with pytest.raises(LarkCliBaseCommandError) as caught:
        cli.list_tables()

    message = str(caught.value)
    assert "exit=1" in message
    assert "AUTH_FAILED" in message
    assert token not in message
    assert "very-secret" not in message
    assert "bearer-secret" not in message
    assert "owner@example.com" not in message


def test_timeout_does_not_echo_command_or_base_token():
    process = RecordingRunner(
        [subprocess.TimeoutExpired(cmd=["lark-cli", "secret-endpoint"], timeout=19)]
    )
    cli = make_runner(process)

    with pytest.raises(LarkCliBaseTimeoutError, match="timed out after 19 seconds") as caught:
        cli.list_tables()

    assert "bascn_sensitive_base_token" not in str(caught.value)
    assert "secret-endpoint" not in str(caught.value)


def test_invalid_json_and_unexpected_shapes_have_sanitized_diagnostics():
    process = RecordingRunner(
        [
            text_completed("not-json", stderr="device_code=secret123"),
            completed({"ok": True, "data": {"unexpected": "value"}}),
        ]
    )
    cli = make_runner(process)

    with pytest.raises(LarkCliBaseResponseError, match="invalid JSON") as caught:
        cli.list_tables()
    assert "secret123" not in str(caught.value)

    with pytest.raises(LarkCliBaseResponseError, match="tables list"):
        cli.list_tables()


def test_rejects_invalid_status_and_non_boolean_dry_run_before_subprocess():
    process = RecordingRunner([])
    cli = make_runner(process)

    with pytest.raises(ValueError, match="status"):
        cli.list_workflows(status="paused")
    with pytest.raises(TypeError, match="dry_run"):
        cli.create_table(
            "合作档案", [{"name": "多行文本", "type": "text"}], dry_run=1
        )
    assert process.calls == []


def test_table_create_rejects_missing_primary_schema_before_subprocess():
    process = RecordingRunner([])
    cli = make_runner(process)

    with pytest.raises(TypeError):
        cli.create_table("合作档案")
    with pytest.raises(ValueError, match="at least the primary field"):
        cli.create_table("合作档案", [])
    assert process.calls == []
