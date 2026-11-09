from copy import deepcopy
import pytest

from workflow import refresh_influencers as mod
from workflow.recent_video_metrics import MEAN_FIELD, MEDIAN_FIELD


class Client:
    def __init__(self):
        self.data = {"creators": [{"record_id": "c1", "fields": {
            "Channel ID": "UC1", "Channel Name": "Creator", MEAN_FIELD: 500, MEDIAN_FIELD: 400,
            "apify邮箱": "manual@example.com", "开发状态": "进入合作", "KOL Name": "Human"}}], "videos": []}
        self.writes = []
        self.fail_save = False

    def get_all_records(self, table):
        return deepcopy(self.data[table])

    def batch_get_records(self, table, ids):
        return deepcopy([r for r in self.data[table] if r["record_id"] in ids])

    def batch_update_records(self, table, updates):
        self.writes.append((table, deepcopy(updates)))
        for update in updates:
            row = next(r for r in self.data[table] if r["record_id"] == update["record_id"])
            row["fields"].update(update["fields"])
        return len(updates)

    def batch_create_records_with_ids(self, table, records):
        if self.fail_save:
            raise RuntimeError("injected storage failure")
        added = []
        for record in records:
            row = {"record_id": f"v{len(self.data[table])}", **deepcopy(record)}
            self.data[table].append(row)
            added.append(row)
        self.writes.append((table, deepcopy(added)))
        return deepcopy(added)


def sample(views=0):
    return [{"video_id": "abcdefghijk", "channel_id": "UC1", "view_count": views,
             "view_count_available": views is not None, "published_at": "2026-10-01 00:00:00"}]


def run(client, **kwargs):
    return mod.refresh_influencers(api_key="test-key", client=client, table_id="creators",
                                  fields="recent-views", videos_table_id="videos", **kwargs)


def test_live_refresh_saves_zero_before_metrics_and_preserves_manual_fields(monkeypatch):
    client = Client()
    monkeypatch.setattr(mod, "collect_recent_videos", lambda *a, **kw: (sample(), ""))
    result = run(client)
    assert not result["errors"]
    assert [table for table, _ in client.writes] == ["videos", "creators"]
    fields = client.data["creators"][0]["fields"]
    assert fields[MEAN_FIELD] == fields[MEDIAN_FIELD] == 0
    assert fields["apify邮箱"] == "manual@example.com" and fields["开发状态"] == "进入合作"
    assert result["recent_video_snapshots"]["UC1"]["recent_video_sample_count"] == 1


@pytest.mark.parametrize("failure", ["fetch", "save", "readback"])
def test_failure_retains_both_previous_metrics(monkeypatch, failure):
    client = Client()
    monkeypatch.setattr(mod, "collect_recent_videos", lambda *a, **kw: ([], "fetch failed") if failure == "fetch" else (sample(10), ""))
    client.fail_save = failure == "save"
    if failure == "readback":
        original = client.batch_get_records
        monkeypatch.setattr(client, "batch_get_records", lambda table, ids: [] if table == "videos" else original(table, ids))
    result = run(client)
    assert result["errors"] and result["actual_updates"] == 0
    fields = client.data["creators"][0]["fields"]
    assert fields[MEAN_FIELD] == 500 and fields[MEDIAN_FIELD] == 400


@pytest.mark.parametrize("rows", [[], sample(None)])
def test_success_without_available_views_clears_old_metrics(monkeypatch, rows):
    client = Client()
    monkeypatch.setattr(mod, "collect_recent_videos", lambda *a, **kw: (rows, ""))
    result = run(client)
    assert not result["errors"]
    fields = client.data["creators"][0]["fields"]
    assert fields[MEAN_FIELD] is None and fields[MEDIAN_FIELD] is None


def test_table_backfill_uses_latest_ten_and_never_calls_youtube(monkeypatch):
    client = Client()
    for i in range(12):
        client.data["videos"].append({"record_id": f"v{i}", "fields": {
            "Video ID": str(i), "Channel ID": "UC1", "Publish Time": f"2026-10-{i+1:02}T00:00:00Z", "Views": i}})
    monkeypatch.setattr(mod, "collect_recent_videos", lambda *a, **kw: pytest.fail("No YouTube requests"))
    result = run(client, recent_source="table")
    assert result["estimated_quota"] == 0
    assert client.data["creators"][0]["fields"][MEAN_FIELD] == 6.5
    assert client.data["creators"][0]["fields"][MEDIAN_FIELD] == 6.5
    assert result["video_records_saved"] == 0


def test_dry_run_reads_sample_but_does_not_write(monkeypatch):
    client = Client()
    monkeypatch.setattr(mod, "collect_recent_videos", lambda *a, **kw: (sample(42), ""))
    result = run(client, dry_run=True)
    assert result["planned_updates"] == 1 and not client.writes


def test_duplicate_creators_share_one_sample_and_repeat_refresh_does_not_create_duplicates(monkeypatch):
    client = Client()
    client.data["creators"].append({"record_id": "c2", "fields": deepcopy(client.data["creators"][0]["fields"])})
    calls = []
    def collect(*args, **kwargs):
        calls.append(1)
        return sample(42), ""
    monkeypatch.setattr(mod, "collect_recent_videos", collect)
    run(client)
    assert len(calls) == 1
    client.data["videos"][0]["fields"]["字幕内容"] = "Human transcript"
    run(client)
    assert len(client.data["videos"]) == 1
    assert client.data["videos"][0]["fields"]["字幕内容"] == "Human transcript"
    assert all(r["fields"][MEAN_FIELD] == 42 for r in client.data["creators"])


def test_concurrent_metric_change_does_not_get_overwritten(monkeypatch):
    client = Client()
    monkeypatch.setattr(mod, "collect_recent_videos", lambda *a, **kw: (sample(42), ""))
    original = client.batch_get_records
    def read(table, ids):
        if table == "creators":
            client.data[table][0]["fields"][MEAN_FIELD] = 999
        return original(table, ids)
    monkeypatch.setattr(client, "batch_get_records", read)
    result = run(client)
    assert result["concurrent_skips"] == 1
    assert client.data["creators"][0]["fields"][MEAN_FIELD] == 999
    assert client.data["creators"][0]["fields"][MEDIAN_FIELD] == 400


@pytest.mark.parametrize("values, expected", [([0, 10, 100, None], (110/3, 10)), ([], (None, None))])
def test_feishu_list_numeric_strings_and_batch_native_numbers(values, expected):
    """Real Feishu list and batch_get encode the same numeric field differently."""
    class MixedEncodingClient(Client):
        def get_all_records(self, table):
            rows = super().get_all_records(table)
            for row in rows:
                for name in ("Views", MEAN_FIELD, MEDIAN_FIELD):
                    value = row["fields"].get(name)
                    if value is not None:
                        row["fields"][name] = str(value)
            return rows

    client = MixedEncodingClient()
    client.data["videos"] = [{"record_id": f"v{i}", "fields": {
        "Channel ID": "UC1", "Video ID": str(i), "Views": value,
        "Publish Time": f"2026-10-{i+1:02}T00:00:00Z"}} for i, value in enumerate(values)]
    result = run(client, recent_source="table")
    assert not result["errors"]
    assert result["concurrent_skips"] == 0 and result["actual_updates"] == 1
    actual = client.data["creators"][0]["fields"]
    assert (actual[MEAN_FIELD], actual[MEDIAN_FIELD]) == expected


def test_metric_readback_accepts_numeric_string_but_not_invalid_or_boolean():
    assert mod._metric_equal("1234.50", 1234.5)
    assert not mod._metric_equal(True, 1)
    assert not mod._metric_equal("invalid", 0)
    assert not mod._metric_equal("Infinity", 1)
