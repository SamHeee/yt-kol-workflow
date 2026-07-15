#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluate whether a YouTube channel has published recently."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence


ACTIVE_STATUS = "持续更新"
AT_RISK_STATUS = "有断更风险"
PENDING_STATUS = "待确认"
ACTIVITY_WINDOW_DAYS = 30


def parse_rfc3339_utc(value: str) -> Optional[datetime]:
    """Parse an RFC3339 timestamp and normalize it to an aware UTC datetime.

    ``None`` is returned for empty, malformed, or timezone-less values. YouTube's
    ``snippet.publishedAt`` always includes a timezone, so accepting a naive value
    here could silently shift the 30-day boundary.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def evaluate_channel_activity(
    videos: Optional[Sequence[Mapping[str, Any]]],
    *,
    fetch_error: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Return the latest publication date and a three-state activity assessment.

    The 30-day window is rolling and evaluated in UTC. A publication exactly 30
    days old is still active. Upcoming broadcasts are excluded because their
    ``publishedAt`` value does not represent an already-published video.

    Returns a dict with stable keys:

    - ``latest_published_at``: UTC ``YYYY-MM-DD HH:MM:SS`` or an empty string.
    - ``activity_status``: ``持续更新``, ``有断更风险``, or ``待确认``.
    - ``error``: an empty string on a conclusive result, otherwise the reason.

    A successful fetch with no published videos is conclusive and at risk. API
    errors, invalid input, and publication timestamp parse failures are pending;
    they never fabricate a latest publication date.
    """
    if fetch_error:
        return _pending(str(fetch_error))

    if videos is None:
        return _pending("video fetch result is missing")

    now_utc = _normalize_now(now)
    if now_utc is None:
        return _pending("invalid evaluation time")

    published_times = []
    for index, video in enumerate(videos):
        if not isinstance(video, Mapping):
            return _pending(f"video item {index} is invalid")

        broadcast_state = str(
            video.get(
                "live_broadcast_content",
                video.get("liveBroadcastContent", "none"),
            )
            or "none"
        ).strip().lower()
        if broadcast_state == "upcoming":
            continue

        raw_value = video.get("published_at_raw", "")
        published_at = parse_rfc3339_utc(raw_value)

        # Backward compatibility for previously persisted video dictionaries.
        # The legacy formatter was derived from a UTC RFC3339 timestamp before
        # dropping its timezone, so it is safe to interpret as UTC here.
        if published_at is None and not raw_value:
            published_at = _parse_legacy_published_at(video.get("published_at", ""))

        if published_at is None:
            return _pending(f"video item {index} has an invalid publication time")
        published_times.append(published_at)

    if not published_times:
        return {
            "latest_published_at": "",
            "activity_status": AT_RISK_STATUS,
            "error": "",
        }

    latest = max(published_times)
    cutoff = now_utc - timedelta(days=ACTIVITY_WINDOW_DAYS)
    status = ACTIVE_STATUS if latest >= cutoff else AT_RISK_STATUS

    return {
        "latest_published_at": latest.strftime("%Y-%m-%d %H:%M:%S"),
        "activity_status": status,
        "error": "",
    }


def _normalize_now(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        # ``now`` is a testing/integration hook; document and treat naive values
        # as UTC rather than applying the host machine's local timezone.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_legacy_published_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _pending(error: str) -> Dict[str, str]:
    return {
        "latest_published_at": "",
        "activity_status": PENDING_STATUS,
        "error": error,
    }
