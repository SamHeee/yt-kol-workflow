#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube API quota tracking and budget estimation."""

import logging
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger("kol_workflow.youtube.quota")

# YouTube API quota costs per endpoint
QUOTA_COSTS: Dict[str, int] = {
    "search.list": 100,
    "videos.list": 1,
    "channels.list": 1,
    "playlistItems.list": 1,
}


class QuotaTracker:
    """Track YouTube API quota usage within a session."""

    def __init__(self, daily_limit: int = 10000):
        self.daily_limit = daily_limit
        self.used = 0
        self.history: List[Dict] = []

    def consume(self, api_name: str, count: int = 1):
        """Record quota consumption."""
        cost = QUOTA_COSTS.get(api_name, 0) * count
        self.used += cost
        self.history.append({
            "api": api_name,
            "count": count,
            "cost": cost,
            "time": datetime.now().isoformat(),
        })
        logger.debug(f"配额消耗: {api_name} x{count} = {cost} units (累计 {self.used})")

    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used)

    def can_afford(self, api_name: str, count: int = 1) -> bool:
        cost = QUOTA_COSTS.get(api_name, 0) * count
        return self.remaining() >= cost

    def estimate_cost(self, api_name: str, count: int = 1) -> int:
        return QUOTA_COSTS.get(api_name, 0) * count

    def warn_if_low(self, threshold_pct: float = 0.2):
        remaining_pct = self.remaining() / self.daily_limit if self.daily_limit else 0
        if remaining_pct < threshold_pct:
            logger.warning(
                f"⚠️ 配额警告: 剩余 {self.remaining()} units "
                f"({remaining_pct*100:.0f}%)"
            )

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"配额使用: {self.used} / {self.daily_limit} units",
            f"剩余: {self.remaining()} units ({self.remaining()/self.daily_limit*100:.0f}%)",
        ]
        # Group by API
        api_totals: Dict[str, int] = {}
        for h in self.history:
            api_totals[h["api"]] = api_totals.get(h["api"], 0) + h["cost"]
        if api_totals:
            lines.append("明细:")
            for api, total in sorted(api_totals.items(), key=lambda x: -x[1]):
                lines.append(f"  {api}: {total} units")
        return "\n".join(lines)


def estimate_single_keyword(
    max_results: int = 100,
    estimated_channels: int = 15,
    use_playlist: bool = True,
    include_detail: bool = True,
) -> Dict[str, int]:
    """Estimate quota for a single keyword search task.

    Returns dict with per-step and total costs.
    """
    search_pages = (max_results + 49) // 50
    search_cost = search_pages * QUOTA_COSTS["search.list"]

    video_batches = (max_results + 49) // 50
    video_detail_cost = video_batches * QUOTA_COSTS["videos.list"]

    if not include_detail:
        total = search_cost + video_detail_cost
        return {
            "search": search_cost,
            "video_detail": video_detail_cost,
            "channel_detail": 0,
            "recent_videos": 0,
            "recent_video_detail": 0,
            "total": total,
        }

    channel_batches = (estimated_channels + 49) // 50
    channel_cost = channel_batches * QUOTA_COSTS["channels.list"]

    if use_playlist:
        recent_videos_cost = estimated_channels * QUOTA_COSTS["playlistItems.list"]
        recent_detail_batches = (estimated_channels * 10 + 49) // 50
        recent_detail_cost = recent_detail_batches * QUOTA_COSTS["videos.list"]
    else:
        recent_videos_cost = estimated_channels * QUOTA_COSTS["search.list"]
        recent_detail_batches = (estimated_channels * 10 + 49) // 50
        recent_detail_cost = recent_detail_batches * QUOTA_COSTS["videos.list"]

    total = search_cost + video_detail_cost + channel_cost + recent_videos_cost + recent_detail_cost

    return {
        "search": search_cost,
        "video_detail": video_detail_cost,
        "channel_detail": channel_cost,
        "recent_videos": recent_videos_cost,
        "recent_video_detail": recent_detail_cost,
        "total": total,
    }


def print_quota_estimate(
    keywords: list,
    max_results_list: list,
    estimated_channels_per_kw: int = 15,
    use_playlist: bool = True,
    daily_limit: int = 10000,
    include_detail: bool = True,
):
    """Print a formatted quota estimate for batch tasks."""
    total_cost = 0
    print("\n┌────────────────────────────────────────────────┐")
    print("│            批量任务配额预估                      │")
    print("├────────────────────────────────────────────────┤")

    for i, (kw, mr) in enumerate(zip(keywords, max_results_list)):
        est = estimate_single_keyword(mr, estimated_channels_per_kw, use_playlist, include_detail)
        print(f"│ 关键词 {i+1}: {kw[:40]}")
        print(f"│   搜索: {est['search']} units | 视频详情: {est['video_detail']} units")
        print(f"│   频道+最近视频: ~{est['channel_detail'] + est['recent_videos'] + est['recent_video_detail']} units")
        print(f"│   小计: ~{est['total']} units")
        print("│")
        total_cost += est["total"]

    status = "[OK]" if total_cost < daily_limit * 0.8 else (
        "[WARN]" if total_cost < daily_limit else "[FAIL]"
    )
    method = "playlistItems" if use_playlist else "search.list"
    print(f"│ 总计预估: ~{total_cost} units (方案: {method})")
    print(f"│ 每日配额: {daily_limit} units")
    print(f"│ 状态: {status}")
    print("└────────────────────────────────────────────────┘")

    return total_cost


def estimate_batch_cost(
    max_results_list: list,
    estimated_channels_per_kw: int = 15,
    use_playlist: bool = True,
    include_detail: bool = True,
) -> int:
    """Estimate total quota cost for a batch."""
    total = 0
    for max_results in max_results_list:
        total += estimate_single_keyword(
            max_results=max_results,
            estimated_channels=estimated_channels_per_kw,
            use_playlist=use_playlist,
            include_detail=include_detail,
        )["total"]
    return total


def assert_budget_available(
    estimated_cost: int,
    remaining: int,
    label: str = "本次任务",
):
    """Raise RuntimeError when a task cannot fit in the local quota budget."""
    if estimated_cost > remaining:
        raise RuntimeError(
            f"{label}预计消耗 {estimated_cost} units，当前本地预算剩余 {remaining} units，"
            "已停止以避免跑到中途耗尽配额"
        )
