import json

from config import FeishuConfig, WorkflowConfig
from workflow.state import CURRENT_DATA_SCHEMA_VERSION, WorkflowState
from workflow.seen_channels import default_seen_channels_path


def test_new_state_contains_current_schema_version(tmp_path):
    state = WorkflowState("test", state_dir=str(tmp_path))

    assert state.data["data_schema_version"] == CURRENT_DATA_SCHEMA_VERSION


def test_legacy_state_loads_as_version_one(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "keyword": "legacy",
                "current_phase": "D",
                "phase_d_complete": True,
                "phase_d_progress": {"completed_channel_ids": []},
            }
        ),
        encoding="utf-8",
    )

    state = WorkflowState.load(str(path))

    assert state.data["data_schema_version"] == 1


def test_default_seen_channels_path_isolated_by_profile_and_base(tmp_path):
    first = default_seen_channels_path(
        cli_profile="kol-workflow",
        app_token="bascn_first_secret",
        root=tmp_path,
    )
    second = default_seen_channels_path(
        cli_profile="kol-workflow",
        app_token="bascn_second_secret",
        root=tmp_path,
    )

    assert first != second
    assert first.parent.name == "kol-workflow"
    assert "bascn_first_secret" not in str(first)
    assert first.suffix == ".json"


def test_workflow_config_rebinds_only_implicit_seen_state(monkeypatch, tmp_path):
    monkeypatch.setattr("workflow.seen_channels.DEFAULT_STATE_ROOT", tmp_path)
    config = WorkflowConfig(feishu=FeishuConfig(cli_profile="profile-one"))
    before = config.seen_channels_file

    after = config.bind_feishu_target("bascn_resolved")

    assert after != before
    assert "bascn_resolved" not in after

    explicit = WorkflowConfig(
        feishu=FeishuConfig(cli_profile="profile-one"),
        seen_channels_file=str(tmp_path / "explicit.json"),
    )
    explicit.bind_feishu_target("bascn_other")
    assert explicit.seen_channels_file == str((tmp_path / "explicit.json").resolve())
