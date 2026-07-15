#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube channels.list API wrapper - fetch channel details."""

import logging
from typing import List, Dict, Tuple

from .client import api_request
from .videos import parse_published_at, _safe_int

logger = logging.getLogger("kol_workflow.youtube.channels")

YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
MAX_BATCH_SIZE = 50


def fetch_channel_details(
    api_key: str,
    channel_ids: List[str],
    quota_tracker=None,
) -> Tuple[List[Dict], str]:
    """Fetch detailed data for a list of channel IDs.

    Returns:
        (channel_data_list, error_message)
    """
    all_channels = []

    for i in range(0, len(channel_ids), MAX_BATCH_SIZE):
        batch_ids = channel_ids[i:i + MAX_BATCH_SIZE]
        ids_param = ",".join(batch_ids)

        params = {
            "part": "snippet,statistics,brandingSettings,contentDetails",
            "id": ids_param,
            "key": api_key,
        }

        success, data, error = api_request(
            YOUTUBE_CHANNELS_URL,
            params=params,
            quota_tracker=quota_tracker,
            api_name="channels.list",
        )

        if not success:
            logger.error(f"频道详情获取失败: {error}")
            return all_channels, error

        for item in data.get("items", []):
            channel = _parse_channel_item(item)
            all_channels.append(channel)

        logger.debug(
            f"频道详情 batch {i//MAX_BATCH_SIZE+1}: "
            f"获取 {len(data.get('items', []))} 个频道"
        )

    logger.info(f"频道详情获取完成: {len(all_channels)} 个频道")
    return all_channels, ""


def _parse_channel_item(item: Dict) -> Dict:
    """Parse a single channel item from API response."""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    branding = item.get("brandingSettings", {}).get("channel", {})
    content = item.get("contentDetails", {})
    related = content.get("relatedPlaylists", {})

    channel_id = item.get("id", "")
    description = snippet.get("description", "") or branding.get("description", "")

    return {
        "channel_id": channel_id,
        "channel_title": snippet.get("title", ""),
        "channel_url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else "",
        "channel_description": description,
        "subscriber_count": _safe_int(stats.get("subscriberCount", 0)),
        "total_video_count": _safe_int(stats.get("videoCount", 0)),
        "total_view_count": _safe_int(stats.get("viewCount", 0)),
        "channel_created_at": parse_published_at(snippet.get("publishedAt", "")),
        "country": snippet.get("country", ""),
        "channel_thumbnail": (
            snippet.get("thumbnails", {}).get("default", {}).get("url", "")
        ),
        "uploads_playlist_id": related.get("uploads", ""),
    }
