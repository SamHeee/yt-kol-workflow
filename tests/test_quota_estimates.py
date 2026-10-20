from youtube.quota import estimate_batch_cost, estimate_channel_detail_cost, estimate_single_keyword


def test_phase_d_estimate_counts_separate_recent_detail_request_per_channel():
    detail = estimate_channel_detail_cost(15)
    assert detail == {"channel_detail": 1, "recent_videos": 15, "recent_video_detail": 15, "total": 31}
    estimate = estimate_single_keyword(100, estimated_channels=15)
    assert estimate["recent_video_detail"] == 15
    assert estimate["total"] == 233


def test_search_method_cost_and_channel_batches_are_consistent():
    detail = estimate_channel_detail_cost(51, use_playlist=False)
    assert detail["channel_detail"] == 2
    assert detail["recent_videos"] == 5100
    assert detail["recent_video_detail"] == 51
    assert detail["total"] == 5153


def test_zero_channels_and_no_detail_do_not_charge_for_detail_calls():
    assert estimate_channel_detail_cost(0)["total"] == 0
    assert estimate_single_keyword(100, include_detail=False)["total"] == 202
    assert estimate_batch_cost([100, 100], estimated_channels_per_kw=15) == 466


def test_phase_d_runtime_request_count_matches_estimate(monkeypatch):
    from workflow import phase_d_detail

    calls = {"channel_detail": 0, "recent_videos": 0, "recent_video_detail": 0}

    def channels(**kwargs):
        calls["channel_detail"] += 1
        return [{"channel_id": channel_id, "channel_title": "Creator"} for channel_id in kwargs["channel_ids"]], ""

    def uploads(**kwargs):
        calls["recent_videos"] += 1
        return ["abcdefghijk"], ""

    def videos(**kwargs):
        calls["recent_video_detail"] += 1
        return [{"video_id": "abcdefghijk", "published_at_raw": "2026-10-01T00:00:00Z"}], [], ""

    monkeypatch.setattr(phase_d_detail, "fetch_channel_details", channels)
    monkeypatch.setattr(phase_d_detail, "get_channel_uploads", uploads)
    monkeypatch.setattr(phase_d_detail, "fetch_video_details", videos)
    _, _, error = phase_d_detail.run_phase_d("key", [{"channel_id": f"UC{i}"} for i in range(15)])
    assert error == ""
    estimated = estimate_channel_detail_cost(15)
    assert {key: estimated[key] for key in calls} == calls
