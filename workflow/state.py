#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Workflow state persistence for checkpoint and resume."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from workflow.persistence import atomic_write_json

logger = logging.getLogger("kol_workflow.workflow.state")

CURRENT_DATA_SCHEMA_VERSION = 2


class WorkflowState:
    """Manages checkpoint state for a single keyword search workflow."""

    def __init__(self, keyword: str, state_dir: str = "./output/temp"):
        self.keyword = keyword
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_kw = "".join(c if c.isalnum() or c == "_" else "_" for c in keyword)[:30]
        self.state_file = self.state_dir / f"state_{ts}_{safe_kw}.json"

        self.data: Dict = {
            "data_schema_version": CURRENT_DATA_SCHEMA_VERSION,
            "checkpoint_version": 2,
            "channel_results": {},
            "output_commits": {},
            "task_id": f"{ts}_{safe_kw}",
            "keyword": keyword,
            "created_at": datetime.now().isoformat(),
            "current_phase": "A",
            "phase_a_complete": False,
            "phase_b_complete": False,
            "phase_c_complete": False,
            "phase_d_complete": False,
            "phase_d_progress": {
                "total_channels": 0,
                "completed_channels": 0,
                "completed_channel_ids": [],
            },
            "quota_used": 0,
            "search_results_count": 0,
            "qualified_count": 0,
            "new_channels_count": 0,
            "search_results": [],
            "qualified_videos": [],
            "all_videos": [],
            "missing_video_ids": [],
            "new_channels": [],
            "existing_channels": [],
        }

    def save(self):
        """Save state to disk."""
        self.data["updated_at"] = datetime.now().isoformat()
        atomic_write_json(self.state_file, self.data)
        logger.debug(f"状态保存: {self.state_file}")

    @classmethod
    def load(cls, filepath: str) -> "WorkflowState":
        """Load state from a saved file."""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = cls(keyword=data.get("keyword", ""), state_dir=str(Path(filepath).parent))
        state.state_file = Path(filepath)
        data.setdefault("data_schema_version", 1)
        state.data = data
        if data.get("checkpoint_version", 1) < 2:
            # Legacy done IDs have no atomic payload and B may have been marked
            # complete after an HTTP error. Re-fetch rather than trust them.
            data["checkpoint_migration"] = {
                "from_version": data.get("checkpoint_version", 1),
                "replay_phases": ["B", "C", "D"],
                "legacy_completed_channel_ids": data.get("phase_d_progress", {}).get(
                    "completed_channel_ids", []
                ),
            }
            data["replay_channel_ids"] = [
                channel["channel_id"] for channel in data.get("new_channels", [])
                if channel.get("channel_id")
            ]
            for phase in ("b", "c", "d"):
                data[f"phase_{phase}_complete"] = False
            data["phase_d_progress"] = {"completed_channel_ids": [], "completed_channels": 0}
            data["channel_results"] = {}
            data["output_commits"] = {}
            data["checkpoint_version"] = 2
            logger.warning("旧检查点缺少可靠提交信息；续跑将重算 B/C/D，原待处理频道重新核验并按唯一键补写。")
        if data["data_schema_version"] < CURRENT_DATA_SCHEMA_VERSION:
            logger.warning(
                "状态文件数据版本为 %s，当前版本为 %s；已完成的阶段D不会自动补齐新字段，"
                "请运行 refresh-influencers。",
                data["data_schema_version"],
                CURRENT_DATA_SCHEMA_VERSION,
            )
        logger.info(f"恢复状态: {filepath}, 阶段={data.get('current_phase')}")
        return state

    def mark_phase(self, phase: str, complete: bool = True):
        key = f"phase_{phase.lower()}_complete"
        self.data[key] = complete
        self.data["current_phase"] = phase
        self.save()

    def is_phase_complete(self, phase: str) -> bool:
        return self.data.get(f"phase_{phase.lower()}_complete", False)

    def mark_channel_done(self, channel_id: str):
        """Compatibility helper for an explicitly excluded/unavailable channel."""
        self.commit_channel(channel_id, outcome="excluded")

    def commit_channel(self, channel_id: str, detail=None, videos=None, *, outcome="complete"):
        """Commit one channel's payload and done marker in the same disk write."""
        if not channel_id:
            return
        self.data.setdefault("channel_results", {})[channel_id] = {
            "outcome": outcome, "detail": detail, "videos": videos or [],
        }
        prog = self.data["phase_d_progress"]
        if channel_id not in prog["completed_channel_ids"]:
            prog["completed_channel_ids"].append(channel_id)
            prog["completed_channels"] = len(prog["completed_channel_ids"])
        self.save()

    def channel_payloads(self):
        details, videos = [], {}
        for result in self.data.get("channel_results", {}).values():
            if result.get("outcome") != "complete":
                continue
            if result.get("detail"):
                details.append(result["detail"])
            for video in result.get("videos", []):
                videos[video["video_id"]] = video
        return details, list(videos.values())

    def completed_channel_ids(self):
        return {
            cid for cid, result in self.data.get("channel_results", {}).items()
            if result.get("outcome") == "complete"
        }

    def output_complete(self, phase: str, destination: str) -> bool:
        return bool(self.data.get("output_commits", {}).get(phase, {}).get(destination))

    def mark_output(self, phase: str, destination: str):
        self.data.setdefault("output_commits", {}).setdefault(phase, {})[destination] = True
        self.save()

    def invalidate_outputs(self, phase: str):
        self.data.setdefault("output_commits", {})[phase] = {}
        self.save()

    def get_remaining_channels(self, all_channel_ids: List[str]) -> List[str]:
        done = set(self.data["phase_d_progress"]["completed_channel_ids"])
        return [cid for cid in all_channel_ids if cid not in done]

    def update_stats(self, **kwargs):
        self.data.update(kwargs)
        self.save()
