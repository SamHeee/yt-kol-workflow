import io
import json
import subprocess

import pytest

from feishu.lark_cli import (
    BrowserLaunchError,
    LarkCliCommandError,
    LarkCliManager,
    LarkCliRuntimeError,
    LarkCliTimeoutError,
    UnsafeAuthorizationUrlError,
)


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class FinishedProcess:
    def __init__(self, output, returncode=0):
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class HangingProcess(FinishedProcess):
    def __init__(self):
        super().__init__("")
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("redacted", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def test_detect_runtime_is_read_only_and_cross_platform_names_are_resolved():
    paths = {
        "node": "/runtime/node",
        "npx": "/runtime/npx",
        "lark-cli": "/runtime/lark-cli",
    }
    manager = LarkCliManager(which=paths.get)

    status = manager.detect_runtime()

    assert status.node == "/runtime/node"
    assert status.npx == "/runtime/npx"
    assert status.lark_cli == "/runtime/lark-cli"


def test_ensure_cli_installs_exact_official_package_when_missing():
    installed = False
    calls = []

    def which(name):
        if name == "node":
            return "/bin/node"
        if name == "npx":
            return "/bin/npx"
        if name == "lark-cli" and installed:
            return "/bin/lark-cli"
        return None

    def runner(args, **kwargs):
        nonlocal installed
        calls.append((args, kwargs))
        assert args == [
            "/bin/npx",
            "--yes",
            "@larksuite/cli@latest",
            "install",
        ]
        assert kwargs["shell"] is False
        installed = True
        return completed(args)

    manager = LarkCliManager(which=which, runner=runner)

    cli_path, installed_now = manager.ensure_cli()

    assert cli_path == "/bin/lark-cli"
    assert installed_now is True
    assert len(calls) == 1


def test_ensure_cli_reports_missing_node_and_npx_without_running_installer():
    manager = LarkCliManager(which=lambda _name: None)

    with pytest.raises(LarkCliRuntimeError, match="Node.js, npx"):
        manager.ensure_cli()


def test_ensure_cli_respects_disabled_auto_install():
    manager = LarkCliManager(
        which=lambda _name: None,
        auto_install=False,
    )

    with pytest.raises(LarkCliRuntimeError, match="automatic installation is disabled"):
        manager.ensure_cli()


def test_ensure_ready_configures_profile_opens_both_pages_and_authorizes():
    opened = []
    progress = []
    config_checks = 0
    auth_checks = 0
    calls = []
    secret_device_code = "device-code-that-must-not-be-logged"

    def runner(args, **kwargs):
        nonlocal config_checks, auth_checks
        calls.append((args, kwargs))
        assert kwargs["shell"] is False
        if args[-2:] == ["config", "show"]:
            config_checks += 1
            if config_checks == 1:
                return completed(args, 1, '{"ok":false}')
            return completed(args, 0, '{"ok":true}')
        if args[-3:] == ["auth", "status", "--verify"]:
            auth_checks += 1
            if auth_checks == 1:
                return completed(args, 1, '{"ok":false}')
            return completed(args, 0, '{"ok":true,"tokenStatus":"valid"}')
        if "--no-wait" in args:
            return completed(
                args,
                0,
                json.dumps(
                    {
                        "verification_url": "https://accounts.feishu.cn/device",
                        "device_code": secret_device_code,
                        "expires_in": 600,
                    }
                ),
            )
        if "--device-code" in args:
            assert args[args.index("--device-code") + 1] == secret_device_code
            return completed(args, 0, '{"ok":true}')
        raise AssertionError(f"unexpected command shape: {args}")

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs["shell"] is False
        assert kwargs["stderr"] == subprocess.STDOUT
        assert args == [
            "/bin/lark-cli",
            "config",
            "init",
            "--new",
            "--name",
            "kol-workflow",
            "--brand",
            "feishu",
            "--lang",
            "zh",
        ]
        return FinishedProcess(
            "请访问 https://open.feishu.cn/cli/setup?request=temporary 完成配置\n"
        )

    manager = LarkCliManager(
        which=lambda name: "/bin/lark-cli" if name == "lark-cli" else None,
        runner=runner,
        popen_factory=popen,
        open_browser=lambda url: opened.append(url) or True,
        progress=progress.append,
    )

    result = manager.ensure_ready()

    assert result.profile == "kol-workflow"
    assert result.installed is False
    assert result.configured is True
    assert result.authorized is True
    assert opened == [
        "https://open.feishu.cn/cli/setup?request=temporary",
        "https://accounts.feishu.cn/device",
    ]
    assert all(secret_device_code not in message for message in progress)
    no_wait_args = next(args for args, _ in calls if "--no-wait" in args)
    assert no_wait_args[-6:] == [
        "login",
        "--domain",
        "base",
        "--recommend",
        "--no-wait",
        "--json",
    ]


def test_valid_profile_and_authorization_skip_browser_flows():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[-2:] == ["config", "show"]:
            return completed(args, stdout='{"ok":true}')
        if args[-3:] == ["auth", "status", "--verify"]:
            return completed(args, stdout='{"ok":true,"tokenStatus":"valid"}')
        raise AssertionError(args)

    manager = LarkCliManager(
        cli_path="/bin/lark-cli",
        path_exists=lambda path: path == "/bin/lark-cli",
        runner=runner,
        popen_factory=lambda *_args, **_kwargs: pytest.fail("must not configure"),
        open_browser=lambda _url: pytest.fail("must not open browser"),
    )

    result = manager.ensure_ready()

    assert result.configured is False
    assert result.authorized is True
    assert sum(command[-3:] == ["auth", "status", "--verify"] for command in calls) == 2


def test_nested_verified_false_is_not_treated_as_authorized():
    manager = LarkCliManager(
        cli_path="/bin/lark-cli",
        path_exists=lambda _path: True,
        runner=lambda args, **_kwargs: completed(
            args,
            stdout='{"ok":true,"data":{"verified":false}}',
        ),
    )

    assert manager.authorization_valid() is False


def test_config_never_opens_untrusted_url():
    opened = []
    process = FinishedProcess("Continue at https://attacker.example/steal\n")
    manager = LarkCliManager(
        cli_path="/bin/lark-cli",
        path_exists=lambda _path: True,
        popen_factory=lambda *_args, **_kwargs: process,
        open_browser=lambda url: opened.append(url) or True,
    )

    with pytest.raises(UnsafeAuthorizationUrlError, match="official"):
        manager._run_streaming_browser_command(
            ["/bin/lark-cli", "config", "init"],
            label="configure CLI profile",
            timeout=1,
        )

    assert opened == []


def test_config_timeout_terminates_child_process():
    process = HangingProcess()
    ticks = iter((0.0, 2.0, 2.0, 2.0))
    manager = LarkCliManager(
        cli_path="/bin/lark-cli",
        timeout=1,
        path_exists=lambda _path: True,
        popen_factory=lambda *_args, **_kwargs: process,
        monotonic=lambda: next(ticks, 2.0),
    )

    with pytest.raises(LarkCliTimeoutError, match="Timed out"):
        manager._run_streaming_browser_command(
            ["/bin/lark-cli", "config", "init"],
            label="configure CLI profile",
            timeout=1,
        )

    assert process.terminated is True


def test_auth_rejects_untrusted_verification_url_before_device_poll():
    device_code = "do-not-print-this-code"
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if "--no-wait" in args:
            return completed(
                args,
                stdout=json.dumps(
                    {
                        "verification_url": "https://example.com/oauth",
                        "device_code": device_code,
                    }
                ),
            )
        raise AssertionError("device polling must not start")

    manager = LarkCliManager(
        cli_path="/bin/lark-cli",
        path_exists=lambda _path: True,
        runner=runner,
        open_browser=lambda _url: True,
    )

    with pytest.raises(UnsafeAuthorizationUrlError, match="official") as error:
        manager._authorize("/bin/lark-cli")

    assert device_code not in str(error.value)
    assert not any("--device-code" in command for command in calls)


def test_disabled_browser_presents_clickable_url_and_continues():
    progress = []
    url = "https://open.feishu.cn/authorization?temporary=user-code"
    manager = LarkCliManager(open_browser=False, progress=progress.append)

    manager._open_authorization_url(url)

    assert progress == [f"请在浏览器打开飞书授权页面：{url}"]


def test_cli_path_property_is_available_to_client_factory():
    manager = LarkCliManager(which=lambda name: "/bin/lark-cli" if name == "lark-cli" else None)

    assert manager.cli_path == "/bin/lark-cli"


def test_create_base_returns_stable_token_and_url_shape():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[-2:] == ["config", "show"]:
            return completed(args, stdout='{"ok":true}')
        if args[-3:] == ["auth", "status", "--verify"]:
            return completed(args, stdout='{"ok":true,"tokenStatus":"valid"}')
        if "+base-create" in args:
            return completed(
                args,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "base": {
                                "name": "KOL 数据库",
                                "token": "bascnCreatedToken",
                                "url": "https://acme.feishu.cn/base/bascnCreatedToken",
                            }
                        },
                    }
                ),
            )
        raise AssertionError(args)

    manager = LarkCliManager(
        cli_path="/bin/lark-cli",
        path_exists=lambda _path: True,
        runner=runner,
    )

    created = manager.create_base("KOL 数据库")

    assert created == {
        "name": "KOL 数据库",
        "app_token": "bascnCreatedToken",
        "base_url": "https://acme.feishu.cn/base/bascnCreatedToken",
    }
    command = next(command for command in calls if "+base-create" in command)
    assert command == [
        "/bin/lark-cli",
        "--profile",
        "kol-workflow",
        "base",
        "+base-create",
        "--as",
        "user",
        "--name",
        "KOL 数据库",
        "--time-zone",
        "Asia/Shanghai",
    ]


def test_create_base_builds_canonical_url_when_cli_only_returns_token():
    def runner(args, **kwargs):
        if args[-2:] == ["config", "show"]:
            return completed(args, stdout='{"ok":true}')
        if args[-3:] == ["auth", "status", "--verify"]:
            return completed(args, stdout='{"ok":true}')
        if "+base-create" in args:
            return completed(args, stdout='{"data":{"base":{"token":"bascnOnly"}}}')
        raise AssertionError(args)

    manager = LarkCliManager(
        cli_path="/bin/lark-cli",
        path_exists=lambda _path: True,
        runner=runner,
    )

    created = manager.create_base("KOL Workflow")

    assert created["app_token"] == "bascnOnly"
    assert created["base_url"] == "https://www.feishu.cn/base/bascnOnly"


def test_create_base_requires_token_but_does_not_echo_raw_cli_output():
    sensitive = "sensitive-response-content"

    def runner(args, **kwargs):
        if args[-2:] == ["config", "show"]:
            return completed(args, stdout='{"ok":true}')
        if args[-3:] == ["auth", "status", "--verify"]:
            return completed(args, stdout='{"ok":true}')
        return completed(args, stdout=json.dumps({"message": sensitive}))

    manager = LarkCliManager(
        cli_path="/bin/lark-cli",
        path_exists=lambda _path: True,
        runner=runner,
    )

    with pytest.raises(LarkCliCommandError, match="app token") as error:
        manager.create_base("KOL Workflow")

    assert sensitive not in str(error.value)
