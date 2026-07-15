#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rule-based preliminary classification of a YouTube channel."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


logger = logging.getLogger("kol_workflow.filter.channel_classifier")

FALLBACK = "待确认"
FALLBACK_ASSESSMENT = (
    "领域=待确认; 内容=待确认; 主体=待确认; 自有品牌=待确认"
)
TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "channel_taxonomy.json"


def classify_channel(
    channel_name: str,
    description: str,
    recent_videos: Sequence[Mapping[str, Any]],
    source_keyword: str = "",
) -> str:
    """Return a fixed-format, whitelist-backed preliminary assessment.

    ``recent_videos`` accepts dictionaries containing ``title`` and ``tags``;
    tags may be a comma-separated string or a sequence.  At most three content
    labels are emitted.  The source keyword is deliberately low-weight and can
    help choose a domain, but cannot determine channel ownership or主体.

    Any taxonomy or input-shape failure safely returns four ``待确认`` values.
    """
    try:
        taxonomy = _load_taxonomy()
        videos = list(recent_videos or [])
        if not all(isinstance(video, Mapping) for video in videos):
            return FALLBACK_ASSESSMENT

        name_text = _normalize_text(channel_name)
        description_text = _normalize_text(description)
        video_text = _video_text(videos)
        source_text = _normalize_text(source_keyword)

        domain = _choose_domain(
            taxonomy,
            name_text=name_text,
            description_text=description_text,
            video_text=video_text,
            source_text=source_text,
        )
        content = _choose_content_types(
            taxonomy,
            description_text=description_text,
            video_text=video_text,
        )
        entity = _choose_entity_type(
            taxonomy,
            name_text=name_text,
            description_text=description_text,
        )
        own_brand = _choose_own_brand(taxonomy, description_text)
        return (
            f"领域={domain}; 内容={','.join(content)}; "
            f"主体={entity}; 自有品牌={own_brand}"
        )
    except Exception as exc:  # A business label must not break Phase D.
        logger.warning("频道初步判断失败，已回退待确认: %s", exc)
        return FALLBACK_ASSESSMENT


@lru_cache(maxsize=1)
def _load_taxonomy() -> Dict[str, Any]:
    with TAXONOMY_PATH.open("r", encoding="utf-8") as handle:
        taxonomy = json.load(handle)
    _validate_taxonomy(taxonomy)
    return taxonomy


def _validate_taxonomy(taxonomy: Mapping[str, Any]) -> None:
    if not isinstance(taxonomy.get("domains"), list):
        raise ValueError("taxonomy.domains 必须是列表")
    if not isinstance(taxonomy.get("content_types"), list):
        raise ValueError("taxonomy.content_types 必须是列表")
    if not isinstance(taxonomy.get("entity_types"), Mapping):
        raise ValueError("taxonomy.entity_types 必须是对象")
    if not isinstance(taxonomy.get("own_brand"), Mapping):
        raise ValueError("taxonomy.own_brand 必须是对象")
    for group_name in ("domains", "content_types"):
        for item in taxonomy[group_name]:
            if not item.get("label") or not isinstance(item.get("keywords"), list):
                raise ValueError(f"taxonomy.{group_name} 的条目缺少 label/keywords")


def _choose_domain(
    taxonomy: Mapping[str, Any],
    *,
    name_text: str,
    description_text: str,
    video_text: str,
    source_text: str,
) -> str:
    scored: List[Tuple[int, int, str]] = []
    for index, item in enumerate(taxonomy["domains"]):
        keywords = item["keywords"]
        score = (
            3 * _keyword_score(name_text, keywords)
            + 2 * _keyword_score(description_text, keywords)
            + 2 * _keyword_score(video_text, keywords)
            + _keyword_score(source_text, keywords)
        )
        if score:
            scored.append((score, -index, item["label"]))
    return max(scored)[2] if scored else FALLBACK


def _choose_content_types(
    taxonomy: Mapping[str, Any],
    *,
    description_text: str,
    video_text: str,
) -> List[str]:
    scored: List[Tuple[int, int, str]] = []
    for index, item in enumerate(taxonomy["content_types"]):
        keywords = item["keywords"]
        score = _keyword_score(description_text, keywords) + 3 * _keyword_score(
            video_text, keywords
        )
        if score:
            scored.append((score, -index, item["label"]))
    if not scored:
        return [FALLBACK]
    # Use scores to choose the three strongest labels, then restore taxonomy
    # order so the serialized field is stable (e.g. 产品测评 before 教程).
    selected = sorted(scored, reverse=True)[:3]
    selected.sort(key=lambda row: -row[1])
    return [label for _, _, label in selected]


def _choose_entity_type(
    taxonomy: Mapping[str, Any],
    *,
    name_text: str,
    description_text: str,
) -> str:
    # Priority resolves phrases such as a branded "team" official channel in a
    # stable and business-useful way.
    priority = {
        "品牌官方": 5,
        "媒体机构": 4,
        "家庭/多人": 3,
        "团队": 2,
        "个人创作者": 1,
    }
    scored: List[Tuple[int, int, str]] = []
    for label, keywords in taxonomy["entity_types"].items():
        score = 3 * _keyword_score(name_text, keywords) + 2 * _keyword_score(
            description_text, keywords
        )
        if score:
            scored.append((score, priority.get(label, 0), label))
    return max(scored)[2] if scored else FALLBACK


def _choose_own_brand(taxonomy: Mapping[str, Any], description_text: str) -> str:
    rules = taxonomy["own_brand"]
    if _keyword_score(description_text, rules.get("strong", [])):
        return "明确有"
    if _keyword_score(description_text, rules.get("weak", [])):
        return "疑似"
    # Sponsorship and affiliate language is deliberately not ownership proof.
    return "未发现"


def _video_text(videos: Iterable[Mapping[str, Any]]) -> str:
    parts: List[str] = []
    for video in videos:
        parts.append(str(video.get("title") or ""))
        tags = video.get("tags") or ""
        if isinstance(tags, str):
            parts.append(tags)
        elif isinstance(tags, (list, tuple, set)):
            parts.extend(str(tag) for tag in tags)
        else:
            parts.append(str(tags))
    return _normalize_text(" ".join(parts))


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _keyword_score(text: str, keywords: Iterable[str]) -> int:
    return sum(1 for keyword in keywords if _contains_keyword(text, keyword))


def _contains_keyword(text: str, keyword: str) -> bool:
    needle = _normalize_text(keyword)
    if not text or not needle:
        return False
    # Latin keywords use alphanumeric boundaries, preventing e.g. "car" from
    # matching "scary". Chinese phrases and punctuation-rich keywords use a
    # direct substring check.
    if re.fullmatch(r"[a-z0-9 ]+", needle):
        pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return needle in text


__all__ = ["FALLBACK_ASSESSMENT", "classify_channel"]
