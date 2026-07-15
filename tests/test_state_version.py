import json

from workflow.state import CURRENT_DATA_SCHEMA_VERSION, WorkflowState


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
