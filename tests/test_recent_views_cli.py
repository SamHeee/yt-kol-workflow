import json
from types import SimpleNamespace

import pytest

import main
from feishu.schema import INFLUENCERS_TABLE, INFLUENCER_VIDEOS_TABLE
from test_schema_migrations import Admin
from test_recent_views_refresh import Client


def test_upgrade_defaults_to_preview_and_requires_explicit_base(monkeypatch, tmp_path):
    parser = main.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["feishu-upgrade"])
    args = parser.parse_args(["feishu-upgrade", "--feishu-app-token", "offline-base", "--report", str(tmp_path / "plan.json")])
    admin = Admin()
    contexts = []
    def context(cfg, **kwargs):
        contexts.append(kwargs)
        return SimpleNamespace(auth_mode="cli", app_token="offline-base", cli_manager=SimpleNamespace(cli_path="mock"))
    monkeypatch.setattr(main, "_create_feishu_context", context)
    monkeypatch.setattr("feishu.lark_cli_base.LarkCliBaseRunner", lambda **kw: admin)
    main.handle_feishu_upgrade(args)
    assert not admin.writes
    assert contexts == [{"create_base_if_missing": False}]
    assert json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))["status"] == "preview"


def test_upgrade_apply_journals_verified_completion(monkeypatch, tmp_path):
    args = main.build_parser().parse_args(["feishu-upgrade", "--feishu-app-token", "offline-base", "--apply", "--report", str(tmp_path / "plan.json")])
    admin = Admin()
    monkeypatch.setattr(main, "_create_feishu_context", lambda *a, **kw: SimpleNamespace(auth_mode="cli", app_token="offline-base", cli_manager=SimpleNamespace(cli_path="mock")))
    monkeypatch.setattr("feishu.lark_cli_base.LarkCliBaseRunner", lambda **kw: admin)
    main.handle_feishu_upgrade(args)
    report = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert report["status"] == "verified" and len(report["applied"]) == 12


def test_table_refresh_cli_needs_no_youtube_key_and_never_creates_base(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    client = Client()
    client.find_table_by_name = lambda name: {INFLUENCERS_TABLE: "creators", INFLUENCER_VIDEOS_TABLE: "videos"}[name]
    client.list_fields = lambda tid: [{"field_name": name} for name in ("最近视频平均播放量", "最近视频中位播放量")]
    def context(cfg, **kwargs):
        assert kwargs == {"create_base_if_missing": False}
        return SimpleNamespace(client=client, created_base=False)
    monkeypatch.setattr(main, "_create_feishu_context", context)
    args = main.build_parser().parse_args(["refresh-influencers", "--fields", "recent-views", "--recent-source", "table", "--dry-run", "--stats", str(tmp_path / "refresh.json")])
    main.handle_refresh_influencers(args)
    report = json.loads((tmp_path / "refresh.json").read_text(encoding="utf-8"))
    assert report["quota_used"] == 0 and report["planned_updates"] == 1
    assert not client.writes
