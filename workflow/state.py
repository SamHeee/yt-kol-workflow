#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Workflow state persistence for checkpoint and resume."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        logger.debug(f"状态保存: {self.state_file}")

    @classmethod
    def load(cls, filepath: str) -> "WorkflowState":
        """Load state from a saved file."""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = cls(keyword=data.get("keyword", ""))
        state.state_file = Path(filepath)
        data.setdefault("data_schema_version", 1)
        state.data = data
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
        prog = self.data["phase_d_progress"]
        if channel_id not in prog["completed_channel_ids"]:
            prog["completed_channel_ids"].append(channel_id)
            prog["completed_channels"] = len(prog["completed_channel_ids"])
            self.save()

    def get_remaining_channels(self, all_channel_ids: List[str]) -> List[str]:
        done = set(self.data["phase_d_progress"]["completed_channel_ids"])
        return [cid for cid in all_channel_ids if cid not in done]

    def update_stats(self, **kwargs):
        self.data.update(kwargs)
        self.save()
