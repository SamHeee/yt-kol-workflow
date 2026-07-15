#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube playlistItems.list API wrapper - get channel's recent uploads."""

import logging
from typing import List, Tuple

from .client import api_request

logger = logging.getLogger("kol_workflow.youtube.playlists")

YOUTUBE_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"


def channel_id_to_uploads_playlist(channel_id: str) -> str:
    """Convert a channel ID to its uploads playlist ID.

    Channel ID format: UC{22 chars}
    Uploads playlist: UU{22 chars}
    """
    if channel_id.startswith("UC"):
        return "UU" + channel_id[2:]
    return channel_id  # fallback


def get_channel_uploads(
    api_key: str,
    channel_id: str,
    uploads_playlist_id: str = "",
    max_results: int = 10,
    quota_tracker=None,
) -> Tuple[List[str], str]:
    """Get recent video IDs from a channel's uploads playlist.

    This costs only 1 unit (vs 100 units for search.list).
    Returns videos in reverse chronological order (newest first).

    Returns:
        (list_of_video_ids, error_message)
    """
    playlist_id = uploads_playlist_id or channel_id_to_uploads_playlist(channel_id)

    params = {
        "part": "contentDetails",
        "playlistId": playlist_id,
        "maxResults": min(max_results, 50),
        "key": api_key,
    }

    success, data, error = api_request(
        YOUTUBE_PLAYLIST_ITEMS_URL,
        params=params,
        quota_tracker=quota_tracker,
        api_name="playlistItems.list",
    )

    if not success:
        logger.warning(f"获取频道上传列表失败 ({channel_id}): {error}")
        return [], error

    video_ids = []
    for item in data.get("items", []):
        vid = item.get("contentDetails", {}).get("videoId", "")
        if vid:
            video_ids.append(vid)

    logger.debug(f"频道 {channel_id} 最近 {len(video_ids)} 个视频")
    return video_ids, ""
