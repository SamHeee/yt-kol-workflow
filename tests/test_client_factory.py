from types import SimpleNamespace

import pytest

from feishu.client_factory import (
    FeishuClientContext,
    create_bitable_client,
    initialize_created_base_schema,
    load_base_target,
)


class FakeManager:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cli_path = "resolved-lark-cli"
        self.ready_calls = 0
        self.create_calls = []
        self.ensure_cli_calls = 0
        self.configured = True
        self.authorized = True
        self.__class__.instances.append(self)

    def ensure_ready(self):
        self.ready_calls += 1
        return SimpleNamespace(cli_path=self.cli_path)

    def ensure_cli(self):
        self.ensure_cli_calls += 1
        return self.cli_path, False

    def profile_configured(self):
        return self.configured

    def authorization_valid(self):
        return self.authorized

    def create_base(self, name):
        self.create_calls.append(name)
        return {
            "name": name,
            "app_token": "bascn_created",
            "base_url": "https://example.feishu.cn/base/bascn_created",
        }


class FakeCliClient:
    def __init__(self, app_token, **kwargs):
        self.app_token = app_token
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def clear_fake_managers():
    FakeManager.instances.clear()


def cli_context(tmp_path, **kwargs):
    return create_bitable_client(
        auth_mode="cli",
        target_store=tmp_path / "targets.json",
        manager_factory=FakeManager,
        cli_client_class=FakeCliClient,
        **kwargs,
    )


def test_cli_mode_uses_supplied_target_and_named_profile(tmp_path):
    context = cli_context(
        tmp_path,
        app_token="https://tenant.feishu.cn/base/bascn_existing?table=tbl1",
        cli_profile="kol-test",
        progress=lambda _: None,
    )

    assert context.app_token == "bascn_existing"
    assert context.auth_mode == "cli"
    assert context.created_base is False
    assert context.client.kwargs["cli_path"] == "resolved-lark-cli"
    assert context.client.kwargs["profile"] == "kol-test"
    assert FakeManager.instances[0].ready_calls == 1
    assert FakeManager.instances[0].create_calls == []
    stored = load_base_target("kol-test", tmp_path / "targets.json")
    assert stored.app_token == "bascn_existing"


def test_missing_target_creates_and_persists_base_then_reuses_it(tmp_path):
    first = cli_context(tmp_path, base_name="KOL测试库")

    assert first.created_base is True
    assert first.app_token == "bascn_created"
    assert first.base_url.endswith("/base/bascn_created")
    assert FakeManager.instances[0].create_calls == ["KOL测试库"]

    second = cli_context(tmp_path, base_name="不应再次创建")
    assert second.created_base is False
    assert second.app_token == "bascn_created"
    assert FakeManager.instances[1].create_calls == []


def test_missing_target_can_be_forbidden(tmp_path):
    with pytest.raises(RuntimeError, match="禁止自动创建"):
        cli_context(tmp_path, create_base_if_missing=False)


def test_auto_mode_preserves_legacy_app_credentials_when_complete(tmp_path):
    context = create_bitable_client(
        app_token="bascn_existing",
        app_id="cli_app",
        app_secret="secret",
        auth_mode="auto",
        target_store=tmp_path / "targets.json",
        manager_factory=FakeManager,
        cli_client_class=FakeCliClient,
    )

    assert context.auth_mode == "app"
    assert context.app_token == "bascn_existing"
    assert FakeManager.instances == []


def test_no_setup_only_verifies_existing_cli_state(tmp_path):
    context = cli_context(
        tmp_path,
        app_token="bascn_existing",
        auto_setup=False,
        auto_install=False,
    )

    manager = FakeManager.instances[0]
    assert manager.ready_calls == 0
    assert manager.ensure_cli_calls == 1
    assert context.app_token == "bascn_existing"


def test_invalid_auth_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="auto、cli 或 app"):
        create_bitable_client(
            auth_mode="unknown",
            target_store=tmp_path / "targets.json",
        )


def test_new_base_initialization_removes_only_original_default_table(monkeypatch):
    import feishu.schema as schema

    class FakeClient:
        def __init__(self):
            self.deleted = []

        def list_tables(self):
            return [{"table_id": "tbl-default", "name": "数据表"}]

        def delete_table(self, table_id):
            self.deleted.append(table_id)

    class FakeSchemaManager:
        def __init__(self, client):
            self.client = client

        def ensure_all_tables(self):
            return {
                schema.SEARCH_TASKS_TABLE: "tbl1",
                schema.SEARCH_VIDEOS_TABLE: "tbl2",
                schema.INFLUENCERS_TABLE: "tbl3",
                schema.INFLUENCER_VIDEOS_TABLE: "tbl4",
            }

    monkeypatch.setattr(schema, "SchemaManager", FakeSchemaManager)
    client = FakeClient()
    context = FeishuClientContext(
        client=client,
        app_token="bascn_created",
        auth_mode="cli",
        created_base=True,
    )

    table_ids = initialize_created_base_schema(context)

    assert len(table_ids) == 4
    assert client.deleted == ["tbl-default"]
