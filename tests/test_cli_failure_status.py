"""Offline CLI routing, authentication, and exit-status regressions."""

import subprocess
from types import SimpleNamespace

import pytest
import requests

import main as cli


def _unexpected_side_effect(*args, **kwargs):
    raise AssertionError("Offline CLI test attempted an external operation")


@pytest.fixture(autouse=True)
def offline_cli(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "offline-test-key")
    monkeypatch.setattr(requests.sessions.Session, "request", _unexpected_side_effect)
    monkeypatch.setattr(subprocess, "run", _unexpected_side_effect)
    monkeypatch.setattr(cli, "_create_feishu_context", _unexpected_side_effect)
    monkeypatch.setattr(cli, "_ensure_full_feishu_workspace", _unexpected_side_effect)
    monkeypatch.setattr(cli, "setup_logger", lambda **kwargs: None)
    monkeypatch.setattr(cli, "LogisticsConfig", lambda: SimpleNamespace(validate=lambda **kwargs: []))


OPERATIONAL_COMMANDS = [
    ("refresh-cooperation-content", "handle_refresh_cooperation_content"),
    ("refresh-sample-logistics", "handle_refresh_sample_logistics"),
    ("dispatch-sample-reminders", "handle_dispatch_sample_reminders"),
]


def _operational_args(command, mode="auto"):
    argv = [
        command, "--dry-run", "--feishu-auth-mode", mode,
        "--no-feishu-auto-setup", "--no-feishu-auto-install", "--no-feishu-browser",
    ]
    if command == "refresh-cooperation-content":
        argv += ["--youtube-api-key", "offline-test-key"]
    return cli.build_parser().parse_args(argv)


@pytest.mark.parametrize(("command", "handler_name"), OPERATIONAL_COMMANDS)
def test_operational_auto_mode_reaches_context_resolution(monkeypatch, command, handler_name):
    class ContextReached(Exception):
        pass

    calls = []
    def context(config, **kwargs):
        calls.append((config.auth_mode, kwargs))
        raise ContextReached()

    monkeypatch.setattr(cli, "_create_feishu_context", context)
    with pytest.raises(ContextReached):
        getattr(cli, handler_name)(_operational_args(command))
    assert calls == [("auto", {"create_base_if_missing": False})]


@pytest.mark.parametrize(("command", "handler_name"), OPERATIONAL_COMMANDS)
def test_operational_explicit_app_mode_is_rejected_before_context(monkeypatch, command, handler_name):
    with pytest.raises(RuntimeError, match="仅允许 FEISHU_AUTH_MODE=cli"):
        getattr(cli, handler_name)(_operational_args(command, mode="app"))


@pytest.mark.parametrize(("command", "handler_name"), OPERATIONAL_COMMANDS)
def test_operational_auto_resolution_still_requires_cli_identity(monkeypatch, command, handler_name):
    monkeypatch.setattr(cli, "_create_feishu_context", lambda *args, **kwargs: SimpleNamespace(auth_mode="app"))
    with pytest.raises(RuntimeError, match="未获得 lark-cli 用户身份客户端"):
        getattr(cli, handler_name)(_operational_args(command))


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("成功", 0), ("失败", 1), ("异常", 1), ("部分成功", 2), ("预算不足", 3), ("配额中断", 3), ("中断", 130)],
)
def test_search_main_parses_arguments_and_returns_workflow_exit_code(monkeypatch, tmp_path, status, exit_code):
    calls = []

    def handler(args, config):
        calls.append((args.keyword, args.no_feishu, args.no_excel, config.max_results))
        return {"status": status, "error": "offline fixture" if status != "成功" else ""}

    monkeypatch.setattr(cli, "handle_search", handler)
    monkeypatch.setattr("sys.argv", ["main.py", "search", "--keyword", "camping", "--max-results", "20", "--no-feishu", "--no-excel", "--output-dir", str(tmp_path)])
    assert cli.main() == exit_code
    assert calls == [("camping", True, True, 20)]


def test_real_search_handler_returns_pipeline_summary_to_main(monkeypatch, tmp_path):
    summary = {"status": "部分成功", "error": "one channel is still pending"}
    calls = []

    def keyword_run(**kwargs):
        calls.append(kwargs)
        return summary

    monkeypatch.setattr(cli, "run_single_keyword", keyword_run)
    monkeypatch.setattr("sys.argv", [
        "main.py", "search", "--keyword", "camping", "--sort-order", "relevance",
        "--no-feishu", "--no-excel", "--output-dir", str(tmp_path),
    ])
    assert cli.main() == 2
    assert len(calls) == 1
    assert calls[0]["keyword"] == "camping"
    assert calls[0]["feishu_client"] is None
    assert calls[0]["excel_exporter"] is None


def _batch_argv(tmp_path):
    keywords = tmp_path / "keywords.txt"
    keywords.write_text("camping\ngear\n", encoding="utf-8")
    return ["main.py", "batch", "--keywords-file", str(keywords), "--no-feishu", "--no-excel", "--yes", "--output-dir", str(tmp_path)]


def _capture_real_batch_result(monkeypatch):
    captured = []
    real_run_batch = cli.run_batch

    def run_batch(*args, **kwargs):
        result = real_run_batch(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(cli, "run_batch", run_batch)
    return captured


def test_batch_budget_shortfall_returns_nonzero_without_starting_work(monkeypatch, tmp_path):
    tracker_class = cli.QuotaTracker
    monkeypatch.setattr(cli, "QuotaTracker", lambda *args, **kwargs: tracker_class(daily_limit=1))
    monkeypatch.setattr(cli, "run_single_keyword", _unexpected_side_effect)
    results = _capture_real_batch_result(monkeypatch)
    monkeypatch.setattr("sys.argv", _batch_argv(tmp_path))
    assert cli.main() == 3
    assert results[0]["status"] == "预算不足"
    assert results[0]["results"] == []
    assert results[0]["remaining_keywords"] == 2


@pytest.mark.parametrize(("second_status", "expected_code"), [("成功", 0), ("失败", 2), ("部分成功", 2)])
def test_batch_exit_code_reflects_all_keyword_results(monkeypatch, tmp_path, second_status, expected_code):
    calls = []

    def keyword_run(**kwargs):
        calls.append(kwargs["keyword"])
        return {"keyword": kwargs["keyword"], "status": "成功" if len(calls) == 1 else second_status}

    monkeypatch.setattr(cli, "run_single_keyword", keyword_run)
    results = _capture_real_batch_result(monkeypatch)
    monkeypatch.setattr("sys.argv", _batch_argv(tmp_path))
    assert cli.main() == expected_code
    assert calls == ["camping", "gear"]
    assert [result["status"] for result in results[0]["results"]] == ["成功", second_status]
    assert results[0]["remaining_keywords"] == 0
    assert results[0]["status"] == ("成功" if second_status == "成功" else "部分成功")


def test_batch_quota_stop_reports_remaining_keywords_and_nonzero_status(monkeypatch, tmp_path):
    def keyword_run(**kwargs):
        kwargs["quota_tracker"].consume("search.list", 99)
        return {"keyword": kwargs["keyword"], "status": "成功"}

    monkeypatch.setattr(cli, "run_single_keyword", keyword_run)
    results = _capture_real_batch_result(monkeypatch)
    monkeypatch.setattr("sys.argv", _batch_argv(tmp_path))
    assert cli.main() == 3
    assert results[0]["status"] == "配额中断"
    assert len(results[0]["results"]) == 1
    assert results[0]["remaining_keywords"] == 1
