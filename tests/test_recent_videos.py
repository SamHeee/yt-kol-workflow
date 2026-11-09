import pytest
from youtube import recent_videos as mod
from youtube.quota import QuotaTracker


def test_pagination_fills_missing_video_preserves_upload_order_and_counts_quota(monkeypatch):
    calls = []
    def request(url, params, quota_tracker, api_name):
        quota_tracker.consume(api_name)
        calls.append(dict(params))
        if not params.get("pageToken"):
            return True, {"items": [{"contentDetails": {"videoId": v}} for v in ["a", "missing", "b"]], "nextPageToken": "next"}, ""
        return True, {"items": [{"contentDetails": {"videoId": v}} for v in ["b", "c", "d"]]}, ""
    def details(key, ids, quota_tracker):
        quota_tracker.consume("videos.list")
        # Deliberately reverse the response; playlist order is authoritative.
        return [{"video_id": v, "channel_id": "UC1"} for v in reversed(ids) if v != "missing"], ["missing"], ""
    monkeypatch.setattr(mod, "api_request", request)
    monkeypatch.setattr(mod, "fetch_video_details", details)
    quota = QuotaTracker(10)
    result, error = mod.collect_recent_videos("key", "UC1", count=3, quota_tracker=quota)
    assert not error
    assert [v["video_id"] for v in result] == ["a", "b", "c"]
    assert calls[1]["pageToken"] == "next"
    assert quota.used == 4


@pytest.mark.parametrize("mode", ["details-error", "owner", "unexpected-id", "duplicate-id", "loop", "quota"])
def test_incomplete_or_corrupt_sample_never_returns_partial_success(monkeypatch, mode):
    def request(*args, **kwargs):
        if kwargs.get("quota_tracker"):
            kwargs["quota_tracker"].consume("playlistItems.list")
        return True, {"items": [{"contentDetails": {"videoId": "a"}}], "nextPageToken": "same"}, ""
    def details(*args, **kwargs):
        if mode == "details-error":
            return [], [], "temporary error"
        row = {"video_id": "other" if mode == "unexpected-id" else "a", "channel_id": "UC2" if mode == "owner" else "UC1"}
        return ([row, row] if mode == "duplicate-id" else [row]), [], ""
    monkeypatch.setattr(mod, "api_request", request)
    monkeypatch.setattr(mod, "fetch_video_details", details)
    rows, error = mod.collect_recent_videos("key", "UC1", count=2,
                      quota_tracker=QuotaTracker(1) if mode == "quota" else None)
    assert rows == []
    assert error


def test_exhausted_playlist_returns_actual_small_sample_including_shorts(monkeypatch):
    monkeypatch.setattr(mod, "api_request", lambda *a, **kw: (True, {"items": [{"contentDetails": {"videoId": "short"}}]}, ""))
    monkeypatch.setattr(mod, "fetch_video_details", lambda *a, **kw: ([{"video_id": "short", "channel_id": "UC1", "duration_seconds": 15}], [], ""))
    rows, error = mod.collect_recent_videos("key", "UC1")
    assert not error and len(rows) == 1
