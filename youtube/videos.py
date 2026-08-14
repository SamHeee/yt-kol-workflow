#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube videos.list API wrapper - fetch detailed video data."""

import re
import logging
from datetime import datetime
from typing import List, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .client import api_request

logger = logging.getLogger("kol_workflow.youtube.videos")

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
MAX_BATCH_SIZE = 50

# ISO 8601 duration
DURATION_PATTERN = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
}
YOUTUBE_NOCOOKIE_HOSTS = {
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}


def extract_video_id(value: str) -> str:
    """Return one validated YouTube video ID from a supported URL or raw ID.

    Supported URL shapes are ``watch?v=``, ``youtu.be/``, ``shorts/``,
    ``live/`` and ``embed/``.  Hosts are allow-listed so lookalike domains do
    not become API inputs.  A valid YouTube video ID is exactly 11 characters.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if VIDEO_ID_PATTERN.fullmatch(text):
        return text

    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""

    host = (parsed.hostname or "").lower()
    candidate = ""
    if host in YOUTU_BE_HOSTS:
        parts = [part for part in parsed.path.split("/") if part]
        candidate = parts[0] if len(parts) == 1 else ""
    elif host in YOUTUBE_HOSTS:
        if parsed.path.rstrip("/") == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
                candidate = parts[1]
    elif host in YOUTUBE_NOCOOKIE_HOSTS:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "embed":
            candidate = parts[1]

    return candidate if VIDEO_ID_PATTERN.fullmatch(candidate) else ""


def parse_duration(iso_duration: str) -> Tuple[int, str]:
    """Parse ISO 8601 duration to (seconds, HH:MM:SS)."""
    if not iso_duration:
        return 0, "00:00:00"
    match = DURATION_PATTERN.match(iso_duration)
    if not match:
        return 0, "00:00:00"
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    total = h * 3600 + m * 60 + s
    return total, f"{h:02d}:{m:02d}:{s:02d}"


def parse_published_at(iso_date: str) -> str:
    """Parse ISO date to 'YYYY-MM-DD HH:MM:SS'."""
    if not iso_date:
        return ""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_date


def fetch_video_details(
    api_key: str,
    video_ids: List[str],
    quota_tracker=None,
) -> Tuple[List[Dict], List[str], str]:
    """Fetch detailed data for a list of video IDs.

    Args:
        api_key: YouTube API key
        video_ids: List of video IDs (will be batched at 50)
        quota_tracker: QuotaTracker instance

    Returns:
        (video_data_list, missing_ids, error_message)
    """
    all_videos = []
    all_requested = set(video_ids)

    for i in range(0, len(video_ids), MAX_BATCH_SIZE):
        batch_ids = video_ids[i:i + MAX_BATCH_SIZE]
        ids_param = ",".join(batch_ids)

        params = {
            "part": "snippet,statistics,contentDetails",
            "id": ids_param,
            "key": api_key,
        }

        success, data, error = api_request(
            YOUTUBE_VIDEOS_URL,
            params=params,
            quota_tracker=quota_tracker,
            api_name="videos.list",
        )

        if not success:
            logger.error(f"视频详情获取失败 (batch {i//MAX_BATCH_SIZE + 1}): {error}")
            return all_videos, list(all_requested - {v["video_id"] for v in all_videos}), error

        for item in data.get("items", []):
            video = _parse_video_item(item)
            all_videos.append(video)

        batch_num = i // MAX_BATCH_SIZE + 1
        total_batches = (len(video_ids) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE
        logger.debug(f"视频详情 batch {batch_num}/{total_batches}: 获取 {len(data.get('items', []))} 条")

    found_ids = {v["video_id"] for v in all_videos}
    missing_ids = [vid for vid in video_ids if vid not in found_ids]

    if missing_ids:
        logger.info(f"缺失视频ID: {len(missing_ids)} 个 (已删除/私有/不存在)")

    return all_videos, missing_ids, ""


def _parse_video_item(item: Dict) -> Dict:
    """Parse a single video item from API response."""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    content = item.get("contentDetails", {})

    duration_sec, duration_hms = parse_duration(content.get("duration", ""))

    view_count = _safe_int(stats.get("viewCount", 0))
    like_count = _safe_int(stats.get("likeCount", 0))
    comment_count = _safe_int(stats.get("commentCount", 0))
    view_count_available = "viewCount" in stats
    like_count_available = "likeCount" in stats
    comment_count_available = "commentCount" in stats

    # Calculate engagement rate
    engagement_rate = 0.0
    if view_count > 0:
        engagement_rate = round((like_count + comment_count) / view_count * 100, 2)

    video_id = item.get("id", "")
    video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

    # Keep YouTube's RFC3339 timestamp verbatim for timezone-aware comparisons.
    # ``published_at`` remains unchanged for existing Excel/Feishu consumers.
    published_at_raw = snippet.get("publishedAt", "")

    return {
        "video_id": video_id,
        "video_url": video_url,
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "title": snippet.get("title", ""),
        "published_at": parse_published_at(published_at_raw),
        "published_at_raw": published_at_raw,
        "live_broadcast_content": snippet.get("liveBroadcastContent", "none"),
        "tags": ",".join(snippet.get("tags", [])),
        "view_count": view_count,
        "like_count": like_count,
        "comment_count": comment_count,
        "view_count_available": view_count_available,
        "like_count_available": like_count_available,
        "comment_count_available": comment_count_available,
        "engagement_rate": engagement_rate,
        "duration_seconds": duration_sec,
        "duration_hms": duration_hms,
        "has_caption": content.get("caption", "false"),
    }


def _safe_int(val) -> int:
    """Safely convert to int."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
