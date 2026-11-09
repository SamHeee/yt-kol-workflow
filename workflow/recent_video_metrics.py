"""Recent-upload snapshot statistics shared by discovery and maintenance."""

from datetime import datetime, timezone
import math
from statistics import mean, median

MEAN_FIELD = "最近视频平均播放量"
MEDIAN_FIELD = "最近视频中位播放量"
METRIC_FIELDS = (MEAN_FIELD, MEDIAN_FIELD)
METRIC_KEYS = ("recent_video_mean_views", "recent_video_median_views")


def valid_views(video):
    value = video.get("view_count")
    if video.get("view_count_available") is False:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def publication_time(video):
    value = video.get("published_at_raw") or video.get("published_at")
    try:
        if isinstance(value, (float, int)):
            return float(value) / 1000 if math.isfinite(value) else None  # Feishu milliseconds
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def select_table_videos(videos, channel_id, count=10):
    """Select the newest unique videos; legacy duplicate observations use max."""
    unique = {}
    for video in videos:
        if video.get("channel_id") != channel_id or not video.get("video_id"):
            continue
        if publication_time(video) is None:
            raise ValueError("视频缺少有效发布时间，无法确定最近视频")
        vid = video["video_id"]
        previous = unique.get(vid)
        current = dict(video)
        if previous:
            values = [v for v in (valid_views(previous), valid_views(current)) if v is not None]
            current["view_count"] = max(values) if values else None
            current["view_count_available"] = bool(values)
            if publication_time(previous) > publication_time(current):
                current["published_at_raw"] = previous.get("published_at_raw") or previous.get("published_at")
        unique[vid] = current
    return sorted(unique.values(), key=lambda v: (publication_time(v), v["video_id"]), reverse=True)[:count]


def calculate_recent_metrics(videos, *, channel_id, observed_at=None):
    unique = {}
    for video in videos:
        if video.get("channel_id") != channel_id:
            raise ValueError("视频所属频道不一致")
        vid = video.get("video_id")
        if not vid:
            raise ValueError("视频缺少 Video ID")
        value = valid_views(video)
        if vid not in unique or (value is not None and (unique[vid] is None or value > unique[vid])):
            unique[vid] = value
    values = [value for value in unique.values() if value is not None]
    return {
        METRIC_KEYS[0]: mean(values) if values else None,
        METRIC_KEYS[1]: median(values) if values else None,
        "recent_video_sample_count": len(values),
        "recent_video_count": len(unique),
        "recent_video_ids": list(unique),
        "recent_video_observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        "recent_video_metrics_status": "success",
    }


def metric_fields(snapshot):
    return dict(zip(METRIC_FIELDS, (snapshot[key] for key in METRIC_KEYS)))
