#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent channel ID store for cross-run incremental deduplication."""

import json
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Set

logger = logging.getLogger("kol_workflow.workflow.seen_channels")


DEFAULT_STATE_ROOT = Path.home() / ".kol_workflow" / "seen_channels"


def default_seen_channels_path(
    *,
    cli_profile: str = "kol-workflow",
    app_token: str = "",
    root: Path = DEFAULT_STATE_ROOT,
) -> Path:
    """Return an isolated, non-secret channel-state path for one Base target.

    The resource token is never written into the path.  A short SHA-256 digest
    keeps separate Base targets from sharing incremental-deduplication history,
    while the sanitized profile name makes the directory understandable to a
    local operator.  Before a Base target is known, the profile-only ``local``
    namespace is used.
    """

    profile = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(cli_profile or "kol-workflow").strip()
    ).strip("_") or "kol-workflow"
    token = str(app_token or "").strip()
    target_key = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else "local"
    return Path(root).expanduser() / profile / f"{target_key}.json"


class SeenChannelStore:
    """Stores channel IDs that have already been processed across runs."""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.data = {
            "version": 1,
            "updated_at": "",
            "channels": {},
        }
        self.load()

    def load(self):
        if not self.filepath.exists():
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.data["version"] = data.get("version", 1)
                self.data["updated_at"] = data.get("updated_at", "")
                self.data["channels"] = data.get("channels", {}) or {}
        except Exception as exc:
            logger.warning(f"读取持久频道库失败: {exc}")

    def save(self):
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def ids(self) -> Set[str]:
        return set(self.data.get("channels", {}).keys())

    def mark_channels(self, channels: Iterable[Dict], source_keyword: str = ""):
        now = datetime.now().isoformat(timespec="seconds")
        store = self.data.setdefault("channels", {})
        changed = False

        for ch in channels:
            channel_id = ch.get("channel_id", "")
            if not channel_id:
                continue

            existing = store.get(channel_id, {})
            first_seen_at = existing.get("first_seen_at") or now
            keywords = set(existing.get("source_keywords", []))
            keyword = source_keyword or ch.get("source_keyword", "")
            if keyword:
                keywords.add(keyword)

            rep = ch.get("representative_video", {})
            store[channel_id] = {
                "channel_id": channel_id,
                "channel_title": ch.get("channel_title") or existing.get("channel_title", ""),
                "first_seen_at": first_seen_at,
                "last_seen_at": now,
                "source_keywords": sorted(keywords),
                "representative_video_url": (
                    rep.get("video_url")
                    or ch.get("rep_video_url")
                    or existing.get("representative_video_url", "")
                ),
            }
            changed = True

        if changed:
            self.save()
