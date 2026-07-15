#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase B: Fetch video details and apply filtering."""

import logging
from typing import List, Dict, Tuple

from youtube.videos import fetch_video_details
from filter.video_filter import MIN_ENGAGEMENT_RATE, MIN_VIEW_COUNT, filter_videos
from config import BrandExclusion

logger = logging.getLogger("kol_workflow.workflow.phase_b")


def run_phase_b(
    api_key: str,
    search_results: List[Dict],
    brand_exclusion: BrandExclusion,
    min_views: int = MIN_VIEW_COUNT,
    min_engagement: float = MIN_ENGAGEMENT_RATE,
    filter_mode: str = "or",
    quota_tracker=None,
) -> Tuple[List[Dict], List[Dict], List[str], str]:
    """Execute Phase B: fetch video details and filter.

    Args:
        search_results: Output from Phase A (list of dicts with videoId)

    Returns:
        (qualified_videos, all_videos_with_status, missing_ids, error_message)
    """
    logger.info(f"═══ 阶段B: 提取视频数据 + 筛选 ({len(search_results)} 个视频) ═══")

    # Extract video IDs from search results
    video_ids = [r["videoId"] for r in search_results if r.get("videoId")]
    if not video_ids:
        return [], [], [], "无有效视频ID"

    # Deduplicate
    seen = set()
    unique_ids = []
    for vid in video_ids:
        if vid not in seen:
            seen.add(vid)
            unique_ids.append(vid)

    logger.info(f"去重后视频ID: {len(unique_ids)} 个 (去重 {len(video_ids) - len(unique_ids)} 个)")

    # Fetch detailed data via videos.list
    videos, missing_ids, error = fetch_video_details(
        api_key=api_key,
        video_ids=unique_ids,
        quota_tracker=quota_tracker,
    )

    if error:
        logger.error(f"视频详情获取失败: {error}")
        return [], videos, missing_ids, error

    logger.info(f"视频详情获取成功: {len(videos)} 条 (缺失 {len(missing_ids)} 条)")

    # Apply filtering
    qualified, all_with_status = filter_videos(
        videos=videos,
        brand_exclusion=brand_exclusion,
        min_views=min_views,
        min_engagement=min_engagement,
        filter_mode=filter_mode,
    )

    logger.info(f"阶段B完成: {len(qualified)}/{len(videos)} 通过筛选")
    return qualified, all_with_status, missing_ids, ""
