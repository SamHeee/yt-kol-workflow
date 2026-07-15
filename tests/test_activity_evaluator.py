from datetime import datetime, timedelta, timezone

from filter.activity_evaluator import (
    ACTIVE_STATUS,
    AT_RISK_STATUS,
    PENDING_STATUS,
    evaluate_channel_activity,
    parse_rfc3339_utc,
)


NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _video(published_at_raw, live_broadcast_content="none"):
    return {
        "published_at_raw": published_at_raw,
        "live_broadcast_content": live_broadcast_content,
    }


def test_parse_rfc3339_normalizes_offsets_to_utc():
    parsed = parse_rfc3339_utc("2026-07-10T20:00:00+08:00")

    assert parsed == NOW
    assert parse_rfc3339_utc("2026-07-10 12:00:00") is None
    assert parse_rfc3339_utc("not-a-date") is None


def test_exactly_thirty_days_old_is_still_active():
    result = evaluate_channel_activity(
        [_video((NOW - timedelta(days=30)).isoformat())],
        now=NOW,
    )

    assert result == {
        "latest_published_at": "2026-06-10 12:00:00",
        "activity_status": ACTIVE_STATUS,
        "error": "",
    }


def test_older_than_thirty_days_is_at_risk():
    result = evaluate_channel_activity(
        [_video((NOW - timedelta(days=30, seconds=1)).isoformat())],
        now=NOW,
    )

    assert result["activity_status"] == AT_RISK_STATUS
    assert result["latest_published_at"] == "2026-06-10 11:59:59"


def test_latest_non_upcoming_publication_wins():
    result = evaluate_channel_activity(
        [
            _video("2026-07-11T12:00:00Z", "upcoming"),
            _video("2026-05-01T12:00:00Z"),
            _video("2026-07-01T12:00:00+00:00"),
        ],
        now=NOW,
    )

    assert result["latest_published_at"] == "2026-07-01 12:00:00"
    assert result["activity_status"] == ACTIVE_STATUS


def test_successful_empty_or_upcoming_only_result_is_at_risk():
    assert evaluate_channel_activity([], now=NOW) == {
        "latest_published_at": "",
        "activity_status": AT_RISK_STATUS,
        "error": "",
    }
    assert evaluate_channel_activity(
        [_video("2026-07-11T12:00:00Z", "upcoming")],
        now=NOW,
    )["activity_status"] == AT_RISK_STATUS


def test_fetch_error_is_pending_and_does_not_expose_a_date():
    result = evaluate_channel_activity(
        [_video("2026-07-01T12:00:00Z")],
        fetch_error="quotaExceeded",
        now=NOW,
    )

    assert result == {
        "latest_published_at": "",
        "activity_status": PENDING_STATUS,
        "error": "quotaExceeded",
    }


def test_any_unparseable_publication_is_pending():
    result = evaluate_channel_activity(
        [
            _video("2026-07-01T12:00:00Z"),
            _video("invalid"),
        ],
        now=NOW,
    )

    assert result["activity_status"] == PENDING_STATUS
    assert result["latest_published_at"] == ""
    assert "invalid publication time" in result["error"]


def test_legacy_published_at_remains_supported_as_utc():
    result = evaluate_channel_activity(
        [{"published_at": "2026-07-01 12:00:00"}],
        now=NOW,
    )

    assert result["latest_published_at"] == "2026-07-01 12:00:00"
    assert result["activity_status"] == ACTIVE_STATUS
