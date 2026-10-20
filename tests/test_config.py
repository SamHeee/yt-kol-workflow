import pytest

from config import FeishuConfig, LogisticsConfig, parse_keywords_file


LOGISTICS_ENV_VARS = (
    "LOGISTICS_PROVIDER",
    "TRACK17_API_KEY",
    "LOGISTICS_HTTP_TIMEOUT_SECONDS",
    "LOGISTICS_MAX_RETRIES",
    "SAMPLE_LOGISTICS_TIMEZONE",
    "SAMPLE_LOGISTICS_STALL_HOURS",
    "SAMPLE_TRACKING_TABLE_ID",
    "SAMPLE_REMINDER_USER_OPEN_ID",
)


def clear_environment(monkeypatch, names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_logistics_config_has_safe_defaults(monkeypatch):
    clear_environment(monkeypatch, LOGISTICS_ENV_VARS)

    config = LogisticsConfig()

    assert config.provider == "17track"
    assert config.api_key == ""
    assert config.timeout_seconds == 30
    assert config.max_retries == 2
    assert config.timezone_name == "Asia/Shanghai"
    assert config.stall_hours == 72
    assert config.sample_table_id == ""
    assert config.reminder_receive_id == ""


def test_logistics_validation_is_action_specific(monkeypatch):
    clear_environment(monkeypatch, LOGISTICS_ENV_VARS)
    config = LogisticsConfig()

    assert config.validate() == []
    assert config.validate(require_api_key=True) == [
        "物流刷新缺少 TRACK17_API_KEY"
    ]
    assert config.validate(require_recipient=True) == [
        "物流提醒缺少 SAMPLE_REMINDER_USER_OPEN_ID"
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"provider": "unsupported"}, "LOGISTICS_PROVIDER"),
        ({"timeout_seconds": -1}, "LOGISTICS_HTTP_TIMEOUT_SECONDS"),
        ({"max_retries": -1}, "LOGISTICS_MAX_RETRIES"),
        ({"timezone_name": "UTC"}, "SAMPLE_LOGISTICS_TIMEZONE"),
        ({"stall_hours": -1}, "SAMPLE_LOGISTICS_STALL_HOURS"),
    ],
)
def test_logistics_config_rejects_unsafe_values(monkeypatch, kwargs, message):
    clear_environment(monkeypatch, LOGISTICS_ENV_VARS)

    with pytest.raises(ValueError, match=message):
        LogisticsConfig(**kwargs)


def test_feishu_defaults_keep_interactive_automatic_setup_enabled(monkeypatch):
    clear_environment(
        monkeypatch,
        (
            "FEISHU_AUTH_MODE",
            "FEISHU_AUTO_SETUP",
            "FEISHU_AUTO_INSTALL",
            "FEISHU_OPEN_BROWSER",
        ),
    )

    config = FeishuConfig()

    assert config.auth_mode == "auto"
    assert config.auto_setup is True
    assert config.auto_install is True
    assert config.open_browser is True


def test_feishu_url_is_preserved_for_cli_resolution():
    url = "https://example.feishu.cn/wiki/fake_test_base_token"

    assert FeishuConfig.extract_app_token(url) == url


def test_keyword_csv_defaults_missing_trailing_optional_cells(tmp_path):
    source = tmp_path / "keywords.csv"
    source.write_text("keyword,sort_order,max_results\ncamping\ngear,date\n", encoding="utf-8")
    tasks = parse_keywords_file(str(source), default_max_results=75)
    assert [(task.keyword, task.sort_order, task.max_results) for task in tasks] == [
        ("camping", "", 75), ("gear", "date", 75),
    ]


@pytest.mark.parametrize("value", ["oops", "0", "-3", "1.5"])
def test_keyword_csv_reports_row_for_invalid_max_results(tmp_path, value):
    source = tmp_path / "keywords.csv"
    source.write_text(f"keyword,sort_order,max_results\nvalid,date,10\ninvalid,date,{value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 3 行 max_results"):
        parse_keywords_file(str(source))


def test_keyword_csv_reports_row_for_invalid_sort_order(tmp_path):
    source = tmp_path / "keywords.csv"
    source.write_text("keyword,sort_order,max_results\ncamping,invalid,10\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 行 sort_order"):
        parse_keywords_file(str(source))


def test_keyword_csv_accepts_every_sort_order_supported_by_cli(tmp_path):
    from main import YOUTUBE_SEARCH_ORDER_CHOICES

    source = tmp_path / "keywords.csv"
    source.write_text(
        "keyword,sort_order,max_results\n" + "".join(
            f"camping,{sort_order},10\n" for sort_order in YOUTUBE_SEARCH_ORDER_CHOICES
        ),
        encoding="utf-8",
    )
    assert [task.sort_order for task in parse_keywords_file(str(source))] == list(YOUTUBE_SEARCH_ORDER_CHOICES)
