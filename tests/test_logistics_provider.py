from datetime import datetime, timezone

import pytest
import requests

from logistics import (
    LogisticsProviderError,
    LogisticsRateLimitError,
    SeventeenTrackClient,
    TrackingRequest,
    TrackingStatus,
    TrackingSyncStatus,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, json, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "json": json,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _ok(accepted=None, rejected=None):
    return FakeResponse(
        payload={
            "code": 0,
            "data": {"accepted": accepted or [], "rejected": rejected or []},
        }
    )


def test_register_batches_at_40_and_converts_numeric_carrier():
    first = [
        {"number": f"TRACK{i:05d}", "carrier": 3011} for i in range(40)
    ]
    second = [{"number": "TRACK00040", "carrier": 3011}]
    session = FakeSession([_ok(first), _ok(second)])
    client = SeventeenTrackClient("private-key", session=session, sleep=lambda _: None)
    items = [
        TrackingRequest(f"TRACK{i:05d}", carrier="3011", tag=f"SMP-{i:06d}")
        for i in range(41)
    ]

    results = client.register(items)

    assert len(results) == 41
    assert [len(call["json"]) for call in session.calls] == [40, 1]
    assert all(call["url"].endswith("/track/v2.4/register") for call in session.calls)
    assert session.calls[0]["json"][0] == {
        "number": "TRACK00000",
        "carrier": 3011,
        "tag": "SMP-000000",
    }
    assert session.calls[0]["headers"]["17token"] == "private-key"
    assert all(result.sync_status is TrackingSyncStatus.REGISTERED for result in results)


def test_register_maps_rejections_and_redacts_sensitive_error_text():
    api_key = "private-super-secret"
    number = "RR123456789CN"
    raw_message = (
        f"The format of '{number}' is invalid; 17token={api_key}; "
        "contact jane@example.com or +86 13800138000"
    )
    session = FakeSession(
        [
            _ok(
                rejected=[
                    {
                        "number": number,
                        "carrier": 0,
                        "error": {"code": -18010012, "message": raw_message},
                    }
                ]
            )
        ]
    )
    client = SeventeenTrackClient(api_key, session=session)

    result = client.register([TrackingRequest(number)])[0]

    assert result.sync_status is TrackingSyncStatus.INVALID_NUMBER
    assert result.error_code == "-18010012"
    assert number not in result.error_message
    assert api_key not in result.error_message
    assert "jane@example.com" not in result.error_message
    assert "13800138000" not in result.error_message


def test_get_track_info_normalizes_status_event_and_source_sync_time_to_utc():
    session = FakeSession(
        [
            _ok(
                accepted=[
                    {
                        "number": "TESTTRACK000001",
                        "carrier": 100003,
                        "track_info": {
                            "latest_status": {
                                "status": "InTransit",
                                "sub_status": "InTransit_Departure",
                            },
                            "latest_event": {
                                "time_iso": "2026-08-04T09:15:00-05:00",
                                "description": "Departed the regional facility",
                                "location": "Chicago, IL, US",
                                "sub_status": "InTransit_Departure",
                            },
                            "tracking": {
                                "providers": [
                                    {
                                        "latest_sync_status": "Success",
                                        "latest_sync_time": "2026-08-04T14:30:00Z",
                                    }
                                ]
                            },
                        },
                    }
                ]
            )
        ]
    )
    client = SeventeenTrackClient("key", session=session)

    result = client.get_track_info([TrackingRequest("TESTTRACK000001", 100003)])[0]

    assert result.latest_status is TrackingStatus.IN_TRANSIT
    assert result.raw_status == "InTransit"
    assert result.latest_event.description == "Departed the regional facility"
    assert result.latest_event.location == "Chicago, IL, US"
    assert result.latest_event.event_time == datetime(
        2026, 8, 4, 14, 15, tzinfo=timezone.utc
    )
    assert result.track_time == datetime(2026, 8, 4, 14, 30, tzinfo=timezone.utc)
    assert result.sync_status is TrackingSyncStatus.SUCCESS
    assert result.source_sync_succeeded is True


@pytest.mark.parametrize(
    ("raw_status", "raw_sub_status", "expected"),
    [
        ("NotFound", "NotFound_Other", TrackingStatus.NOT_FOUND),
        ("OutForDelivery", "OutForDelivery", TrackingStatus.OUT_FOR_DELIVERY),
        ("Delivered", "Delivered", TrackingStatus.DELIVERED),
        ("InTransit", "InTransit_Returning", TrackingStatus.RETURNED),
        ("new-provider-status", "", TrackingStatus.UNKNOWN),
    ],
)
def test_get_track_info_maps_provider_statuses(raw_status, raw_sub_status, expected):
    session = FakeSession(
        [
            _ok(
                accepted=[
                    {
                        "number": "TRACK12345",
                        "carrier": 3011,
                        "track_info": {
                            "latest_status": {
                                "status": raw_status,
                                "sub_status": raw_sub_status,
                            },
                            "latest_event": None,
                            "tracking": {
                                "providers": [
                                    {
                                        "latest_sync_status": "Success",
                                        "latest_sync_time": "2026-08-04T10:00:00Z",
                                    }
                                ]
                            },
                        },
                    }
                ]
            )
        ]
    )
    result = SeventeenTrackClient("key", session=session).get_track_info(
        [TrackingRequest("TRACK12345", 3011)]
    )[0]
    assert result.latest_status is expected


def test_get_track_info_distinguishes_source_failure_from_http_success():
    session = FakeSession(
        [
            _ok(
                accepted=[
                    {
                        "number": "TRACK12345",
                        "carrier": 3011,
                        "track_info": {
                            "latest_status": {
                                "status": "NotFound",
                                "sub_status": "NotFound_Other",
                            },
                            "latest_event": None,
                            "tracking": {
                                "providers": [
                                    {
                                        "latest_sync_status": "Failure",
                                        "latest_sync_time": "2026-08-04T10:00:00Z",
                                    }
                                ]
                            },
                        },
                    }
                ]
            )
        ]
    )

    result = SeventeenTrackClient("key", session=session).get_track_info(
        [TrackingRequest("TRACK12345")]
    )[0]

    assert result.latest_status is TrackingStatus.NOT_FOUND
    assert result.sync_status is TrackingSyncStatus.SOURCE_FAILED
    assert result.source_sync_succeeded is False
    assert result.has_error is True
    assert "TRACK12345" not in result.error_message


def test_get_track_info_uses_only_the_latest_provider_sync_group():
    session = FakeSession(
        [
            _ok(
                accepted=[
                    {
                        "number": "TRACK12345",
                        "carrier": 3011,
                        "track_info": {
                            "tracking": {
                                "providers": [
                                    {
                                        "latest_sync_status": "Success",
                                        "latest_sync_time": "2026-08-04T10:00:00Z",
                                    },
                                    {
                                        "latest_sync_status": "Failure",
                                        "latest_sync_time": "2026-08-04T11:00:00Z",
                                    },
                                ]
                            }
                        },
                    }
                ]
            )
        ]
    )

    result = SeventeenTrackClient("key", session=session).get_track_info(
        [TrackingRequest("TRACK12345")]
    )[0]

    assert result.sync_status is TrackingSyncStatus.SOURCE_FAILED
    assert result.track_time == datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)


def test_get_track_info_tied_latest_providers_use_latest_success_time():
    session = FakeSession(
        [
            _ok(
                accepted=[
                    {
                        "number": "TRACK12345",
                        "carrier": 3011,
                        "track_info": {
                            "tracking": {
                                "providers": [
                                    {
                                        "latest_sync_status": "Success",
                                        "latest_sync_time": "2026-08-04T10:00:00Z",
                                    },
                                    {
                                        "latest_sync_status": "Failure",
                                        "latest_sync_time": "2026-08-04T12:00:00Z",
                                    },
                                    {
                                        "latest_sync_status": "Success",
                                        "latest_sync_time": "2026-08-04T12:00:00Z",
                                    },
                                ]
                            }
                        },
                    }
                ]
            )
        ]
    )

    result = SeventeenTrackClient("key", session=session).get_track_info(
        [TrackingRequest("TRACK12345")]
    )[0]

    assert result.sync_status is TrackingSyncStatus.SUCCESS
    assert result.track_time == datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def test_get_track_info_supports_top_level_track_time_and_sync_status():
    session = FakeSession(
        [
            _ok(
                accepted=[
                    {
                        "number": "TRACK12345",
                        "carrier": 3011,
                        "track_time": "2026-08-04 10:00:00",
                        "sync_status": True,
                        "package_status": "Delivered",
                    }
                ]
            )
        ]
    )

    result = SeventeenTrackClient("key", session=session).get_track_info(
        [TrackingRequest("TRACK12345")]
    )[0]

    assert result.latest_status is TrackingStatus.DELIVERED
    assert result.track_time == datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    assert result.sync_status is TrackingSyncStatus.SUCCESS


def test_429_honors_retry_after_then_succeeds():
    sleeps = []
    session = FakeSession(
        [
            FakeResponse(429, {}, headers={"Retry-After": "2.5"}),
            _ok(accepted=[{"number": "TRACK12345", "carrier": 3011}]),
        ]
    )
    client = SeventeenTrackClient(
        "key", session=session, max_retries=1, sleep=sleeps.append
    )

    results = client.register([TrackingRequest("TRACK12345")])

    assert len(results) == 1
    assert sleeps == [2.5]
    assert len(session.calls) == 2


def test_timeout_and_5xx_are_retried_with_finite_backoff():
    sleeps = []
    session = FakeSession(
        [
            FakeResponse(503, {}),
            requests.Timeout("request timed out for TRACK12345"),
            _ok(accepted=[{"number": "TRACK12345", "carrier": 3011}]),
        ]
    )
    client = SeventeenTrackClient(
        "key", session=session, max_retries=2, sleep=sleeps.append
    )

    results = client.register([TrackingRequest("TRACK12345")])

    assert len(results) == 1
    assert sleeps == [1.0, 2.0]
    assert len(session.calls) == 3


def test_retry_exhaustion_raises_typed_rate_limit_error():
    session = FakeSession(
        [FakeResponse(429, {}), FakeResponse(429, {})]
    )
    client = SeventeenTrackClient(
        "key", session=session, max_retries=1, sleep=lambda _: None
    )

    with pytest.raises(LogisticsRateLimitError) as exc_info:
        client.register([TrackingRequest("TRACK12345")])

    assert exc_info.value.status_code == 429
    assert exc_info.value.retryable is True
    assert len(session.calls) == 2


def test_api_level_error_is_sanitized_and_never_includes_raw_response_data():
    key = "private-key"
    number = "RR123456789CN"
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "code": -1,
                    "message": f"failed for {number}; api_key={key}",
                    "data": {"recipient": "private-recipient-data"},
                }
            )
        ]
    )
    client = SeventeenTrackClient(key, session=session)

    with pytest.raises(LogisticsProviderError) as exc_info:
        client.get_track_info([TrackingRequest(number)])

    message = str(exc_info.value)
    assert key not in message
    assert number not in message
    assert "private-recipient-data" not in message


def test_invalid_json_error_never_echoes_response_text():
    session = FakeSession(
        [FakeResponse(payload=None, json_error=ValueError("private response body"))]
    )
    client = SeventeenTrackClient("key", session=session)

    with pytest.raises(LogisticsProviderError) as exc_info:
        client.get_track_info([TrackingRequest("TRACK12345")])

    assert str(exc_info.value) == "17TRACK returned an invalid JSON response"
    assert "private response body" not in str(exc_info.value)


def test_refresh_defaults_to_read_only_gettrackinfo_and_register_is_opt_in():
    read_only_session = FakeSession([_ok()])
    read_only_client = SeventeenTrackClient("key", session=read_only_session)

    read_only_client.refresh([TrackingRequest("TRACK12345")])

    assert [call["url"].rsplit("/", 1)[-1] for call in read_only_session.calls] == [
        "gettrackinfo"
    ]

    register_session = FakeSession(
        [_ok(accepted=[{"number": "TRACK12345", "carrier": 3011}]), _ok()]
    )
    register_client = SeventeenTrackClient("key", session=register_session)

    register_client.refresh([TrackingRequest("TRACK12345")], register_first=True)

    assert [call["url"].rsplit("/", 1)[-1] for call in register_session.calls] == [
        "register",
        "gettrackinfo",
    ]


def test_empty_batch_makes_no_http_request():
    session = FakeSession([])
    client = SeventeenTrackClient("key", session=session)
    assert client.register([]) == []
    assert client.get_track_info([]) == []
    assert session.calls == []
