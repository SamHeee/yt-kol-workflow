from types import SimpleNamespace

import main as workflow_main
from config import FeishuConfig, WorkflowConfig


def test_maintenance_commands_allow_omitting_base_target(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_TOKEN", raising=False)
    parser = workflow_main.build_parser()

    sync_args = parser.parse_args(["sync-workbook", "--workbook", "summary.xlsx"])
    clean_args = parser.parse_args(["clean-feishu-empty"])
    setup_args = parser.parse_args(["feishu-setup"])

    assert sync_args.feishu_app_token == ""
    assert clean_args.feishu_app_token == ""
    assert setup_args.feishu_app_token == ""
    assert sync_args.feishu_auth_mode in {"auto", "cli", "app"}


def test_auto_and_cli_validation_do_not_require_static_credentials(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_APP_TOKEN", raising=False)

    auto = WorkflowConfig(feishu=FeishuConfig(auth_mode="auto"))
    cli = WorkflowConfig(feishu=FeishuConfig(auth_mode="cli"))

    assert auto.validate(require_youtube=False, require_feishu=True) == []
    assert cli.validate(require_youtube=False, require_feishu=True) == []


def test_app_validation_still_requires_all_three_values(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_APP_TOKEN", raising=False)
    config = WorkflowConfig(feishu=FeishuConfig(auth_mode="app"))

    errors = config.validate(require_youtube=False, require_feishu=True)

    assert len(errors) == 3
    assert any("FEISHU_APP_ID" in error for error in errors)
    assert any("FEISHU_APP_SECRET" in error for error in errors)
    assert any("FEISHU_APP_TOKEN" in error for error in errors)


def test_create_context_updates_config_and_reports_created_base(monkeypatch, capsys):
    fake_context = SimpleNamespace(
        app_token="bascn_created",
        auth_mode="cli",
        base_url="https://tenant.feishu.cn/base/bascn_created",
        base_name="KOL网红开发工作流",
        created_base=True,
        client=object(),
    )
    calls = []

    def fake_factory(config, **kwargs):
        calls.append((config, kwargs))
        return fake_context

    monkeypatch.setattr(workflow_main, "create_bitable_client_from_config", fake_factory)
    config = FeishuConfig(auth_mode="cli")

    result = workflow_main._create_feishu_context(config)

    assert result is fake_context
    assert config.app_token == "bascn_created"
    assert calls[0][1]["create_base_if_missing"] is True
    assert callable(calls[0][1]["progress"])
    output = capsys.readouterr().out
    assert "已自动创建多维表格" in output
    assert fake_context.base_url in output


def test_new_base_schema_is_initialized_only_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        workflow_main,
        "initialize_created_base_schema",
        lambda context: calls.append(context),
    )
    client = object()
    context = SimpleNamespace(created_base=True, client=client)

    workflow_main._ensure_created_base_schema(context)

    assert calls == [context]
