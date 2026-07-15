#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared value conversion helpers for Feishu Bitable sync tools."""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

import pandas as pd


def is_blank(value: Any) -> bool:
    """Return True for empty values returned by pandas or Feishu."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list):
        return len(value) == 0
    if isinstance(value, dict):
        return all(is_blank(v) for v in value.values())
    return False


def clean_text(value: Any) -> str:
    """Convert a spreadsheet/Feishu value into a trimmed plain string."""
    if is_blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_text(value: Any) -> str:
    """Normalize text for stable comparisons after Feishu round-trips."""
    return clean_text(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def field_to_text(value: Any) -> str:
    """Extract readable text from Feishu field values."""
    if is_blank(value):
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(clean_text(item.get("text") or item.get("name") or item.get("link")))
            else:
                parts.append(clean_text(item))
        return "".join(parts).strip()
    if isinstance(value, dict):
        return clean_text(value.get("text") or value.get("name") or value.get("link"))
    return clean_text(value)


def number_value(value: Any) -> Optional[float]:
    """Parse a value as an int/float suitable for Feishu number fields."""
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return value
    text = clean_text(value).replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed.is_integer():
        return int(parsed)
    return parsed


def bool_value(value: Any) -> Optional[bool]:
    """Parse common English/Chinese truthy and falsy values."""
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = clean_text(value).lower()
    if text in {"true", "yes", "y", "1", "是", "有", "通过"}:
        return True
    if text in {"false", "no", "n", "0", "否", "无", "未通过"}:
        return False
    return bool(text)


def timestamp_ms(value: Any) -> Optional[int]:
    """Parse a date-like value into a millisecond timestamp."""
    if is_blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 10_000_000_000:
            return int(value)
        try:
            dt = pd.to_datetime(value, unit="D", origin="1899-12-30")
            if getattr(dt, "tzinfo", None) is None:
                dt = dt.tz_localize("UTC")
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    try:
        dt = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(dt):
        return None
    # Workflow date strings originate from YouTube RFC3339 UTC timestamps.
    # Older Excel files dropped the suffix, so interpret naive values as UTC
    # instead of the host timezone (Asia/Shanghai would otherwise shift 8h).
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.tz_localize("UTC")
    else:
        dt = dt.tz_convert("UTC")
    return int(dt.timestamp() * 1000)


def url_value(value: Any) -> Optional[Dict[str, str]]:
    """Format a value for Feishu URL fields."""
    text = clean_text(value)
    if not text:
        return None
    return {"text": text, "link": text}


def format_value(value: Any, target_field: str, field_meta: Dict[str, Any]) -> Any:
    """Format a source value according to the target Feishu field type."""
    field_type = field_meta.get("type")
    ui_type = field_meta.get("ui_type")

    if field_type == 15 or ui_type == "Url":
        return url_value(value)
    if field_type == 2 or ui_type == "Number":
        return number_value(value)
    if field_type == 5 or ui_type == "DateTime":
        return timestamp_ms(value)
    if field_type == 7 or ui_type == "Checkbox":
        return bool_value(value)
    if target_field == "Has Subtitles":
        parsed = bool_value(value)
        if parsed is None:
            return None
        return "true" if parsed else "false"
    return clean_text(value)
