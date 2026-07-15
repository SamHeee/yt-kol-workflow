#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Video filtering: engagement rate, view count thresholds, brand exclusion."""

import logging
from typing import List, Dict, Tuple

from config import BrandExclusion

logger = logging.getLogger("kol_workflow.filter.video_filter")

# Default thresholds
MIN_VIEW_COUNT = 10000
MIN_ENGAGEMENT_RATE = 3.0  # percent


def filter_videos(
    videos: List[Dict],
    brand_exclusion: BrandExclusion,
    min_views: int = MIN_VIEW_COUNT,
    min_engagement: float = MIN_ENGAGEMENT_RATE,
    filter_mode: str = "or",
) -> Tuple[List[Dict], List[Dict]]:
    """Apply filtering rules to video list.

    Each video dict gets added fields:
        - is_qualified: bool
        - filter_reason: str

    Returns:
        (qualified_videos, all_videos_with_filter_status)
    """
    qualified = []

    for video in videos:
        channel_id = video.get("channel_id", "")
        channel_title = video.get("channel_title", "")
        view_count = video.get("view_count", 0)
        engagement_rate = video.get("engagement_rate", 0.0)

        # Rule 1: Brand exclusion
        if brand_exclusion.is_excluded(channel_id, channel_title):
            video["is_qualified"] = False
            video["filter_reason"] = "品牌官方频道"
            continue

        # Rule 2: View count or engagement rate
        passes_views = view_count > min_views
        passes_engagement = engagement_rate > min_engagement and view_count > 0

        filter_mode = (filter_mode or "or").lower()
        weighted_score = _weighted_score(view_count, engagement_rate, min_views, min_engagement)

        if filter_mode == "and":
            is_qualified = passes_views and passes_engagement
        elif filter_mode == "weighted":
            is_qualified = weighted_score >= 1.0
        else:
            is_qualified = passes_views or passes_engagement

        if is_qualified and passes_views and passes_engagement:
            video["is_qualified"] = True
            video["filter_reason"] = "双重达标"
            qualified.append(video)
        elif is_qualified and passes_views:
            video["is_qualified"] = True
            video["filter_reason"] = "播放量达标"
            qualified.append(video)
        elif is_qualified and passes_engagement:
            video["is_qualified"] = True
            video["filter_reason"] = "互动率达标"
            qualified.append(video)
        elif is_qualified:
            video["is_qualified"] = True
            video["filter_reason"] = f"加权达标(score={weighted_score:.2f})"
            qualified.append(video)
        else:
            video["is_qualified"] = False
            reasons = []
            if view_count <= min_views:
                reasons.append(f"播放量{view_count}<={min_views}")
            if view_count == 0:
                reasons.append("播放量为0")
            elif engagement_rate <= min_engagement:
                reasons.append(f"互动率{engagement_rate:.1f}%<={min_engagement}%")
            if filter_mode == "weighted":
                reasons.append(f"加权分{weighted_score:.2f}<1.00")
            video["filter_reason"] = "未达标: " + ", ".join(reasons)

    total = len(videos)
    q_count = len(qualified)
    brand_count = sum(1 for v in videos if v.get("filter_reason") == "品牌官方频道")

    logger.info(
        f"筛选结果: {q_count}/{total} 通过 "
        f"(品牌排除 {brand_count}, 未达标 {total - q_count - brand_count})"
    )

    return qualified, videos


def _weighted_score(
    view_count: int,
    engagement_rate: float,
    min_views: int,
    min_engagement: float,
) -> float:
    """Balanced score. 1.0 means both metrics jointly meet the target."""
    view_score = min(view_count / min_views, 1.0) if min_views > 0 else 1.0
    engagement_score = min(engagement_rate / min_engagement, 1.0) if min_engagement > 0 else 1.0
    return round(view_score * 0.5 + engagement_score * 0.5, 4)
