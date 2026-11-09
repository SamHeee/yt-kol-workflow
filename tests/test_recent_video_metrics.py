from datetime import datetime, timezone

import pytest

from workflow.recent_video_metrics import (
    calculate_recent_metrics, select_table_videos, METRIC_FIELDS, metric_fields,
)


def video(vid, views, **extra):
    return {"video_id": vid, "channel_id": "UC1", "view_count": views, **extra}


def test_real_zero_missing_and_duplicates_do_not_bias_mean_or_median():
    rows = [video("a", 0), video("b", 10), video("b", 20), video("c", 100),
            video("d", 0, view_count_available=False), video("e", None), video("f", float("nan")),
            video("g", -1), video("h", True)]
    result = calculate_recent_metrics(rows, channel_id="UC1")
    assert result["recent_video_sample_count"] == 3
    assert metric_fields(result) == dict(zip(METRIC_FIELDS, (40, 20)))


@pytest.mark.parametrize("values, expected", [([], (None, None)), ([0], (0, 0)),
                        ([1, 2, 3, 4], (2.5, 2.5)), ([1, 2, 100], (103/3, 2))])
def test_empty_small_even_and_skewed_samples(values, expected):
    result = calculate_recent_metrics([video(str(i), v) for i, v in enumerate(values)], channel_id="UC1")
    assert tuple(metric_fields(result).values()) == expected


def test_wrong_channel_cannot_enter_statistics():
    with pytest.raises(ValueError, match="频道"):
        calculate_recent_metrics([video("a", 1, channel_id="UC2")], channel_id="UC1")


def test_table_selects_newest_ten_after_dedupe_not_ten_highest_views():
    rows = [video(str(i), 100-i, published_at=f"2026-01-{i+1:02}T00:00:00Z") for i in range(12)]
    rows.append(video("11", 999, published_at="2026-01-12T00:00:00Z"))
    selected = select_table_videos(rows, "UC1")
    assert [v["video_id"] for v in selected] == [str(i) for i in range(11, 1, -1)]
    assert selected[0]["view_count"] == 999


def test_table_missing_date_is_reported_instead_of_guessing_recency():
    with pytest.raises(ValueError, match="发布时间"):
        select_table_videos([video("a", 1)], "UC1")


def test_table_compares_feishu_milliseconds_and_iso_with_offsets():
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()*1000
    rows = [video("old", 10, published_at=timestamp),
            video("new", 20, published_at="2026-01-01T09:00:00+08:00")]
    assert select_table_videos(rows, "UC1", 1)[0]["video_id"] == "new"


def test_feishu_table_strings_zero_duplicates_and_invalid_values():
    from workflow.recent_video_store import table_videos
    rows = [{"fields": {"Video ID": vid, "Channel ID": "UC1", "Views": value}}
            for vid, value in [("a", "0"), ("b", "10"), ("b", "20"), ("c", "100"),
                               ("d", None), ("e", "invalid"), ("f", True), ("g", "NaN"), ("h", "-5")]]
    result = calculate_recent_metrics(table_videos(rows), channel_id="UC1")
    assert result["recent_video_sample_count"] == 3
    assert tuple(metric_fields(result).values()) == (40, 20)
