#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu Bitable schema definitions - 4 tables for KOL workflow.

Field types:
  1 = 文本 (text)
  2 = 数字 (number)
  3 = 单选 (single_select)
  5 = 日期 (date)
  7 = 复选框 (checkbox)
  15 = 链接 (url)
  1001 = 创建时间 (created_time, system managed)
"""

import logging
from typing import Dict
from .bitable import BitableClient

logger = logging.getLogger("kol_workflow.feishu.schema")

# ============================================================================
# Table definitions: (field_name, field_type)
# ============================================================================

# Every Feishu Bitable table is created with this primary text field.  Keep it
# in the canonical schema so downstream Excel exports can mirror the physical
# table exactly.  Record formatters intentionally leave it blank.
PRIMARY_FIELD_NAME = "多行文本"
PRIMARY_FIELD = (PRIMARY_FIELD_NAME, 1)

SEARCH_TASKS_TABLE = "搜索任务表"
SEARCH_TASKS_FIELDS = [
    PRIMARY_FIELD,
    ("搜索关键词", 1),
    ("排序策略", 3),
    ("地区", 1),
    ("搜索时间", 5),
    ("搜索结果数", 2),
    ("筛选通过数", 2),
    ("独立频道数", 2),
    ("新增网红数", 2),
    ("配额消耗", 2),
    ("执行状态", 3),
    ("备注", 1),
    ("唯一键", 1),
]

SEARCH_VIDEOS_TABLE = "视频数据表"
SEARCH_VIDEOS_FIELDS = [
    PRIMARY_FIELD,
    ("搜索关键词", 1),
    ("视频URL", 15),
    ("视频记录日期", 1001),
    ("Video ID", 1),
    ("Channel ID", 1),
    ("Channel Name", 1),
    ("Video Title", 1),
    ("Publish Time", 5),
    ("Views", 2),
    ("Likes", 2),
    ("Comments", 2),
    ("互动率(%)", 2),
    ("Duration (sec)", 2),
    ("Duration (H:M:S)", 1),
    ("Tags", 1),
    ("Has Subtitles", 1),
    ("是否通过筛选", 7),
    ("筛选原因", 1),
    ("唯一键", 1),
]

INFLUENCERS_TABLE = "网红详情表"
INFLUENCERS_FIELDS = [
    PRIMARY_FIELD,
    ("Channel ID", 1),
    ("Channel Name", 1),
    ("KOL Name", 1),
    ("网红记录日期", 1001),
    ("最新发布日期", 5),
    ("断更评估", 3),
    ("频道URL", 15),
    ("订阅数", 2),
    ("频道总播放量", 2),
    ("视频总数", 2),
    ("国家/地区", 1),
    ("频道创建日期", 5),
    ("频道描述", 1),
    ("频道初步判断", 1),
    ("联系邮箱", 1),
    ("邮箱状态", 3),
    ("代表视频URL", 15),
    ("代表视频标题", 1),
    ("代表视频播放量", 2),
    ("代表视频互动率", 2),
    ("来源关键词", 1),
    ("开发状态", 3),
    ("开发负责人", 1),
    ("备注", 1),
]

INFLUENCERS_FIELD_OPTIONS = {
    "断更评估": ["持续更新", "有断更风险", "待确认"],
}

INFLUENCER_VIDEOS_TABLE = "网红视频表"
INFLUENCER_VIDEOS_FIELDS = [
    PRIMARY_FIELD,
    ("Channel ID", 1),
    ("Channel Name", 1),
    ("视频URL", 15),
    ("Video ID", 1),
    ("Video Title", 1),
    ("Publish Time", 5),
    ("Views", 2),
    ("Likes", 2),
    ("Comments", 2),
    ("互动率(%)", 2),
    ("Duration (sec)", 2),
    ("Duration (H:M:S)", 1),
    ("Tags", 1),
    ("字幕内容", 1),
    ("唯一键", 1),
]


class SchemaManager:
    """Manages the 4-table schema in Feishu Bitable."""

    def __init__(self, client: BitableClient):
        self.client = client
        self.table_ids: Dict[str, str] = {}

    def ensure_all_tables(self) -> Dict[str, str]:
        """Create all 4 tables and their fields if they don't exist.

        Returns: {table_name: table_id}
        """
        tables = [
            (SEARCH_TASKS_TABLE, SEARCH_TASKS_FIELDS),
            (SEARCH_VIDEOS_TABLE, SEARCH_VIDEOS_FIELDS),
            (INFLUENCERS_TABLE, INFLUENCERS_FIELDS),
            (INFLUENCER_VIDEOS_TABLE, INFLUENCER_VIDEOS_FIELDS),
        ]

        for table_name, fields in tables:
            logger.info(f"检查数据表: {table_name}")
            table_id = self.client.get_or_create_table(table_name)
            self.table_ids[table_name] = table_id

            # Ensure fields
            self.client.ensure_fields(table_id, fields)
            if table_name == INFLUENCERS_TABLE:
                ensure_influencer_field_options(self.client, table_id)
            logger.info(f"数据表就绪: {table_name} -> {table_id}")

        return self.table_ids

    def get_table_id(self, table_name: str) -> str:
        """Get table_id by name, must call ensure_all_tables first."""
        tid = self.table_ids.get(table_name, "")
        if not tid:
            raise RuntimeError(f"数据表 '{table_name}' 未初始化，请先调用 ensure_all_tables()")
        return tid


def ensure_influencer_field_options(client: BitableClient, table_id: str) -> None:
    """Ensure controlled single-select options required by enrichment fields."""
    fields = {
        field.get("field_name", ""): field
        for field in client.list_fields(table_id)
        if field.get("field_name")
    }
    for field_name, required_names in INFLUENCERS_FIELD_OPTIONS.items():
        field = fields.get(field_name)
        if not field:
            continue
        property_data = dict(field.get("property") or {})
        options = list(property_data.get("options") or [])
        existing_names = {str(option.get("name", "")) for option in options}
        missing_names = [name for name in required_names if name not in existing_names]
        if not missing_names:
            continue
        start_color = len(options)
        options.extend(
            {"name": name, "color": (start_color + index) % 54}
            for index, name in enumerate(missing_names)
        )
        property_data["options"] = options
        client.update_field(
            table_id,
            field.get("field_id", ""),
            field_name,
            int(field.get("type", 3)),
            property_data,
        )


# ============================================================================
# Record formatting helpers - convert internal data dict to Feishu fields
# ============================================================================

def format_search_task_record(data: dict) -> dict:
    """Format a search task record for Feishu."""
    return {"fields": {
        "唯一键": str(data.get("task_key") or data.get("keyword", "")),
        "搜索关键词": str(data.get("keyword", "")),
        "排序策略": str(data.get("sort_order", "")),
        "地区": str(data.get("region", "")),
        "搜索时间": _ts(data.get("search_time")),
        "搜索结果数": data.get("result_count", 0),
        "筛选通过数": data.get("qualified_count", 0),
        "独立频道数": data.get("unique_channels", 0),
        "新增网红数": data.get("new_channels", 0),
        "配额消耗": data.get("quota_used", 0),
        "执行状态": str(data.get("status", "成功")),
        "备注": str(data.get("note", "")),
    }}


def format_search_video_record(video: dict, keyword: str = "") -> dict:
    """Format a search video record for Feishu."""
    video_id = str(video.get("video_id", ""))
    return {"fields": {
        "唯一键": f"{keyword}|{video_id}",
        "搜索关键词": keyword,
        "视频URL": _link(video.get("video_url", "")),
        "Video ID": video_id,
        "Channel ID": str(video.get("channel_id", "")),
        "Channel Name": str(video.get("channel_title", "")),
        "Video Title": str(video.get("title", "")),
        "Publish Time": _ts(video.get("published_at")),
        "Views": video.get("view_count", 0),
        "Likes": video.get("like_count", 0),
        "Comments": video.get("comment_count", 0),
        "互动率(%)": video.get("engagement_rate", 0.0),
        "Duration (sec)": video.get("duration_seconds", 0),
        "Duration (H:M:S)": str(video.get("duration_hms", "")),
        "Tags": str(video.get("tags", "")),
        "Has Subtitles": str(video.get("has_caption", "")),
        "是否通过筛选": bool(video.get("is_qualified", False)),
        "筛选原因": str(video.get("filter_reason", "")),
    }}


def format_influencer_record(channel: dict, rep_video: dict, keyword: str = "") -> dict:
    """Format an influencer record for Feishu."""
    channel_id = str(channel.get("channel_id", ""))
    fields = {
        "Channel ID": channel_id,
        "Channel Name": str(channel.get("channel_title", "")),
        "KOL Name": str(channel.get("kol_name", "") or ""),
    }

    # Empty dates must be omitted. Sending 0 makes Feishu display 1970-01-01.
    latest_published_at = _ts(channel.get("latest_published_at"))
    if latest_published_at:
        fields["最新发布日期"] = latest_published_at

    fields.update({
        "断更评估": str(channel.get("activity_status", "") or ""),
        "频道URL": _link(channel.get("channel_url", "")),
        "订阅数": channel.get("subscriber_count", 0),
        "频道总播放量": channel.get("total_view_count", 0),
        "视频总数": channel.get("total_video_count", 0),
        "国家/地区": str(channel.get("country", "")),
    })

    channel_created_at = _ts(channel.get("channel_created_at"))
    if channel_created_at:
        fields["频道创建日期"] = channel_created_at

    fields.update({
        "频道描述": _truncate(channel.get("channel_description", ""), 2000),
        "频道初步判断": str(channel.get("channel_initial_assessment", "") or ""),
        "联系邮箱": str(channel.get("contact_email", "") or ""),
        "邮箱状态": "已获取" if channel.get("contact_email") else "需手动查找",
        "代表视频URL": _link(rep_video.get("video_url", "")),
        "代表视频标题": str(
            channel.get("rep_video_title") or rep_video.get("title", "") or ""
        ),
        "代表视频播放量": rep_video.get("view_count", 0),
        "代表视频互动率": rep_video.get("engagement_rate", 0.0),
        "来源关键词": keyword,
        "开发状态": "待联系",
    })

    return {"fields": fields}


def format_influencer_video_record(video: dict, channel_title: str = "") -> dict:
    """Format an influencer video record for Feishu."""
    video_id = str(video.get("video_id", ""))
    return {"fields": {
        "唯一键": video_id,
        "Channel ID": str(video.get("channel_id", "")),
        "Channel Name": channel_title or str(video.get("channel_title", "")),
        "视频URL": _link(video.get("video_url", "")),
        "Video ID": video_id,
        "Video Title": str(video.get("title", "")),
        "Publish Time": _ts(video.get("published_at")),
        "Views": video.get("view_count", 0),
        "Likes": video.get("like_count", 0),
        "Comments": video.get("comment_count", 0),
        "互动率(%)": video.get("engagement_rate", 0.0),
        "Duration (sec)": video.get("duration_seconds", 0),
        "Duration (H:M:S)": str(video.get("duration_hms", "")),
        "Tags": str(video.get("tags", "")),
        "字幕内容": "",  # Phase 2
    }}


# ============================================================================
# Helpers
# ============================================================================

def _ts(val) -> int:
    """Convert date string to Feishu timestamp (milliseconds) or 0."""
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    try:
        from datetime import date, datetime, timezone

        if isinstance(val, datetime):
            dt = val
        elif isinstance(val, date):
            dt = datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
        elif isinstance(val, str):
            text = val.strip()
            if not text:
                return 0
            # YouTube timestamps use RFC3339. ``fromisoformat`` accepts the
            # equivalent explicit UTC offset across supported Python versions.
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        else:
            return 0

        # Legacy workflow values such as "2026-05-21 14:30:00" are UTC values
        # with the suffix removed. Treating them as local time caused an
        # eight-hour shift on Asia/Shanghai hosts.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        pass
    return 0


def _link(url: str) -> dict:
    """Format a URL for Feishu link field."""
    if not url:
        return {"text": "", "link": ""}
    return {"text": url, "link": url}


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max length."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
