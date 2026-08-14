import os
import subprocess
import sys
from pathlib import Path

import pytest

from main import build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def parser(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_TOKEN", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    return build_parser()


def test_feishu_setup_defaults_to_complete_automated_workspace(parser):
    args = parser.parse_args(["feishu-setup"])

    assert args.command == "feishu-setup"
    assert args.feishu_app_token == ""
    assert args.no_create_base is False
    assert args.dry_run is False
    assert args.no_enable_automation is False
    assert args.collection_only is False


def test_cooperation_content_refresh_parser_contract(parser):
    args = parser.parse_args(
        [
            "refresh-cooperation-content",
            "--content-record-id",
            "rec-one",
            "--content-record-id",
            "rec-two",
            "--limit",
            "7",
            "--max-age-hours",
            "12",
            "--force",
            "--dry-run",
        ]
    )

    assert args.content_record_id == ["rec-one", "rec-two"]
    assert args.limit == 7
    assert args.max_age_hours == 12
    assert args.force is True
    assert args.dry_run is True


def test_sample_logistics_refresh_parser_contract(parser):
    args = parser.parse_args(
        [
            "refresh-sample-logistics",
            "--sample-record-id",
            "rec-sample",
            "--limit",
            "3",
            "--max-age-hours",
            "8",
            "--force",
            "--dry-run",
        ]
    )

    assert args.sample_record_id == ["rec-sample"]
    assert args.limit == 3
    assert args.max_age_hours == 8
    assert args.force is True
    assert args.dry_run is True


def test_sample_reminder_dispatch_parser_contract(parser):
    args = parser.parse_args(
        [
            "dispatch-sample-reminders",
            "--sample-record-id",
            "rec-sample",
            "--limit",
            "5",
            "--dry-run",
        ]
    )

    assert args.sample_record_id == ["rec-sample"]
    assert args.limit == 5
    assert args.dry_run is True


def test_missing_api_key_exits_cleanly_on_windows_gbk():
    env = os.environ.copy()
    env.pop("YOUTUBE_API_KEY", None)
    env["PYTHONIOENCODING"] = "cp936"
    result = subprocess.run(
        [
            sys.executable,
            "main.py",
            "batch",
            "--keywords-file",
            "keywords.example.txt",
            "--no-feishu",
            "--yes",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        encoding="cp936",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "YouTube API Key" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
