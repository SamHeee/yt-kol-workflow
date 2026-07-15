import json
import subprocess
from types import SimpleNamespace

import pytest

from feishu.lark_cli_bitable import LarkCliBitableClient


class RecordingRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def completed(payload, *, returncode=0, stderr=""):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr=stderr,
    )


def make_client(runner, token="bascn_sensitive_token"):
    return LarkCliBitableClient(
        token,
        cli_path=r"C:\tools\lark-cli.exe",
        profile="kol-test",
        timeout=17,
        runner=runner,
    )


def test_get_passes_json_params_and_never_uses_a_shell():
    runner = RecordingRunner([
        completed({"ok": True, "data": {"items": [{"table_id": "tbl1"}]}})
    ])
    client = make_client(runner)

    result = client._get("/tables", {"page_size": 50, "page_token": "下一页"})

    assert result == {
        "code": 0,
        "msg": "success",
        "data": {"items": [{"table_id": "tbl1"}]},
    }
    command, kwargs = runner.calls[0]
    assert command == [
        r"C:\tools\lark-cli.exe",
        "--profile",
        "kol-test",
        "api",
        "GET",
        "/open-apis/bitable/v1/apps/bascn_sensitive_token/tables",
        "--as",
        "user",
        "--format",
        "json",
        "--params",
        '{"page_size":50,"page_token":"下一页"}',
    ]
    assert kwargs == {
        "input": None,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "capture_output": True,
        "timeout": 17,
        "check": False,
        "shell": False,
    }


def test_request_body_is_sent_only_through_stdin():
    runner = RecordingRunner([
        completed(
            {
                "ok": True,
                "data": {
                    "code": 0,
                    "msg": "ok",
                    "data": {"table_id": "tbl-new"},
                },
            }
        )
    ])
    client = make_client(runner)

    assert client.create_table("网红详情表") == "tbl-new"

    command, kwargs = runner.calls[0]
    assert command[-2:] == ["--data", "-"]
    assert "网红详情表" not in " ".join(command)
    assert json.loads(kwargs["input"]) == {"table": {"name": "网红详情表"}}


def test_inherited_delete_table_and_update_record_use_cli_transport():
    runner = RecordingRunner(
        [
            completed({"ok": True, "data": {"code": 0, "data": {}}}),
            completed(
                {
                    "ok": True,
                    "data": {
                        "code": 0,
                        "data": {
                            "record": {
                                "record_id": "rec1",
                                "fields": {"状态": "已更新"},
                            }
                        },
                    },
                }
            ),
        ]
    )
    client = make_client(runner)

    assert client.delete_table("tbl-temp") is True
    assert client.update_record("tbl1", "rec1", {"状态": "已更新"}) == {
        "record_id": "rec1",
        "fields": {"状态": "已更新"},
    }

    delete_command, delete_kwargs = runner.calls[0]
    assert delete_command[4:7] == [
        "DELETE",
        "/open-apis/bitable/v1/apps/bascn_sensitive_token/tables/tbl-temp",
        "--as",
    ]
    assert delete_kwargs["input"] is None

    update_command, update_kwargs = runner.calls[1]
    assert update_command[4] == "PUT"
    assert update_command[-2:] == ["--data", "-"]
    assert json.loads(update_kwargs["input"]) == {"fields": {"状态": "已更新"}}


@pytest.mark.parametrize("secret_key", ["device_code", "access_token", "app_secret"])
def test_cli_errors_are_clear_and_redact_tokens(secret_key):
    token = "bascn_do_not_echo"
    runner = RecordingRunner(
        [
            completed(
                {
                    "ok": False,
                    "error": {
                        "code": "AUTH_FAILED",
                        "message": (
                            f"request for {token} failed; "
                            f'{secret_key}: \"very-secret-value\"'
                        ),
                    },
                },
                returncode=1,
            )
        ]
    )
    client = make_client(runner, token=token)

    with pytest.raises(RuntimeError) as caught:
        client.list_tables()

    message = str(caught.value)
    assert "exit=1" in message
    assert "AUTH_FAILED" in message
    assert token not in message
    assert "very-secret-value" not in message


def test_openapi_nonzero_code_raises_without_leaking_app_token():
    token = "bascn_private"
    runner = RecordingRunner(
        [
            completed(
                {
                    "ok": True,
                    "data": {
                        "code": 1254001,
                        "msg": f"invalid app token {token}",
                        "data": {},
                    },
                }
            )
        ]
    )
    client = make_client(runner, token=token)

    with pytest.raises(RuntimeError) as caught:
        client.list_tables()

    assert "1254001" in str(caught.value)
    assert token not in str(caught.value)


def test_timeout_error_does_not_include_the_command_or_token():
    runner = RecordingRunner(
        [subprocess.TimeoutExpired(cmd=["lark-cli", "secret-endpoint"], timeout=17)]
    )
    client = make_client(runner)

    with pytest.raises(RuntimeError, match="timed out after 17 seconds") as caught:
        client.list_tables()

    assert "bascn_sensitive_token" not in str(caught.value)
    assert "secret-endpoint" not in str(caught.value)


def test_invalid_json_has_a_sanitized_diagnostic():
    runner = RecordingRunner(
        [SimpleNamespace(returncode=0, stdout="not-json", stderr="device_code=secret123")]
    )
    client = make_client(runner)

    with pytest.raises(RuntimeError) as caught:
        client.list_tables()

    assert "invalid JSON" in str(caught.value)
    assert "secret123" not in str(caught.value)
