#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu API authentication - tenant access token management."""

import json
import os
import time
import logging
from pathlib import Path
from typing import Tuple, Optional

import requests

logger = logging.getLogger("kol_workflow.feishu.auth")

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

# 全局缓存目录
_CACHE_DIR = Path.home() / ".kol_workflow"


def _ensure_cache_dir():
    _CACHE_DIR.mkdir(exist_ok=True)


class FeishuAuth:
    """Manages Feishu tenant access token with auto-refresh and file caching.

    Args:
        app_id: Feishu app ID. If empty, reads from environment variable.
        app_secret: Feishu app secret. If empty, reads from environment variable.
    """

    def __init__(self, app_id: str = "", app_secret: str = ""):
        self._resolve_credentials(app_id, app_secret)
        self._token: Optional[str] = None
        self._expires_at: float = 0

    def _resolve_credentials(self, app_id: str, app_secret: str):
        """Resolve credentials from args or the single Feishu environment variable set."""
        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")

    def _token_cache_path(self) -> Path:
        """Get path to token cache file for this app_id."""
        _ensure_cache_dir()
        # Sanitize app_id for filesystem safety
        safe_id = self.app_id.replace("/", "_").replace("\\", "_")
        return _CACHE_DIR / f".feishu_token_{safe_id}"

    def _load_cached_token(self) -> Tuple[Optional[str], float]:
        """Load token and expiry from cache file. Returns (token, expires_at)."""
        cache_file = self._token_cache_path()
        if not cache_file.exists():
            return None, 0
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            token = data.get("token", "")
            expires_at = data.get("expires_at", 0)
            if token and expires_at > 0:
                return token, expires_at
        except (json.JSONDecodeError, IOError) as e:
            logger.debug(f"Failed to load cached token: {e}")
        return None, 0

    def _save_token_cache(self, token: str, expires_at: float):
        """Save token and expiry to cache file."""
        cache_file = self._token_cache_path()
        try:
            cache_file.write_text(
                json.dumps({"token": token, "expires_at": expires_at}, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug(f"Token cached to {cache_file}")
        except IOError as e:
            logger.warning(f"Failed to cache token: {e}")

    def _clear_token_cache(self):
        """Remove cached token file."""
        cache_file = self._token_cache_path()
        if cache_file.exists():
            try:
                cache_file.unlink()
                logger.debug(f"Token cache cleared")
            except IOError as e:
                logger.warning(f"Failed to clear token cache: {e}")

    def get_token(self) -> str:
        """Get a valid tenant access token, refreshing if needed.

        Uses file-based caching to persist token across process restarts.
        Raises RuntimeError on auth failure.
        """
        # Check memory cache first
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        # Try to load from file cache
        cached_token, cached_expires_at = self._load_cached_token()
        if cached_token and time.time() < cached_expires_at - 60:
            self._token = cached_token
            self._expires_at = cached_expires_at
            logger.debug("Using cached token from file")
            return self._token

        # Need to refresh token
        url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
        data = {"app_id": self.app_id, "app_secret": self.app_secret}

        try:
            resp = requests.post(url, json=data, timeout=30)
            result = resp.json()
        except Exception as e:
            raise RuntimeError(f"飞书认证请求失败: {e}")

        if result.get("code") != 0:
            raise RuntimeError(
                f"飞书认证失败: {result.get('msg', 'Unknown error')} "
                f"(code={result.get('code')})"
            )

        self._token = result["tenant_access_token"]
        self._expires_at = time.time() + result.get("expire", 7200)

        # Persist to file cache
        self._save_token_cache(self._token, self._expires_at)

        logger.info("飞书认证成功")
        return self._token

    def get_headers(self) -> dict:
        """Get HTTP headers with valid auth token."""
        token = self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def invalidate(self):
        """Clear in-memory and file-cached token, forcing re-auth on next call."""
        self._token = None
        self._expires_at = 0
        self._clear_token_cache()
        logger.info("飞书认证已失效，下次调用将重新认证")
