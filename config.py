#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configuration management for KOL Workflow.
Reads from .env, CLI args, and brand_exclusions.json.
"""

import os
import json
import re
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field

# Project root directory (where config.py is located)
PROJECT_ROOT = Path(__file__).parent

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_bool(name: str, default: bool) -> bool:
    """Read a conventional boolean environment variable."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class YouTubeConfig:
    api_key: str = ""
    daily_quota: int = 10000

    def __post_init__(self):
        if not self.api_key:
            self.api_key = os.environ.get("YOUTUBE_API_KEY", "")


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    app_token: str = ""  # bitable app_token
    auth_mode: str = ""
    cli_profile: str = ""
    cli_path: str = ""
    auto_setup: Optional[bool] = None
    auto_install: Optional[bool] = None
    open_browser: Optional[bool] = None
    auth_timeout_seconds: int = 0
    base_name: str = ""

    def __post_init__(self):
        if not self.app_id:
            self.app_id = os.environ.get("FEISHU_APP_ID", "")
        if not self.app_secret:
            self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        if not self.app_token:
            self.app_token = os.environ.get("FEISHU_APP_TOKEN", "")
        if not self.auth_mode:
            self.auth_mode = os.environ.get("FEISHU_AUTH_MODE", "auto").strip().lower() or "auto"
        if self.auth_mode not in {"auto", "cli", "app"}:
            raise ValueError("FEISHU_AUTH_MODE 必须是 auto、cli 或 app")
        if not self.cli_profile:
            self.cli_profile = os.environ.get("FEISHU_CLI_PROFILE", "kol-workflow").strip() or "kol-workflow"
        if not self.cli_path:
            self.cli_path = os.environ.get("FEISHU_LARK_CLI_PATH", "").strip()
        if self.auto_setup is None:
            self.auto_setup = _env_bool("FEISHU_AUTO_SETUP", True)
        if self.auto_install is None:
            self.auto_install = _env_bool("FEISHU_AUTO_INSTALL", True)
        if self.open_browser is None:
            self.open_browser = _env_bool("FEISHU_OPEN_BROWSER", True)
        if not self.auth_timeout_seconds:
            try:
                self.auth_timeout_seconds = int(os.environ.get("FEISHU_AUTH_TIMEOUT_SECONDS", "900"))
            except ValueError:
                self.auth_timeout_seconds = 900
        if self.auth_timeout_seconds <= 0:
            raise ValueError("FEISHU_AUTH_TIMEOUT_SECONDS 必须大于 0")
        if not self.base_name:
            self.base_name = os.environ.get("FEISHU_BASE_NAME", "KOL网红开发工作流").strip() or "KOL网红开发工作流"

    @staticmethod
    def extract_app_token(url_or_token: str) -> str:
        """Extract app_token from a Feishu URL or return as-is if already a token."""
        if not url_or_token:
            return ""
        # Try to extract from URL pattern: /wiki/{app_token} or /base/{app_token}
        match = re.search(r'(?:wiki|base)/([a-zA-Z0-9]+)', url_or_token)
        if match:
            return match.group(1)
        # Already a plain token
        return url_or_token.strip()

    def create_auth(self) -> "FeishuAuth":
        """Create a FeishuAuth instance with this config's credentials.

        Returns:
            FeishuAuth instance with token caching support.
        """
        from feishu.auth import FeishuAuth
        return FeishuAuth(self.app_id, self.app_secret)

    def invalidate_auth(self):
        """Invalidate cached token, forcing re-auth on next call."""
        auth = self.create_auth()
        auth.invalidate()


@dataclass
class BrandExclusion:
    """Brand exclusion list for filtering out official brand channels."""
    brand_names: List[str] = field(default_factory=list)
    channel_ids: List[str] = field(default_factory=list)
    channel_name_keywords: List[str] = field(default_factory=list)

    def is_excluded(self, channel_id: str, channel_title: str) -> bool:
        """Check if a channel should be excluded."""
        if channel_id in self.channel_ids:
            return True
        title_lower = channel_title.lower()
        for keyword in self.channel_name_keywords:
            if keyword.lower() in title_lower:
                return True
        for brand in self.brand_names:
            if brand.lower() in title_lower:
                return True
        return False

    @classmethod
    def load(cls, filepath: str = "brand_exclusions.json") -> "BrandExclusion":
        """Load from JSON file."""
        path = Path(filepath)
        if not path.exists():
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(
                brand_names=data.get("brand_names", []),
                channel_ids=data.get("channel_ids", []),
                channel_name_keywords=data.get("channel_name_keywords", []),
            )
        except Exception:
            return cls()

    def save(self, filepath: str = "brand_exclusions.json"):
        """Save to JSON file."""
        data = {
            "brand_names": self.brand_names,
            "channel_ids": self.channel_ids,
            "channel_name_keywords": self.channel_name_keywords,
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add_brand(self, brand: str):
        if brand not in self.brand_names:
            self.brand_names.append(brand)

    def add_channel_id(self, channel_id: str):
        if channel_id not in self.channel_ids:
            self.channel_ids.append(channel_id)

    def add_keyword(self, keyword: str):
        if keyword not in self.channel_name_keywords:
            self.channel_name_keywords.append(keyword)


@dataclass
class WorkflowConfig:
    """Top-level configuration container."""
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    brand_exclusion: BrandExclusion = field(default_factory=BrandExclusion)
    output_dir: str = ""  # 空字符串表示使用项目目录下的 output
    log_level: str = "INFO"
    region: str = "US"
    lang: str = "en"
    max_results: int = 100
    min_views: int = 10000
    min_engagement: float = 3.0
    filter_mode: str = "or"
    min_subscribers: int = 0
    estimated_channels_per_keyword: int = 15
    seen_channels_file: str = ""
    search_filters: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        # 如果 output_dir 未设置，使用项目目录下的 output
        if not self.output_dir:
            self.output_dir = str(PROJECT_ROOT / "output")
        # 转换为绝对路径，确保目录一致性
        self.output_dir = str(Path(self.output_dir).resolve())
        if not self.seen_channels_file:
            self.seen_channels_file = str(PROJECT_ROOT / "seen_channels.json")
        self.seen_channels_file = str(Path(self.seen_channels_file).resolve())

    def validate(self, require_youtube: bool = True, require_feishu: bool = False) -> List[str]:
        """Validate configuration, return list of errors."""
        errors = []
        if require_youtube and not self.youtube.api_key:
            errors.append("YouTube API Key 未配置，请设置环境变量 YOUTUBE_API_KEY")
        if require_feishu:
            # CLI/auto mode can create an application, authorize the user and
            # create a Base when no target was supplied.  Only the legacy app
            # credential mode still has mandatory static configuration.
            if self.feishu.auth_mode == "app":
                if not self.feishu.app_id:
                    errors.append("飞书 App 模式缺少 FEISHU_APP_ID")
                if not self.feishu.app_secret:
                    errors.append("飞书 App 模式缺少 FEISHU_APP_SECRET")
                if not self.feishu.app_token:
                    errors.append("飞书 App 模式缺少 FEISHU_APP_TOKEN 或 --feishu-app-token")
        return errors


# ============================================================================
# Keyword file parsing
# ============================================================================

@dataclass
class KeywordTask:
    """A single keyword search task with its parameters."""
    keyword: str
    sort_order: str = ""  # empty means ask user
    max_results: int = 0  # 0 means use global default


def parse_keywords_file(filepath: str, default_max_results: int = 100) -> List[KeywordTask]:
    """Parse keywords from txt or csv file.

    txt format: one keyword per line
    csv format: keyword,sort_order,max_results
    """
    path = Path(filepath)
    tasks = []

    if path.suffix.lower() == ".csv":
        import csv
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                keyword = row.get("keyword", "").strip()
                if not keyword:
                    continue
                sort_order = row.get("sort_order", "").strip()
                max_results_str = row.get("max_results", "").strip()
                max_results = int(max_results_str) if max_results_str else default_max_results
                tasks.append(KeywordTask(
                    keyword=keyword,
                    sort_order=sort_order,
                    max_results=max_results,
                ))
    else:
        # txt: one keyword per line
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                keyword = line.strip()
                if keyword and not keyword.startswith("#"):
                    tasks.append(KeywordTask(
                        keyword=keyword,
                        max_results=default_max_results,
                    ))

    return tasks
