"""Offline recovery tests exercising the real search/filter/detail pipeline."""

import copy
import json
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

import main
from config import FeishuConfig, KeywordTask, WorkflowConfig, YouTubeConfig
from export.excel_exporter import ExcelExporter
from workflow import persistence
from workflow.seen_channels import SeenChannelStore
from workflow.state import WorkflowState
from youtube import channels, playlists, search, videos


@pytest.fixture(autouse=True)
def forbid_external_access(monkeypatch):
    for name in ("FEISHU_AUTO_SETUP", "FEISHU_AUTO_INSTALL", "FEISHU_OPEN_BROWSER"):
        monkeypatch.setenv(name, "false")

    def forbidden(*args, **kwargs):
        pytest.fail("Recovery regression tests must not access the network or authorization")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(main, "_create_feishu_context", forbidden)
    monkeypatch.setattr(main, "ask_video_fetch_method", lambda *args: True)


@pytest.fixture
def config(tmp_path):
    return WorkflowConfig(
        youtube=YouTubeConfig(api_key="offline-test-key"),
        feishu=FeishuConfig(
            auth_mode="cli", cli_profile="offline-tests", app_token="offline-base",
            auto_setup=False, auto_install=False, open_browser=False,
        ),
        output_dir=str(tmp_path / "output"),
        seen_channels_file=str(tmp_path / "seen.json"),
        max_results=2,
        estimated_channels_per_keyword=2,
    )


class YouTubeScenario:
    """Emulate API payloads below all four real workflow phases."""

    def __init__(self, monkeypatch):
        self.channel_ids = ["UC1", "UC2"]
        self.video_calls = Counter()
        self.upload_calls = Counter()
        self.channel_requests = []
        self.search_queries = []
        self.upload_actions = defaultdict(list)
        self.video_actions = defaultdict(list)
        for module in (channels, playlists, search, videos):
            monkeypatch.setattr(module, "api_request", self.request)

    def request(self, url, params, quota_tracker=None, api_name=""):
        if quota_tracker:
            quota_tracker.consume(api_name)
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "search":
            self.search_queries.append(params["q"])
            return True, {"items": [
                {"id": {"videoId": f"search{cid[2:]:0>5}"},
                 "snippet": {"channelId": cid, "channelTitle": f"Creator {cid}",
                             "title": "Camping gear review", "publishedAt": "2026-10-01T00:00:00Z"}}
                for cid in self.channel_ids
            ]}, ""
        if endpoint == "channels":
            requested = params["id"].split(",")
            self.channel_requests.append(requested)
            return True, {"items": [
                {"id": cid,
                 "snippet": {"title": f"Creator {cid}", "description": "Camping reviews",
                             "publishedAt": "2020-01-01T00:00:00Z"},
                 "statistics": {"subscriberCount": "1000", "videoCount": "10", "viewCount": "100000"},
                 "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}
                for cid in requested
            ]}, ""
        if endpoint == "playlistItems":
            cid = "UC" + params["playlistId"][2:]
            self.upload_calls[cid] += 1
            if self.upload_actions[cid]:
                action = self.upload_actions[cid].pop(0)
                if isinstance(action, BaseException):
                    raise action
                return False, {}, action
            return True, {"items": [{"contentDetails": {"videoId": f"recent{cid[2:]:0>5}"}}]}, ""
        assert endpoint == "videos", endpoint
        ids = params["id"].split(",")
        group = "search" if ids[0].startswith("search") else "recent"
        self.video_calls[group] += 1
        if self.video_actions[group]:
            return False, {}, self.video_actions[group].pop(0)
        return True, {"items": [
            {"id": vid,
             "snippet": {"channelId": "UC" + str(int(vid[-5:])),
                         "channelTitle": "Camping Creator", "title": "Camping gear review",
                         "publishedAt": "2026-10-01T00:00:00Z", "tags": ["camping", "review"]},
             "statistics": {"viewCount": "20000", "likeCount": "900", "commentCount": "100"},
             "contentDetails": {"duration": "PT5M", "caption": "true"}}
            for vid in ids
        ]}, ""


@pytest.fixture
def api(monkeypatch):
    return YouTubeScenario(monkeypatch)


class MemoryExcel:
    def __init__(self, directory):
        self.directory = str(directory)
        self.attempts = Counter()
        self.failures = Counter()
        self.rows = {}

    def get_output_dir(self):
        return self.directory

    def _write(self, table, rows):
        self.attempts[table] += 1
        if self.failures[table]:
            self.failures[table] -= 1
            raise PermissionError(f"Workbook is open: {table}")
        self.rows[table] = copy.deepcopy(rows)

    def export_search_tasks(self, rows):
        self._write("tasks", rows)

    def export_search_videos(self, rows, keyword=""):
        self._write("search", rows)

    def export_influencers(self, rows):
        self._write("influencers", rows)

    def export_influencer_videos(self, rows):
        self._write("recent", rows)

    def ensure_all_files(self):
        self._write("ensure", [])


class MemoryFeishu:
    app_token = "offline-base"

    def __init__(self):
        self.attempts = Counter()
        self.failures = Counter()
        self.records = defaultdict(dict)

    def upsert_records(self, table_id, records, **kwargs):
        self.attempts[table_id] += 1
        if self.failures[table_id]:
            self.failures[table_id] -= 1
            raise RuntimeError(f"Temporary Feishu write failure: {table_id}")
        created = 0
        for record in records:
            fields = record["fields"]
            key = fields.get("唯一键") or fields.get("Channel ID")
            created += key not in self.records[table_id]
            previous = self.records[table_id].get(key, {}).get("fields", {})
            self.records[table_id][key] = {"fields": {
                **previous, **copy.deepcopy(fields),
            }}
        return {"created": created, "updated": len(records) - created}

    def get_all_records(self, table_id):
        return list(self.records[table_id].values())

    def get_records_by_field_values(self, table_id, field_name, values, **kwargs):
        return {
            record["fields"][field_name]: {**record, "created_time": 1790812800000}
            for record in self.records[table_id].values()
            if record["fields"].get(field_name) in values
        }


def schema():
    return SimpleNamespace(
        table_ids={name: name for name in (
            main.SEARCH_TASKS_TABLE, main.SEARCH_VIDEOS_TABLE,
            main.INFLUENCERS_TABLE, main.INFLUENCER_VIDEOS_TABLE,
        )},
        get_table_id=lambda name: name,
    )


def run(config, state, exporter=None, client=None, **kwargs):
    return main.run_single_keyword(
        state.keyword, config, sort_order="relevance", use_playlist=True,
        state=state, excel_exporter=exporter, feishu_client=client,
        schema_mgr=schema() if client else None, **kwargs,
    )


def reload(state):
    return WorkflowState.load(str(state.state_file))


def test_phase_b_temporary_error_remains_retryable_after_loading_checkpoint(config, api, tmp_path):
    api.video_actions["search"] = ["HTTP 503: temporary error"]
    state = WorkflowState("camping", str(tmp_path / "state"))
    exporter = MemoryExcel(tmp_path / "excel")

    interrupted = run(config, state, exporter, no_detail=True)
    state = reload(state)
    assert interrupted["status"] == "部分成功"
    assert not state.is_phase_complete("b")
    assert not state.is_phase_complete("c")

    completed = run(config, state, exporter, no_detail=True)
    assert completed["status"] == "成功"
    assert completed["qualified_count"] == 2
    assert api.video_calls["search"] == 2
    assert api.search_queries == ["camping"]
    assert {v["video_id"] for v in exporter.rows["search"]} == {"search00001", "search00002"}


@pytest.mark.parametrize("destination", ["feishu", "excel"])
def test_phase_b_output_failure_retries_only_uncommitted_destinations(config, api, tmp_path, destination):
    state = WorkflowState("camping", str(tmp_path / "state"))
    client = MemoryFeishu()
    exporter = MemoryExcel(tmp_path / "excel")
    if destination == "feishu":
        client.failures[main.SEARCH_VIDEOS_TABLE] = 1
    else:
        exporter.failures["search"] = 1

    interrupted = run(config, state, exporter, client, no_detail=True)
    assert interrupted["status"] == "异常"
    completed = run(config, reload(state), exporter, client, no_detail=True)
    assert completed["status"] == "成功"
    assert api.video_calls["search"] == 1
    assert len(client.records[main.SEARCH_VIDEOS_TABLE]) == 2
    assert len(exporter.rows["search"]) == 2
    assert client.attempts[main.SEARCH_VIDEOS_TABLE] == (2 if destination == "feishu" else 1)
    assert exporter.attempts["search"] == (2 if destination == "excel" else 1)

    before = (client.attempts[main.SEARCH_VIDEOS_TABLE], exporter.attempts["search"])
    assert run(config, reload(state), exporter, client, no_detail=True)["status"] == "成功"
    assert (client.attempts[main.SEARCH_VIDEOS_TABLE], exporter.attempts["search"]) == before


@pytest.mark.parametrize("interruption", ["quotaExceeded", KeyboardInterrupt()])
def test_phase_d_resume_retains_committed_channels_and_complete_excel(config, api, tmp_path, interruption):
    api.upload_actions["UC2"] = [interruption]
    state = WorkflowState("camping", str(tmp_path / "state"))
    exporter = ExcelExporter(config.output_dir, "camping", timestamp="recovery")

    interrupted = run(config, state, exporter)
    assert interrupted["status"] == ("中断" if isinstance(interruption, KeyboardInterrupt) else "配额中断")
    state = reload(state)
    details, recent = state.channel_payloads()
    assert [v["channel_id"] for v in details] == ["UC1"]
    assert [v["video_id"] for v in recent] == ["recent00001"]
    assert state.get_remaining_channels(["UC1", "UC2"]) == ["UC2"]

    exporter = ExcelExporter.resume(state.data["excel_output_dir"])
    completed = run(config, state, exporter)
    assert completed["status"] == "成功"
    state = reload(state)
    assert {v["channel_id"] for v in state.data["influencer_details"]} == {"UC1", "UC2"}
    assert {v["video_id"] for v in state.data["influencer_videos"]} == {"recent00001", "recent00002"}
    assert api.channel_requests == [["UC1", "UC2"], ["UC2"]]
    assert api.upload_calls == Counter({"UC1": 1, "UC2": 2})
    output = Path(exporter.get_output_dir())
    assert set(pd.read_excel(output / "search_videos.xlsx")["Video ID"]) == {"search00001", "search00002"}
    assert set(pd.read_excel(output / "influencers.xlsx")["Channel ID"]) == {"UC1", "UC2"}
    assert set(pd.read_excel(output / "influencer_videos.xlsx")["Video ID"]) == {"recent00001", "recent00002"}
    assert pd.read_excel(output / "search_tasks.xlsx").iloc[0]["执行状态"] == "成功"
    assert SeenChannelStore(config.seen_channels_file).ids() == {"UC1", "UC2"}


@pytest.mark.parametrize("destination", ["feishu", "excel"])
def test_phase_d_output_failure_replays_cached_payload_without_refetch(config, api, tmp_path, destination):
    state = WorkflowState("camping", str(tmp_path / "state"))
    client = MemoryFeishu()
    exporter = MemoryExcel(tmp_path / "excel")
    if destination == "feishu":
        client.failures[main.INFLUENCERS_TABLE] = 1
    else:
        exporter.failures["influencers"] = 1

    assert run(config, state, exporter, client)["status"] == "异常"
    assert reload(state).is_phase_complete("d")
    assert not SeenChannelStore(config.seen_channels_file).ids()
    assert run(config, reload(state), exporter, client)["status"] == "成功"
    assert api.channel_requests == [["UC1", "UC2"]]
    assert api.upload_calls == Counter({"UC1": 1, "UC2": 1})
    assert len(client.records[main.INFLUENCERS_TABLE]) == 2
    assert len(client.records[main.INFLUENCER_VIDEOS_TABLE]) == 2
    assert {v["channel_id"] for v in exporter.rows["influencers"]} == {"UC1", "UC2"}
    assert {v["video_id"] for v in exporter.rows["recent"]} == {"recent00001", "recent00002"}
    assert client.attempts[main.INFLUENCERS_TABLE] == (2 if destination == "feishu" else 1)
    assert exporter.attempts["influencers"] == (2 if destination == "excel" else 1)


def test_no_detail_search_does_not_suppress_a_later_full_search(config, api, tmp_path):
    first = WorkflowState("camping", str(tmp_path / "first"))
    assert run(config, first, MemoryExcel(tmp_path / "first-excel"), no_detail=True)["status"] == "成功"
    assert SeenChannelStore(config.seen_channels_file).ids() == set()
    assert api.channel_requests == []

    second = WorkflowState("camping", str(tmp_path / "second"))
    result = run(config, second, MemoryExcel(tmp_path / "second-excel"))
    assert result["status"] == "成功"
    assert result["influencers_fetched"] == 2
    assert api.channel_requests == [["UC1", "UC2"]]


def test_batch_retries_failed_channel_in_next_keyword(config, api):
    api.channel_ids = ["UC1"]
    api.upload_actions["UC1"] = ["HTTP 503: temporary error"]
    result = main.run_batch(
        [KeywordTask("camping chairs", max_results=1), KeywordTask("camping gear", max_results=1)],
        config, default_sort_order="relevance", use_playlist=True, skip_confirm=True,
        write_excel=True, write_feishu=False,
    )
    assert [row["status"] for row in result["results"]] == ["部分成功", "成功"]
    assert [row["new_channels"] for row in result["results"]] == [1, 1]
    assert api.upload_calls["UC1"] == 2
    assert SeenChannelStore(config.seen_channels_file).ids() == {"UC1"}


def test_legacy_seen_ids_without_completion_evidence_are_rechecked(config, api, tmp_path):
    Path(config.seen_channels_file).write_text(json.dumps({
        "version": 1, "channels": {"UC1": {"channel_id": "UC1"}},
    }), encoding="utf-8")
    assert SeenChannelStore(config.seen_channels_file).ids() == set()
    result = run(config, WorkflowState("camping", str(tmp_path / "state")), MemoryExcel(tmp_path / "excel"))
    assert result["status"] == "成功"
    assert result["influencers_fetched"] == 2
    assert api.upload_calls["UC1"] == 1
    assert SeenChannelStore(config.seen_channels_file).ids() == {"UC1", "UC2"}


def test_atomic_checkpoint_replace_failure_preserves_previous_json(tmp_path, monkeypatch):
    state = WorkflowState("camping", str(tmp_path / "state"))
    state.update_stats(search_results_count=1)
    original = state.state_file.read_bytes()

    def fail_replace(source, destination):
        raise PermissionError("Simulated interrupted atomic replace")

    monkeypatch.setattr(persistence.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="atomic replace"):
        state.update_stats(search_results_count=999)
    assert state.state_file.read_bytes() == original
    assert reload(state).data["search_results_count"] == 1
    assert not list(state.state_file.parent.glob("*.tmp"))


def test_handle_search_resume_reuses_original_excel_directory(config, api, tmp_path):
    api.upload_actions["UC2"] = ["quotaExceeded"]
    state = WorkflowState("camping", str(tmp_path / "state"))
    exporter = ExcelExporter(config.output_dir, "camping", timestamp="original")
    assert run(config, state, exporter)["status"] == "配额中断"
    original_directories = list(Path(config.output_dir).iterdir())

    args = main.build_parser().parse_args([
        "search", "--resume", str(state.state_file), "--no-feishu",
    ])
    result = main.handle_search(args, config)
    assert result["status"] == "成功"
    assert list(Path(config.output_dir).iterdir()) == original_directories
    assert reload(state).data["excel_output_dir"] == str(Path(exporter.get_output_dir()).resolve())
    assert set(pd.read_excel(Path(exporter.get_output_dir()) / "influencer_videos.xlsx")["Video ID"]) == {
        "recent00001", "recent00002",
    }


def test_resume_keeps_original_filters_instead_of_new_cli_defaults(config, api, tmp_path):
    initial_config = replace(config, min_views=50000, min_engagement=10.0)
    state = WorkflowState("camping", str(tmp_path / "state"))
    exporter = MemoryExcel(tmp_path / "excel")
    api.video_actions["search"] = ["HTTP 503: temporary error"]
    assert run(initial_config, state, exporter, no_detail=True)["status"] == "部分成功"

    changed_defaults = replace(config, min_views=1, min_engagement=0)
    result = run(changed_defaults, reload(state), exporter, no_detail=True)
    assert result["status"] == "成功"
    assert result["qualified_count"] == 0
    assert all(not row["is_qualified"] for row in exporter.rows["search"])
    assert reload(state).data["run_options"]["min_views"] == 50000


def test_legacy_checkpoint_recomputes_filters_and_rechecks_old_pending_channel(config, api, tmp_path):
    state = WorkflowState("camping", str(tmp_path / "state"))
    exporter = MemoryExcel(tmp_path / "excel")
    assert run(config, state, exporter, no_detail=True)["status"] == "成功"
    legacy = json.loads(state.state_file.read_text(encoding="utf-8"))
    legacy.pop("checkpoint_version")
    legacy["qualified_videos"] = []
    legacy["phase_c_complete"] = legacy["phase_d_complete"] = True
    legacy["phase_d_progress"]["completed_channel_ids"] = ["UC1"]
    legacy["new_channels"] = [{"channel_id": "UC1", "representative_video": {}}]
    state.state_file.write_text(json.dumps(legacy), encoding="utf-8")
    client = MemoryFeishu()
    client.records[main.INFLUENCERS_TABLE]["UC1"] = {"fields": {
        "Channel ID": "UC1", "断更评估": "有断更风险",
    }}
    # Remote presence alone must not override the old task's pending channel.
    Path(config.seen_channels_file).unlink()
    migrated = reload(state)
    assert not migrated.is_phase_complete("b")
    assert not migrated.is_phase_complete("c")
    assert not migrated.is_phase_complete("d")
    assert migrated.get_remaining_channels(["UC1", "UC2"]) == ["UC1", "UC2"]

    result = run(config, migrated, exporter, client)
    assert result["status"] == "成功"
    assert result["qualified_count"] == 2
    assert api.video_calls["search"] == 2
    assert api.channel_requests == [["UC1", "UC2"]]
    assert {v["video_id"] for v in exporter.rows["recent"]} == {"recent00001", "recent00002"}


@pytest.mark.parametrize("failed_output", ["remote_task", "local_task", "local_headers"])
def test_final_audit_failure_is_visible_in_checkpoint_and_surviving_outputs(
    config, api, tmp_path, failed_output,
):
    state = WorkflowState("camping", str(tmp_path / "state"))
    client = MemoryFeishu()
    exporter = MemoryExcel(tmp_path / "excel")
    if failed_output == "remote_task":
        client.failures[main.SEARCH_TASKS_TABLE] = 1
    else:
        exporter.failures["tasks" if failed_output == "local_task" else "ensure"] = 1

    result = run(config, state, exporter, client, no_detail=True)
    assert result["status"] == "部分成功"
    assert main._result_exit_code(result) == 2
    saved_task = reload(state).data["task_output"]
    assert saved_task["status"] == "部分成功"
    assert saved_task["note"] == result["error"]
    assert result["error"]
    if failed_output != "local_task":
        assert exporter.rows["tasks"][0]["status"] == "部分成功"
    if failed_output != "remote_task":
        remote_task = client.records[main.SEARCH_TASKS_TABLE]["camping"]["fields"]
        assert remote_task["执行状态"] == "部分成功"


def test_keyboard_interrupt_stops_batch_before_next_keyword(config, api):
    api.upload_actions["UC1"] = [KeyboardInterrupt()]
    result = main.run_batch(
        [KeywordTask("first", max_results=2), KeywordTask("second", max_results=2)],
        config, default_sort_order="relevance", use_playlist=True, skip_confirm=True,
        write_excel=False, write_feishu=False,
    )
    assert result["status"] == "中断"
    assert main._result_exit_code(result) == 130
    assert result["remaining_keywords"] == 1
    assert len(result["results"]) == 1
    assert api.search_queries == ["first"]


def test_partial_remote_detail_row_does_not_hide_unwritten_videos_or_reset_manual_fields(
    config, api, tmp_path,
):
    api.channel_ids = ["UC1"]
    client = MemoryFeishu()
    client.failures[main.INFLUENCER_VIDEOS_TABLE] = 1
    first_state = WorkflowState("camping chairs", str(tmp_path / "first"))
    result = run(config, first_state, MemoryExcel(tmp_path / "first-excel"), client)
    assert result["status"] == "异常"
    remote_fields = client.records[main.INFLUENCERS_TABLE]["UC1"]["fields"]
    assert remote_fields["断更评估"] in {"持续更新", "有断更风险"}
    assert not client.records[main.INFLUENCER_VIDEOS_TABLE]
    assert SeenChannelStore(config.seen_channels_file).pending_ids() == {"UC1"}
    assert SeenChannelStore(config.seen_channels_file).ids() == set()

    human_changes = {
        "KOL Name": "Sarah User", "联系邮箱": "manual@business.example",
        "开发状态": "已联系", "来源关键词": "original human keyword",
    }
    remote_fields.update(human_changes)
    second_state = WorkflowState("camping gear", str(tmp_path / "second"))
    result = run(config, second_state, MemoryExcel(tmp_path / "second-excel"), client)
    assert result["status"] == "成功"
    assert result["new_channels"] == 1
    assert result["influencers_fetched"] == 1
    assert api.channel_requests == [["UC1"], ["UC1"]]
    assert api.upload_calls["UC1"] == 2
    assert set(client.records[main.INFLUENCER_VIDEOS_TABLE]) == {"recent00001"}
    updated = client.records[main.INFLUENCERS_TABLE]["UC1"]["fields"]
    assert {field: updated[field] for field in human_changes} == human_changes
    assert updated["邮箱状态"] == "已获取"
    assert SeenChannelStore(config.seen_channels_file).pending_ids() == set()
    assert SeenChannelStore(config.seen_channels_file).ids() == {"UC1"}
