#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase A: YouTube keyword search."""

import logging
from typing import List, Dict, Tuple

from youtube.search import search_videos

logger = logging.getLogger("kol_workflow.workflow.phase_a")


def run_phase_a(
    api_key: str,
    keyword: str,
    sort_order: str = "relevance",
    max_results: int = 100,
    region_code: str = "US",
    relevance_language: str = "en",
    search_filters: dict = None,
    quota_tracker=None,
) -> Tuple[List[Dict], str]:
    """Execute Phase A: search YouTube for videos.

    Returns:
        (search_results, error_message)
        Each result has: videoId, channelId, channelTitle, title, publishedAt, etc.
    """
    logger.info(f"═══ 阶段A: 搜索 '{keyword}' (排序={sort_order}, 数量={max_results}) ═══")

    results, error = search_videos(
        api_key=api_key,
        query=keyword,
        max_results=max_results,
        order=sort_order,
        region_code=region_code,
        relevance_language=relevance_language,
        search_filters=search_filters,
        quota_tracker=quota_tracker,
    )

    if error:
        logger.error(f"阶段A失败: {error}")
        return results, error

    logger.info(f"阶段A完成: 获取 {len(results)} 个搜索结果")
    return results, ""
