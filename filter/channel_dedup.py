#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Channel deduplication: group by channelId, pick best representative video."""

import logging
from typing import List, Dict, Tuple, Set
from collections import defaultdict

logger = logging.getLogger("kol_workflow.filter.channel_dedup")


def deduplicate_channels(
    qualified_videos: List[Dict],
    existing_channel_ids: Set[str] = None,
    source_keyword: str = "",
) -> Tuple[List[Dict], List[Dict]]:
    """Group qualified videos by channel, pick best representative.

    Args:
        qualified_videos: Videos that passed filtering
        existing_channel_ids: Channel IDs already in Feishu (for incremental check)
        source_keyword: The search keyword that produced these results

    Returns:
        (new_channels, existing_channels)
        Each channel dict has: channel_id, channel_title, representative_video,
        video_count_in_search, source_keyword
    """
    if existing_channel_ids is None:
        existing_channel_ids = set()

    # Group by channelId
    channel_groups: Dict[str, List[Dict]] = defaultdict(list)
    for video in qualified_videos:
        cid = video.get("channel_id", "")
        if cid:
            channel_groups[cid].append(video)

    new_channels = []
    existing_channels = []

    for channel_id, videos in channel_groups.items():
        # Pick the video with highest view count as representative
        best_video = max(videos, key=lambda v: v.get("view_count", 0))

        channel_info = {
            "channel_id": channel_id,
            "channel_title": best_video.get("channel_title", ""),
            "representative_video": best_video,
            "video_count_in_search": len(videos),
            "source_keyword": source_keyword,
        }

        if channel_id in existing_channel_ids:
            existing_channels.append(channel_info)
        else:
            new_channels.append(channel_info)

    # Sort new channels by representative video view count (descending)
    new_channels.sort(
        key=lambda c: c["representative_video"].get("view_count", 0),
        reverse=True,
    )

    logger.info(
        f"频道去重: {len(channel_groups)} 个独立频道, "
        f"新增 {len(new_channels)}, 已有 {len(existing_channels)}"
    )

    return new_channels, existing_channels
