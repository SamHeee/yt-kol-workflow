#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Base HTTP client for YouTube API with retry logic and error handling.
"""

import time
import logging
from typing import Tuple, Dict, Optional

import requests

logger = logging.getLogger("kol_workflow.youtube.client")

# Timeout settings
TIMEOUT_CONNECT = 10
TIMEOUT_READ = 30


class APIError(Exception):
    """Custom API error with status code and reason."""
    def __init__(self, message: str, status_code: int = 0, reason: str = "", fatal: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.fatal = fatal  # If True, should stop the entire workflow


def _classify_error(response: requests.Response) -> APIError:
    """Classify HTTP error response into an APIError."""
    status = response.status_code
    try:
        data = response.json()
        error_info = data.get("error", {})
        errors_list = error_info.get("errors", [{}])
        reason = errors_list[0].get("reason", "unknown") if errors_list else "unknown"
        message = error_info.get("message", str(data))
    except Exception:
        reason = "unknown"
        message = response.text[:200]

    if status == 403:
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            return APIError(
                f"YouTube API 配额已耗尽，请明日再试 ({reason})",
                status_code=403, reason="quotaExceeded", fatal=True
            )
        return APIError(
            f"API Key 无效或权限不足: {reason}",
            status_code=403, reason=reason, fatal=True
        )
    elif status == 429:
        return APIError(
            "请求频率过高，需要等待",
            status_code=429, reason="rateLimited", fatal=False
        )
    elif status >= 500:
        return APIError(
            f"YouTube 服务端错误: HTTP {status}",
            status_code=status, reason="serverError", fatal=False
        )
    else:
        return APIError(
            f"HTTP {status}: {message}",
            status_code=status, reason=reason, fatal=False
        )


def api_request(
    url: str,
    params: Optional[Dict] = None,
    max_retries: int = 3,
    quota_tracker=None,
    api_name: str = "",
) -> Tuple[bool, Dict, str]:
    """Make a YouTube API GET request with retry logic.

    Returns:
        (success, data_dict, error_message)
    """
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=(TIMEOUT_CONNECT, TIMEOUT_READ),
            )

            if response.status_code == 200:
                # Track quota usage on success
                if quota_tracker and api_name:
                    quota_tracker.consume(api_name)
                return True, response.json(), ""

            # Classify the error
            error = _classify_error(response)

            if error.fatal:
                logger.error(f"致命错误: {error}")
                return False, {}, str(error)

            if error.status_code == 429:
                wait_time = 60
                logger.warning(f"限流，等待 {wait_time}s 后重试 ({attempt+1}/{max_retries})")
                time.sleep(wait_time)
                continue

            if error.status_code >= 500:
                wait_time = 5 * (2 ** attempt)
                logger.warning(f"服务端错误，{wait_time}s 后重试 ({attempt+1}/{max_retries})")
                time.sleep(wait_time)
                continue

            # Other errors, no retry
            return False, {}, str(error)

        except requests.exceptions.Timeout:
            wait_time = 1 * (2 ** attempt)  # 1s -> 2s -> 4s
            logger.warning(f"请求超时，{wait_time}s 后重试 ({attempt+1}/{max_retries})")
            time.sleep(wait_time)
            continue

        except requests.exceptions.ConnectionError:
            wait_time = 2 * (2 ** attempt)
            logger.warning(f"连接失败，{wait_time}s 后重试 ({attempt+1}/{max_retries})")
            time.sleep(wait_time)
            continue

        except Exception as e:
            logger.error(f"未知请求错误: {e}")
            return False, {}, str(e)

    return False, {}, f"重试 {max_retries} 次后仍然失败"
