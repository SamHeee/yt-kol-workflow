from types import SimpleNamespace
from unittest.mock import ANY

import pytest

import main as workflow_main
from config import FeishuConfig, LogisticsConfig, WorkflowConfig
from feishu.client_factory import BaseTarget, load_base_target, save_base_target
from feishu.cooperation_content import COOPERATION_CONTENT_FIELDS
from feishu.full_schema import FULL_SCHEMA


def test_maintenance_commands_allow_omitting_base_target(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_TOKEN", raising=False)
    parser = workflow_main.build_parser()

    sync_args = parser.parse_args(["sync-workbook", "--workbook", "summary.xlsx"])
    clean_args = parser.parse_args(["clean-feishu-empty"])
    setup_args = parser.parse_args(["feishu-setup"])

    assert sync_args.feishu_app_token == ""
    assert clean_args.feishu_app_token == ""
    assert setup_args.feishu_app_token == ""
    assert setup_args.dry_run is False
    assert setup_args.collection_only is False
    assert setup_args.no_enable_automation is False
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
    assert "https://tenant.feishu.cn/…" in output
    assert "bascn_created" not in output


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


def test_setup_parser_exposes_read_only_plan_and_minimal_scope():
    args = workflow_main.build_parser().parse_args(
        [
            "feishu-setup",
            "--dry-run",
            "--collection-only",
            "--no-enable-automation",
        ]
    )

    assert args.dry_run is True
    assert args.collection_only is True
    assert args.no_enable_automation is True


def test_full_setup_dry_run_plans_without_preflight_writes(monkeypatch, capsys):
    parser = workflow_main.build_parser()
    args = parser.parse_args(
        [
            "feishu-setup",
            "--feishu-auth-mode",
            "cli",
            "--feishu-app-token",
            "bascn_test",
            "--dry-run",
        ]
    )
    context_calls = []
    fake_context = SimpleNamespace(
        app_token="bascn_test",
        auth_mode="cli",
        base_url="",
        cli_manager=SimpleNamespace(cli_path="lark-cli"),
        client=object(),
    )

    def fake_context_factory(_config, **kwargs):
        context_calls.append(kwargs)
        return fake_context

    monkeypatch.setattr(workflow_main, "_create_feishu_context", fake_context_factory)
    action = SimpleNamespace(kind="create_table", target="合作红人表", detail="20 个字段")
    monkeypatch.setattr(
        workflow_main,
        "_ensure_full_feishu_workspace",
        lambda _context, _config, **kwargs: (
            pytest.fail("unexpected arguments")
            if kwargs != {"dry_run": True, "enable_workflow": True}
            else SimpleNamespace(
                plan=SimpleNamespace(actions=[action], mutation_count=1),
                workflow_enabled=False,
            )
        ),
    )
    monkeypatch.setattr(
        workflow_main,
        "run_feishu_preflight",
        lambda **_kwargs: pytest.fail("dry-run must not write preflight records"),
    )

    workflow_main.handle_feishu_setup(args)

    assert context_calls == [{"create_base_if_missing": False}]
    output = capsys.readouterr().out
    assert "预计变更: 1 项" in output
    assert "合作红人表" in output


def test_full_setup_dry_run_without_target_is_offline_and_creates_nothing(
    monkeypatch,
    capsys,
):
    monkeypatch.delenv("FEISHU_APP_TOKEN", raising=False)
    args = workflow_main.build_parser().parse_args(
        ["feishu-setup", "--feishu-auth-mode", "cli", "--dry-run"]
    )
    monkeypatch.setattr("feishu.client_factory.load_base_target", lambda _profile: None)
    monkeypatch.setattr(
        workflow_main,
        "_create_feishu_context",
        lambda *_args, **_kwargs: pytest.fail("offline plan must not resolve/create Base"),
    )

    workflow_main.handle_feishu_setup(args)

    output = capsys.readouterr().out
    assert "离线计划" in output
    assert "7 张工作表" in output
    assert f"{FULL_SCHEMA.field_count} 个" in output
    assert "本次未创建" in output


def test_setup_target_is_explicit_or_profile_scoped_not_ambient_env(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_TOKEN", "bascn_unrelated_env")
    parser = workflow_main.build_parser()

    implicit = workflow_main._feishu_config_from_args(
        parser.parse_args(["feishu-setup", "--feishu-cli-profile", "new-profile"])
    )
    explicit = workflow_main._feishu_config_from_args(
        parser.parse_args(
            [
                "feishu-setup",
                "--feishu-cli-profile",
                "new-profile",
                "--feishu-app-token",
                "bascn_explicit",
            ]
        )
    )

    assert implicit.app_token == ""
    assert explicit.app_token == "bascn_explicit"


def test_full_setup_rejects_legacy_app_mode_before_provisioning(monkeypatch):
    args = workflow_main.build_parser().parse_args(
        [
            "feishu-setup",
            "--feishu-auth-mode",
            "app",
            "--feishu-app-token",
            "bascn_test",
        ]
    )
    fake_context = SimpleNamespace(
        app_token="bascn_test",
        auth_mode="app",
        base_url="",
        client=object(),
    )
    monkeypatch.setattr(
        workflow_main,
        "_create_feishu_context",
        lambda *_args, **_kwargs: fake_context,
    )

    with pytest.raises(RuntimeError, match="仅支持 FEISHU_AUTH_MODE=cli"):
        workflow_main.handle_feishu_setup(args)


def test_full_workspace_helper_skips_legacy_app_mode(monkeypatch):
    context = SimpleNamespace(auth_mode="app")
    monkeypatch.setattr(
        "feishu.lark_cli_base.LarkCliBaseRunner",
        lambda **_kwargs: pytest.fail("app mode must not start CLI provisioner"),
    )

    assert workflow_main._ensure_full_feishu_workspace(
        context,
        FeishuConfig(auth_mode="app"),
    ) is None


def test_full_workspace_helper_requests_workflow_scopes_before_writes(monkeypatch):
    calls = []
    target_store = None
    manager = SimpleNamespace(
        cli_path="lark-cli",
        ensure_full_workspace_scopes=lambda: calls.append("scopes"),
    )
    context = SimpleNamespace(
        auth_mode="cli",
        app_token="bascn_test",
        cli_manager=manager,
        client="record-client",
        pending_view_tables=(),
    )

    class FakeAdmin:
        def __init__(self, **_kwargs):
            pass

        def probe_capabilities(self):
            calls.append("probe")
            return SimpleNamespace(supported=True, missing_commands=())

    class FakeProvisioner:
        def __init__(self, _admin):
            pass

        def apply(self, **kwargs):
            calls.append(("apply", kwargs))
            return SimpleNamespace(workflow_id="wkf-test", workflow_enabled=False)

        def ensure_workflow_enabled(self):
            calls.append("enable")
            return "wkf-test", True

    monkeypatch.setattr("feishu.lark_cli_base.LarkCliBaseRunner", FakeAdmin)
    monkeypatch.setattr("feishu.provisioning.SevenTableProvisioner", FakeProvisioner)
    monkeypatch.setattr(
        workflow_main,
        "run_feishu_preflight",
        lambda **kwargs: (
            calls.append(("preflight", kwargs["client"]))
            or (kwargs["client"], "schema-manager")
        ),
    )

    result = workflow_main._ensure_full_feishu_workspace(
        context,
        FeishuConfig(auth_mode="cli", auto_setup=True),
    )

    assert calls == [
        "scopes",
        "probe",
            (
                "apply",
                {
                    "dry_run": False,
                    "enable_workflow": False,
                    "initialize_view_tables": (),
                    "journal_pending_views": ANY,
                },
            ),
        ("preflight", "record-client"),
        "enable",
    ]
    assert result.preflight_client == "record-client"
    assert result.schema_manager == "schema-manager"
    assert result.workflow_enabled is True


def test_full_workspace_preflight_failure_never_reaches_workflow_enable(monkeypatch):
    calls = []
    manager = SimpleNamespace(
        cli_path="lark-cli",
        ensure_full_workspace_scopes=lambda: None,
    )
    context = SimpleNamespace(
        auth_mode="cli",
        app_token="bascn_test",
        cli_manager=manager,
        client=object(),
        pending_view_tables=(),
    )

    class FakeAdmin:
        def __init__(self, **_kwargs):
            pass

        def probe_capabilities(self):
            return SimpleNamespace(supported=True, missing_commands=())

        def disable_workflow(self, workflow_id):
            calls.append(("disable", workflow_id))

    class FakeProvisioner:
        def __init__(self, _admin):
            pass

        def apply(self, **_kwargs):
            return SimpleNamespace(workflow_id="wkf-test", workflow_enabled=False)

    monkeypatch.setattr("feishu.lark_cli_base.LarkCliBaseRunner", FakeAdmin)
    monkeypatch.setattr("feishu.provisioning.SevenTableProvisioner", FakeProvisioner)
    monkeypatch.setattr(
        workflow_main,
        "run_feishu_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("record check failed")),
    )

    with pytest.raises(RuntimeError, match="record check failed"):
        workflow_main._ensure_full_feishu_workspace(
            context,
            FeishuConfig(auth_mode="cli", auto_setup=True),
        )

    assert calls == []


def test_existing_enabled_workflow_is_not_enabled_again(monkeypatch):
    calls = []
    manager = SimpleNamespace(
        cli_path="lark-cli",
        ensure_full_workspace_scopes=lambda: None,
    )
    context = SimpleNamespace(
        auth_mode="cli",
        app_token="bascn_test",
        cli_manager=manager,
        client="record-client",
        pending_view_tables=(),
    )

    class FakeAdmin:
        def __init__(self, **_kwargs):
            pass

        def probe_capabilities(self):
            return SimpleNamespace(supported=True, missing_commands=())

    class FakeProvisioner:
        def __init__(self, _admin):
            pass

        def apply(self, **_kwargs):
            return SimpleNamespace(workflow_id="wkf-existing", workflow_enabled=True)

        def ensure_workflow_enabled(self):
            calls.append("unexpected-enable")
            return "wkf-existing", True

    monkeypatch.setattr("feishu.lark_cli_base.LarkCliBaseRunner", FakeAdmin)
    monkeypatch.setattr("feishu.provisioning.SevenTableProvisioner", FakeProvisioner)
    monkeypatch.setattr(
        workflow_main,
        "run_feishu_preflight",
        lambda **kwargs: (kwargs["client"], "schema-manager"),
    )

    result = workflow_main._ensure_full_feishu_workspace(
        context,
        FeishuConfig(auth_mode="cli", auto_setup=True),
    )

    assert result.workflow_enabled is True
    assert calls == []


def test_full_workspace_success_clears_pending_view_journal(monkeypatch, tmp_path):
    target_store = tmp_path / "targets.json"
    save_base_target(
        "kol-test",
        BaseTarget(
            app_token="bascn_test",
            name="KOL Test",
            created_at="2026-08-12T00:00:00+00:00",
            pending_view_tables=("搜索任务表", "合作红人表"),
        ),
        target_store,
    )
    calls = []
    manager = SimpleNamespace(
        cli_path="lark-cli",
        ensure_full_workspace_scopes=lambda: None,
    )
    context = SimpleNamespace(
        auth_mode="cli",
        app_token="bascn_test",
        base_url="",
        base_name="KOL Test",
        cli_manager=manager,
        client="record-client",
        pending_view_tables=("搜索任务表", "合作红人表"),
        target_store=target_store,
    )

    class FakeAdmin:
        def __init__(self, **_kwargs):
            pass

        def probe_capabilities(self):
            return SimpleNamespace(supported=True, missing_commands=())

    class FakeProvisioner:
        def __init__(self, _admin):
            pass

        def apply(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                workflow_id="wkf-test",
                workflow_enabled=False,
                initialized_view_tables=["搜索任务表"],
                pending_view_tables=["搜索任务表", "合作红人表"],
            )

        def ensure_workflow_enabled(self):
            return "wkf-test", True

    monkeypatch.setattr("feishu.lark_cli_base.LarkCliBaseRunner", FakeAdmin)
    monkeypatch.setattr("feishu.provisioning.SevenTableProvisioner", FakeProvisioner)
    monkeypatch.setattr(
        workflow_main,
        "run_feishu_preflight",
        lambda **kwargs: (kwargs["client"], "schema-manager"),
    )

    workflow_main._ensure_full_feishu_workspace(
        context,
        FeishuConfig(auth_mode="cli", cli_profile="kol-test", auto_setup=True),
    )

    assert calls[0]["initialize_view_tables"] == (
        "搜索任务表",
        "合作红人表",
    )
    assert context.pending_view_tables == ("合作红人表",)
    stored = load_base_target("kol-test", target_store)
    assert stored.pending_view_tables == ("合作红人表",)
    assert stored.created_at == "2026-08-12T00:00:00+00:00"


def test_refresh_cooperation_content_parser_exposes_safe_maintenance_options(monkeypatch):
    monkeypatch.setenv("FEISHU_AUTH_MODE", "cli")
    parser = workflow_main.build_parser()

    args = parser.parse_args([
        "refresh-cooperation-content",
        "--content-record-id",
        "CNT-000001",
        "--max-age-hours",
        "12",
        "--dry-run",
    ])

    assert args.feishu_auth_mode == "cli"
    assert args.content_record_id == ["CNT-000001"]
    assert args.max_age_hours == 12
    assert args.dry_run is True
    assert args.force is False


def test_logistics_config_reads_only_environment_secret(monkeypatch):
    monkeypatch.setenv("TRACK17_API_KEY", "test-17track-key")
    monkeypatch.setenv("SAMPLE_REMINDER_USER_OPEN_ID", "ou_test_recipient")
    monkeypatch.setenv("SAMPLE_LOGISTICS_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("SAMPLE_LOGISTICS_STALL_HOURS", "72")
    monkeypatch.setenv("LOGISTICS_HTTP_TIMEOUT_SECONDS", "25")
    monkeypatch.setenv("LOGISTICS_MAX_RETRIES", "3")

    config = LogisticsConfig()

    assert config.provider == "17track"
    assert config.api_key == "test-17track-key"
    assert config.reminder_receive_id == "ou_test_recipient"
    assert config.timezone_name == "Asia/Shanghai"
    assert config.stall_hours == 72
    assert config.timeout_seconds == 25
    assert config.max_retries == 3
    assert config.validate(require_api_key=True, require_recipient=True) == []


def test_logistics_config_rejects_timezone_not_used_by_daily_job(monkeypatch):
    monkeypatch.setenv("SAMPLE_LOGISTICS_TIMEZONE", "UTC")

    with pytest.raises(ValueError, match="必须是 Asia/Shanghai"):
        LogisticsConfig()


def test_sample_logistics_parsers_expose_safe_maintenance_options(monkeypatch):
    monkeypatch.setenv("FEISHU_AUTH_MODE", "cli")
    parser = workflow_main.build_parser()

    refresh = parser.parse_args([
        "refresh-sample-logistics",
        "--sample-record-id",
        "SMP-000001",
        "--max-age-hours",
        "18",
        "--force",
        "--dry-run",
    ])
    reminders = parser.parse_args([
        "dispatch-sample-reminders",
        "--sample-record-id",
        "SMP-000001",
        "--limit",
        "5",
        "--dry-run",
    ])

    assert refresh.feishu_auth_mode == "cli"
    assert refresh.sample_record_id == ["SMP-000001"]
    assert refresh.max_age_hours == 18
    assert refresh.force is True
    assert refresh.dry_run is True
    assert not hasattr(refresh, "tracking_api_key")
    assert reminders.sample_record_id == ["SMP-000001"]
    assert reminders.limit == 5
    assert reminders.dry_run is True


def test_refresh_cooperation_content_rejects_app_transport_before_connecting(
    monkeypatch,
):
    parser = workflow_main.build_parser()
    args = parser.parse_args([
        "refresh-cooperation-content",
        "--feishu-auth-mode",
        "app",
        "--youtube-api-key",
        "test-key",
    ])
    monkeypatch.setattr(
        workflow_main,
        "_create_feishu_context",
        lambda *_args, **_kwargs: pytest.fail("must not connect"),
    )

    with pytest.raises(RuntimeError, match="仅允许 FEISHU_AUTH_MODE=cli"):
        workflow_main.handle_refresh_cooperation_content(args)


def test_refresh_cooperation_content_handler_uses_cli_without_creating_base(
    monkeypatch,
    tmp_path,
):
    parser = workflow_main.build_parser()
    stats_path = tmp_path / "content_stats.json"
    args = parser.parse_args([
        "refresh-cooperation-content",
        "--feishu-auth-mode",
        "cli",
        "--youtube-api-key",
        "test-key",
        "--dry-run",
        "--stats",
        str(stats_path),
    ])

    class FakeClient:
        def find_table_by_name(self, name):
            return {
                "合作内容数据表": "tbl_content",
                "合作红人表": "tbl_cooperation",
                "样品跟踪表": "tbl_sample",
            }.get(name)

        def list_fields(self, table_id):
            if table_id != "tbl_content":
                return [{"field_name": "source", "type": 1}]
            return [
                {"field_name": name, "type": field_type}
                for name, field_type in COOPERATION_CONTENT_FIELDS
            ]

    context_calls = []

    def fake_context(_config, **kwargs):
        context_calls.append(kwargs)
        return SimpleNamespace(auth_mode="cli", client=FakeClient())

    workflow_calls = []

    def fake_refresh(**kwargs):
        workflow_calls.append(kwargs)
        return {
            "source_records": 0,
            "selected_records": 0,
            "candidate_records": 0,
            "requested_video_ids": 0,
            "api_batches": 0,
            "estimated_quota": 0,
            "planned_updates": 0,
            "actual_updates": 0,
            "dry_run": True,
            "force": False,
            "stale_hours": 24,
            "partial_metric_records": 0,
            "concurrent_skips": 0,
            "missing_before_write": 0,
            "write_fallback_used": False,
            "write_failures": 0,
            "batch_write_error": "",
            "status_counts": {},
            "skipped_counts": {},
            "errors": [],
            "samples": [],
        }

    monkeypatch.setattr(workflow_main, "_create_feishu_context", fake_context)
    metadata_calls = []
    monkeypatch.setattr(
        "feishu.cooperation_content.validate_cooperation_content_metadata",
        lambda fields, **kwargs: metadata_calls.append((fields, kwargs)),
    )
    monkeypatch.setattr(
        "workflow.refresh_cooperation_content.refresh_cooperation_content",
        fake_refresh,
    )

    workflow_main.handle_refresh_cooperation_content(args)

    assert context_calls == [{"create_base_if_missing": False}]
    assert workflow_calls[0]["table_id"] == "tbl_content"
    assert workflow_calls[0]["dry_run"] is True
    assert metadata_calls[0][1]["cooperation_table_id"] == "tbl_cooperation"
    assert metadata_calls[0][1]["sample_table_id"] == "tbl_sample"
    assert metadata_calls[0][1]["strict_order"] is False
    assert stats_path.exists()


def test_refresh_sample_logistics_rejects_app_transport_before_connecting(
    monkeypatch,
):
    parser = workflow_main.build_parser()
    args = parser.parse_args([
        "refresh-sample-logistics",
        "--feishu-auth-mode",
        "app",
        "--dry-run",
    ])
    monkeypatch.setattr(
        workflow_main,
        "_create_feishu_context",
        lambda *_args, **_kwargs: pytest.fail("must not connect"),
    )

    with pytest.raises(RuntimeError, match="仅允许 FEISHU_AUTH_MODE=cli"):
        workflow_main.handle_refresh_sample_logistics(args)


def test_refresh_sample_logistics_dry_run_uses_cli_without_api_key_or_writes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRACK17_API_KEY", raising=False)
    monkeypatch.delenv("SAMPLE_TRACKING_TABLE_ID", raising=False)
    parser = workflow_main.build_parser()
    stats_path = tmp_path / "sample_logistics_stats.json"
    args = parser.parse_args([
        "refresh-sample-logistics",
        "--feishu-auth-mode",
        "cli",
        "--dry-run",
        "--stats",
        str(stats_path),
    ])

    class FakeClient:
        def find_table_by_name(self, name):
            assert name == "样品跟踪表"
            return "tbl_sample"

        def list_fields(self, table_id):
            assert table_id == "tbl_sample"
            return [{"field_name": "多行文本", "type": 1, "is_primary": True}]

    context_calls = []

    def fake_context(_config, **kwargs):
        context_calls.append(kwargs)
        return SimpleNamespace(auth_mode="cli", client=FakeClient())

    metadata_calls = []
    workflow_calls = []

    def fake_refresh(**kwargs):
        workflow_calls.append(kwargs)
        return {
            "source_records": 1,
            "selected_records": 1,
            "candidate_records": 1,
            "unique_shipments": 1,
            "registration_shipments": 1,
            "provider_requests": 0,
            "planned_updates": 1,
            "actual_updates": 0,
            "dry_run": True,
            "force": False,
            "stale_hours": 20,
            "concurrent_skips": 0,
            "missing_before_write": 0,
            "write_fallback_used": False,
            "write_failures": 0,
            "batch_write_error": "",
            "status_counts": {},
            "sync_status_counts": {},
            "skipped_counts": {},
            "delivery_events": [],
            "stagnant_candidates": [],
            "errors": [],
            "samples": [],
        }

    monkeypatch.setattr(workflow_main, "_create_feishu_context", fake_context)
    monkeypatch.setattr(
        "feishu.sample_logistics.validate_sample_logistics_metadata",
        lambda fields, **kwargs: metadata_calls.append((fields, kwargs)),
    )
    monkeypatch.setattr(
        "workflow.refresh_sample_logistics.refresh_sample_logistics",
        fake_refresh,
    )

    workflow_main.handle_refresh_sample_logistics(args)

    assert context_calls == [{"create_base_if_missing": False}]
    assert metadata_calls[0][1] == {"strict_order": False}
    assert workflow_calls[0]["table_id"] == "tbl_sample"
    assert workflow_calls[0]["dry_run"] is True
    assert not hasattr(workflow_calls[0]["provider"], "refresh")
    assert stats_path.exists()


def test_dispatch_sample_reminders_rejects_app_transport_before_connecting(
    monkeypatch,
):
    parser = workflow_main.build_parser()
    args = parser.parse_args([
        "dispatch-sample-reminders",
        "--feishu-auth-mode",
        "app",
        "--dry-run",
    ])
    monkeypatch.setattr(
        workflow_main,
        "_create_feishu_context",
        lambda *_args, **_kwargs: pytest.fail("must not connect"),
    )

    with pytest.raises(RuntimeError, match="仅允许 FEISHU_AUTH_MODE=cli"):
        workflow_main.handle_dispatch_sample_reminders(args)


def test_dispatch_sample_reminders_locks_current_cli_user_and_writes_stats(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SAMPLE_REMINDER_USER_OPEN_ID", "ou_current_user")
    monkeypatch.delenv("SAMPLE_TRACKING_TABLE_ID", raising=False)
    parser = workflow_main.build_parser()
    stats_path = tmp_path / "sample_reminder_stats.json"
    args = parser.parse_args([
        "dispatch-sample-reminders",
        "--feishu-auth-mode",
        "cli",
        "--sample-record-id",
        "SMP-000001",
        "--limit",
        "2",
        "--stats",
        str(stats_path),
    ])

    class FakeClient:
        def find_table_by_name(self, name):
            assert name == "样品跟踪表"
            return "tbl_sample"

        def list_fields(self, table_id):
            assert table_id == "tbl_sample"
            return [{"field_name": "多行文本", "type": 1, "is_primary": True}]

    fake_context = SimpleNamespace(
        auth_mode="cli",
        client=FakeClient(),
        base_url="https://tenant.feishu.cn/wiki/example",
        cli_manager=SimpleNamespace(cli_path="lark-cli.cmd"),
    )
    monkeypatch.setattr(
        workflow_main,
        "_create_feishu_context",
        lambda _config, **_kwargs: fake_context,
    )
    monkeypatch.setattr(
        "feishu.sample_logistics.validate_sample_logistics_metadata",
        lambda _fields, **_kwargs: None,
    )

    notifier_calls = []

    class FakeNotifier:
        def __init__(self, **kwargs):
            notifier_calls.append(("init", kwargs))

        def assert_current_user(self, user_id):
            notifier_calls.append(("lock", user_id))
            return user_id

    workflow_calls = []

    def fake_dispatch(**kwargs):
        workflow_calls.append(kwargs)
        return {
            "dry_run": False,
            "planned_events": 1,
            "planned_delivery_reminders": 1,
            "planned_stagnation_reminders": 0,
            "sent": 1,
            "messages_sent": 1,
            "skipped_counts": {},
            "event_keys": ["delivery-test"],
        }

    monkeypatch.setattr("notifications.feishu_cli.FeishuCliNotifier", FakeNotifier)
    monkeypatch.setattr(
        "workflow.sample_reminders.dispatch_sample_reminders",
        fake_dispatch,
    )

    workflow_main.handle_dispatch_sample_reminders(args)

    assert ("lock", "ou_current_user") in notifier_calls
    assert workflow_calls[0]["recipient_user_id"] == "ou_current_user"
    assert workflow_calls[0]["base_url"] == fake_context.base_url
    assert workflow_calls[0]["sample_ids"] == ["SMP-000001"]
    assert workflow_calls[0]["limit"] == 2
    assert stats_path.exists()
