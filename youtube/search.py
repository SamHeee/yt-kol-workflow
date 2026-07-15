#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube search.list API wrapper."""

import logging
from typing import List, Dict, Tuple, Optional

from .client import api_request

logger = logging.getLogger("kol_workflow.youtube.search")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


def search_videos(
    api_key: str,
    query: str,
    max_results: int = 100,
    order: str = "relevance",
    region_code: str = "US",
    relevance_language: str = "en",
    search_filters: Optional[Dict[str, str]] = None,
    quota_tracker=None,
) -> Tuple[List[Dict], str]:
    """Search YouTube for videos matching a query.

    Args:
        api_key: YouTube API key
        query: Search query string
        max_results: Maximum total results to fetch (will paginate if >50)
        order: Sort order - relevance, viewCount, date, rating
        region_code: ISO 3166-1 alpha-2 country code
        relevance_language: ISO 639-1 language code
        quota_tracker: QuotaTracker instance

    Returns:
        (list_of_search_results, error_message)
        Each result dict has: videoId, channelId, channelTitle, title,
        publishedAt, description, thumbnailUrl
    """
    all_results = []
    page_token = None
    fetched = 0

    while fetched < max_results:
        page_size = min(50, max_results - fetched)

        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": page_size,
            "order": order,
            "regionCode": region_code,
            "relevanceLanguage": relevance_language,
            "key": api_key,
        }
        if search_filters:
            params.update({k: v for k, v in search_filters.items() if v})
        if page_token:
            params["pageToken"] = page_token

        success, data, error = api_request(
            YOUTUBE_SEARCH_URL,
            params=params,
            quota_tracker=quota_tracker,
            api_name="search.list",
        )

        if not success:
            logger.error(f"搜索失败: {error}")
            return all_results, error

        items = data.get("items", [])
        for item in items:
            snippet = item.get("snippet", {})
            video_id = item.get("id", {}).get("videoId", "")
            if not video_id:
                continue

            result = {
                "videoId": video_id,
                "channelId": snippet.get("channelId", ""),
                "channelTitle": snippet.get("channelTitle", ""),
                "title": snippet.get("title", ""),
                "publishedAt": snippet.get("publishedAt", ""),
                "description": snippet.get("description", ""),
                "thumbnailUrl": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
            }
            all_results.append(result)

        fetched += len(items)
        page_token = data.get("nextPageToken")

        if not page_token or not items:
            break

        logger.info(f"搜索翻页: 已获取 {fetched}/{max_results}")

    logger.info(f"搜索完成: '{query}' 共获取 {len(all_results)} 条结果")
    return all_results, ""


def search_channel_videos(
    api_key: str,
    channel_id: str,
    max_results: int = 10,
    order: str = "date",
    quota_tracker=None,
) -> Tuple[List[str], str]:
    """Search for recent videos from a specific channel.

    This uses search.list (100 units/call). For quota-efficient alternative,
    use playlists.get_channel_uploads() instead.

    Returns:
        (list_of_video_ids, error_message)
    """
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "type": "video",
        "maxResults": min(max_results, 50),
        "order": order,
        "key": api_key,
    }

    success, data, error = api_request(
        YOUTUBE_SEARCH_URL,
        params=params,
        quota_tracker=quota_tracker,
        api_name="search.list",
    )

    if not success:
        return [], error

    video_ids = []
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId", "")
        if vid:
            video_ids.append(vid)

    return video_ids, ""
