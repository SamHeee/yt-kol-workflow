from types import SimpleNamespace

import pytest

from config import FeishuConfig
from feishu.client_factory import (
    BaseTarget,
    FeishuClientContext,
    create_bitable_client,
    extract_app_token,
    initialize_created_base_schema,
    load_base_target,
    save_base_target,
)


class FakeManager:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cli_path = "resolved-lark-cli"
        self.ready_calls = 0
        self.create_calls = []
        self.resolve_calls = []
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

    def create_base(self, name, *, table_name="", fields=()):
        self.create_calls.append((name, table_name, list(fields)))
        return {
            "name": name,
            "app_token": "bascn_created",
            "base_url": "https://example.feishu.cn/base/bascn_created",
            "initial_table_name": table_name,
        }

    def resolve_base_url(self, url):
        self.resolve_calls.append(url)
        token = "bascn_from_wiki" if "/wiki/" in url else "bascn_existing"
        return {
            "name": "KOL CRM",
            "app_token": token,
            "base_url": url,
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
    assert FakeManager.instances[0].resolve_calls == [
        "https://tenant.feishu.cn/base/bascn_existing?table=tbl1"
    ]
    stored = load_base_target("kol-test", tmp_path / "targets.json")
    assert stored.app_token == "bascn_existing"


def test_explicit_same_target_preserves_pending_view_recovery_metadata(tmp_path):
    store = tmp_path / "targets.json"
    save_base_target(
        "kol-test",
        BaseTarget(
            app_token="bascn_existing",
            url="https://tenant.feishu.cn/base/bascn_existing",
            name="KOL Existing",
            created_at="2026-08-12T00:00:00+00:00",
            pending_view_tables=("搜索任务表", "合作红人表"),
        ),
        store,
    )

    context = create_bitable_client(
        auth_mode="cli",
        app_token="bascn_existing",
        cli_profile="kol-test",
        target_store=store,
        manager_factory=FakeManager,
        cli_client_class=FakeCliClient,
    )

    assert context.pending_view_tables == ("搜索任务表", "合作红人表")
    stored = load_base_target("kol-test", store)
    assert stored.created_at == "2026-08-12T00:00:00+00:00"
    assert stored.pending_view_tables == context.pending_view_tables


def test_cli_mode_resolves_wiki_url_and_persists_real_base_token(tmp_path):
    wiki_url = "https://tenant.feishu.cn/wiki/wikcn_node_token"

    context = cli_context(
        tmp_path,
        app_token=wiki_url,
        cli_profile="kol-wiki",
    )

    assert context.app_token == "bascn_from_wiki"
    assert context.base_url == wiki_url
    assert context.auth_mode == "cli"
    assert FakeManager.instances[0].resolve_calls == [wiki_url]
    stored = load_base_target("kol-wiki", tmp_path / "targets.json")
    assert stored.app_token == "bascn_from_wiki"
    assert stored.url == wiki_url


def test_auto_mode_routes_wiki_url_to_cli_even_with_app_credentials(tmp_path):
    context = create_bitable_client(
        app_token="https://tenant.feishu.cn/wiki/wikcn_node_token",
        app_id="legacy_app",
        app_secret="legacy_secret",
        auth_mode="auto",
        target_store=tmp_path / "targets.json",
        manager_factory=FakeManager,
        cli_client_class=FakeCliClient,
    )

    assert context.auth_mode == "cli"
    assert context.app_token == "bascn_from_wiki"
    assert FakeManager.instances[0].resolve_calls


def test_app_mode_rejects_wiki_url_instead_of_using_node_token(tmp_path):
    with pytest.raises(RuntimeError, match="不能把 Wiki/分享链接当作 app_token"):
        create_bitable_client(
            app_token="https://tenant.feishu.cn/wiki/wikcn_node_token",
            app_id="legacy_app",
            app_secret="legacy_secret",
            auth_mode="app",
            target_store=tmp_path / "targets.json",
            manager_factory=FakeManager,
            cli_client_class=FakeCliClient,
        )

    assert FakeManager.instances == []


def test_url_helpers_preserve_cli_input_but_never_extract_wiki_node_token():
    wiki_url = "https://tenant.feishu.cn/wiki/wikcn_node_token"
    base_url = "https://tenant.feishu.cn/base/bascn_real?table=tbl1"

    assert FeishuConfig.extract_app_token(wiki_url) == wiki_url
    assert FeishuConfig.extract_app_token(base_url) == base_url
    assert FeishuConfig.extract_app_token("bascn_plain") == "bascn_plain"
    assert extract_app_token(wiki_url) == ""
    assert extract_app_token(base_url) == "bascn_real"


def test_missing_target_creates_and_persists_base_then_reuses_it(tmp_path):
    first = cli_context(tmp_path, base_name="KOL测试库")

    assert first.created_base is True
    assert first.app_token == "bascn_created"
    assert first.base_url.endswith("/base/bascn_created")
    assert len(first.pending_view_tables) == 7
    assert "搜索任务表" in first.pending_view_tables
    stored_first = load_base_target("kol-workflow", tmp_path / "targets.json")
    assert stored_first.pending_view_tables == first.pending_view_tables
    assert FakeManager.instances[0].create_calls == [
        (
            "KOL测试库",
            "搜索任务表",
            [{"type": "text", "name": "多行文本"}],
        )
    ]

    second = cli_context(tmp_path, base_name="不应再次创建")
    assert second.created_base is False
    assert second.app_token == "bascn_created"
    assert second.pending_view_tables == first.pending_view_tables
    assert FakeManager.instances[1].create_calls == []


def test_base_creation_failure_warns_about_possible_orphan(tmp_path):
    class FailingManager(FakeManager):
        def create_base(self, name, *, table_name="", fields=()):
            raise RuntimeError("replacement table failed")

    with pytest.raises(RuntimeError, match="可能已经创建了一个未保存到本地的 Base"):
        create_bitable_client(
            auth_mode="cli",
            target_store=tmp_path / "targets.json",
            manager_factory=FailingManager,
            cli_client_class=FakeCliClient,
        )

    assert not (tmp_path / "targets.json").exists()


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


def test_app_mode_still_accepts_a_direct_base_url(tmp_path):
    context = create_bitable_client(
        app_token="https://tenant.feishu.cn/base/bascn_existing?table=tbl1",
        app_id="legacy_app",
        app_secret="secret",
        auth_mode="app",
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
    assert manager.resolve_calls == []
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
