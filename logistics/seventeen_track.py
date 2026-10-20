#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""17TRACK V2.4 client used by the daily sample logistics refresh.

Only the registered-tracking endpoints are used.  ``gettrackinfo`` reads the
latest result already maintained by 17TRACK and therefore does not consume a
real-time-query credit.  Every request is capped at the provider's documented
40-number batch size.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import requests as requests_lib

from .base import (
    LogisticsProviderError,
    LogisticsRateLimitError,
    TrackingEvent,
    TrackingRequest,
    TrackingResult,
    TrackingStatus,
    TrackingSyncStatus,
)


API_BASE_URL = "https://api.17track.net/track/v2.4"
BATCH_SIZE = 40
_MAX_ERROR_LENGTH = 500


def _chunks(items: Sequence[TrackingRequest], size: int) -> Iterable[list[TrackingRequest]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _as_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-like provider timestamp and normalize it to aware UTC."""

    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) > 100_000_000_000:
            timestamp /= 1000.0
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sanitize_error(
    value: Any,
    *,
    api_key: str,
    tracking_numbers: Sequence[str] = (),
) -> str:
    """Return an operationally useful message without credentials or PII."""

    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    secrets = [api_key, *(str(number) for number in tracking_numbers)]
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        label = "[API_KEY_REDACTED]" if secret == api_key else "[TRACKING_NUMBER_REDACTED]"
        text = re.sub(re.escape(secret), label, text, flags=re.IGNORECASE)

    # Be defensive if a provider message unexpectedly echoes request headers,
    # an e-mail address, or a phone-like long number not present in our input.
    text = re.sub(
        r"(?i)\b(17token|authorization|api[ _-]?key)\s*[:=]\s*[^,;\s]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[EMAIL_REDACTED]", text)
    text = re.sub(r"(?<!\w)\+?\d(?:[\d ()-]{7,}\d)(?!\w)", "[NUMBER_REDACTED]", text)
    return text[:_MAX_ERROR_LENGTH] or "17TRACK request failed"


def _carrier_text(value: Any) -> str:
    return "" if value in (None, "") else str(value).strip()


def _request_payload(item: TrackingRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {"number": item.number}
    if item.carrier not in (None, ""):
        carrier: Any = item.carrier
        if isinstance(carrier, str) and carrier.strip().isdigit():
            carrier = int(carrier.strip())
        payload["carrier"] = carrier
    if item.tag:
        payload["tag"] = item.tag
    return payload


def _normalized_status(raw_status: Any, raw_sub_status: Any = "") -> TrackingStatus:
    status = re.sub(r"[^a-z]", "", str(raw_status or "").lower())
    sub_status = re.sub(r"[^a-z]", "", str(raw_sub_status or "").lower())

    # Returning/returned normally appears as a sub-status of InTransit.
    if "return" in status or "return" in sub_status:
        return TrackingStatus.RETURNED

    mapping = {
        "pending": TrackingStatus.PENDING_QUERY,
        "notfound": TrackingStatus.NOT_FOUND,
        "inforeceived": TrackingStatus.INFO_RECEIVED,
        "intransit": TrackingStatus.IN_TRANSIT,
        "availableforpickup": TrackingStatus.AVAILABLE_FOR_PICKUP,
        "outfordelivery": TrackingStatus.OUT_FOR_DELIVERY,
        "deliveryfailure": TrackingStatus.DELIVERY_FAILURE,
        "attemptfail": TrackingStatus.DELIVERY_FAILURE,
        "delivered": TrackingStatus.DELIVERED,
        "expired": TrackingStatus.EXPIRED,
        "exception": TrackingStatus.EXCEPTION,
    }
    return mapping.get(status, TrackingStatus.UNKNOWN)


def _event_time(event: Mapping[str, Any]) -> Optional[datetime]:
    parsed = _as_utc(event.get("time_utc")) or _as_utc(event.get("time_iso"))
    if parsed is not None:
        return parsed

    raw = event.get("time_raw")
    if not isinstance(raw, Mapping) or not raw.get("date"):
        return None
    raw_text = str(raw["date"])
    if raw.get("time"):
        raw_text += f"T{raw['time']}"
    if raw.get("timezone"):
        raw_text += str(raw["timezone"])
    return _as_utc(raw_text)


def _event_location(event: Mapping[str, Any]) -> str:
    direct = str(event.get("location") or "").strip()
    if direct:
        return direct
    address = event.get("address")
    if not isinstance(address, Mapping):
        return ""
    parts: list[str] = []
    for key in ("city", "state", "country"):
        part = str(address.get(key) or "").strip()
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


def _latest_event(track_info: Mapping[str, Any]) -> Optional[TrackingEvent]:
    event = track_info.get("latest_event")
    if not isinstance(event, Mapping):
        return None
    description = str(event.get("description") or "").strip()
    timestamp = _event_time(event)
    location = _event_location(event)
    sub_status = str(event.get("sub_status") or "").strip()
    if not any((description, timestamp, location, sub_status)):
        return None
    return TrackingEvent(
        description=description,
        event_time=timestamp,
        location=location,
        sub_status=sub_status,
    )


def _providers(track_info: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tracking = track_info.get("tracking")
    if not isinstance(tracking, Mapping):
        return []
    providers = tracking.get("providers")
    if not isinstance(providers, list):
        return []
    return [item for item in providers if isinstance(item, Mapping)]


def _source_sync_outcome(
    item: Mapping[str, Any], track_info: Mapping[str, Any]
) -> tuple[TrackingSyncStatus, Optional[datetime]]:
    """Derive sync status and time from the same most-recent source set.

    17TRACK can return more than one carrier provider.  An older successful
    provider must not make a newer failed synchronization look successful.
    Providers sharing the latest parsed sync timestamp are treated as one
    source set; undated providers are only considered when no provider has a
    usable timestamp.
    """

    direct = item.get("sync_status")
    if isinstance(direct, bool):
        return (
            (
                TrackingSyncStatus.SUCCESS
                if direct
                else TrackingSyncStatus.SOURCE_FAILED
            ),
            _as_utc(item.get("track_time")),
        )

    provider_outcomes = [
        (
            str(provider.get("latest_sync_status") or "").strip().lower(),
            _as_utc(provider.get("latest_sync_time")),
        )
        for provider in _providers(track_info)
    ]
    timed_outcomes = [outcome for outcome in provider_outcomes if outcome[1] is not None]
    if timed_outcomes:
        latest_time = max(time for _, time in timed_outcomes if time is not None)
        current_outcomes = [
            outcome for outcome in timed_outcomes if outcome[1] == latest_time
        ]
    else:
        current_outcomes = provider_outcomes

    statuses = [status for status, _ in current_outcomes if status]
    if any(status == "success" for status in statuses):
        successful_times = [
            sync_time
            for status, sync_time in current_outcomes
            if status == "success" and sync_time is not None
        ]
        return (
            TrackingSyncStatus.SUCCESS,
            max(successful_times) if successful_times else None,
        )
    if any(status in {"pending", "syncing", "processing"} for status in statuses):
        pending_times = [
            sync_time
            for status, sync_time in current_outcomes
            if status in {"pending", "syncing", "processing"}
            and sync_time is not None
        ]
        return (
            TrackingSyncStatus.SOURCE_PENDING,
            max(pending_times) if pending_times else None,
        )
    if statuses:
        failed_times = [
            sync_time
            for status, sync_time in current_outcomes
            if status and sync_time is not None
        ]
        return (
            TrackingSyncStatus.SOURCE_FAILED,
            max(failed_times) if failed_times else None,
        )
    return TrackingSyncStatus.NO_TRACK_INFO, None


def _rejection_status(code: Any, message: Any) -> TrackingSyncStatus:
    code_text = str(code or "")
    normalized = str(message or "").lower()
    if "already registered" in normalized or "already been registered" in normalized:
        return TrackingSyncStatus.REGISTERED
    if code_text == "-18010012" or ("format" in normalized and "invalid" in normalized):
        return TrackingSyncStatus.INVALID_NUMBER
    if code_text == "-18019903" or "carrier cannot be detected" in normalized:
        return TrackingSyncStatus.UNSUPPORTED_CARRIER
    if code_text == "-18019902" or "register first" in normalized or "not register" in normalized:
        return TrackingSyncStatus.NOT_REGISTERED
    if any(
        term in normalized
        for term in ("additional field", "additional parameter", "postal code", "phone last")
    ):
        return TrackingSyncStatus.ADDITIONAL_PARAMETER_REQUIRED
    return TrackingSyncStatus.REJECTED


class SeventeenTrackClient:
    """Minimal 17TRACK V2.4 registered-tracking client."""

    base_url = API_BASE_URL
    batch_size = BATCH_SIZE

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30,
        max_retries: int = 2,
        session: Any = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        key = str(api_key or "").strip()
        if not key:
            raise ValueError("17TRACK API key must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")

        self._api_key = key
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session if session is not None else requests_lib.Session()
        self._sleep = sleep if sleep is not None else time.sleep

    @property
    def provider_name(self) -> str:
        return "17track"

    def _retry_delay(self, attempt: int, response: Any = None) -> float:
        headers = getattr(response, "headers", None) or {}
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), 30.0)
            except (TypeError, ValueError):
                pass
        return min(float(2**attempt), 30.0)

    def _post(
        self,
        endpoint: str,
        payload: list[dict[str, Any]],
        *,
        tracking_numbers: Sequence[str],
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        headers = {"content-type": "application/json", "17token": self._api_key}

        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
            except (requests_lib.Timeout, requests_lib.ConnectionError) as exc:
                if attempt < self.max_retries:
                    self._sleep(self._retry_delay(attempt))
                    continue
                message = _sanitize_error(
                    f"17TRACK network failure: {exc.__class__.__name__}",
                    api_key=self._api_key,
                    tracking_numbers=tracking_numbers,
                )
                raise LogisticsProviderError(message, retryable=True) from exc
            except requests_lib.RequestException as exc:
                message = _sanitize_error(
                    f"17TRACK request failure: {exc.__class__.__name__}",
                    api_key=self._api_key,
                    tracking_numbers=tracking_numbers,
                )
                raise LogisticsProviderError(message, retryable=False) from exc

            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code == 429:
                if attempt < self.max_retries:
                    self._sleep(self._retry_delay(attempt, response))
                    continue
                raise LogisticsRateLimitError(
                    "17TRACK rate limit remained active after retries",
                    status_code=429,
                    retryable=True,
                )
            if 500 <= status_code <= 599:
                if attempt < self.max_retries:
                    self._sleep(self._retry_delay(attempt, response))
                    continue
                raise LogisticsProviderError(
                    f"17TRACK HTTP {status_code} after retries",
                    status_code=status_code,
                    retryable=True,
                )
            if status_code < 200 or status_code >= 300:
                raise LogisticsProviderError(
                    f"17TRACK HTTP {status_code}",
                    status_code=status_code,
                    retryable=False,
                )

            try:
                body = response.json()
            except (TypeError, ValueError) as exc:
                # Never include response.text here; it may contain tracking,
                # recipient or account information.
                raise LogisticsProviderError(
                    "17TRACK returned an invalid JSON response",
                    status_code=status_code,
                    retryable=False,
                ) from exc
            if not isinstance(body, dict):
                raise LogisticsProviderError(
                    "17TRACK returned an unexpected response shape",
                    status_code=status_code,
                    retryable=False,
                )

            code = body.get("code")
            if str(code) != "0":
                raw_message = body.get("message") or body.get("msg") or "API request failed"
                message = _sanitize_error(
                    f"17TRACK API {code}: {raw_message}",
                    api_key=self._api_key,
                    tracking_numbers=tracking_numbers,
                )
                raise LogisticsProviderError(
                    message,
                    status_code=status_code,
                    retryable=False,
                )
            return body

        raise AssertionError("retry loop exited unexpectedly")

    def _call_batches(
        self,
        endpoint: str,
        tracking_requests: Sequence[TrackingRequest],
        parser: Callable[[dict[str, Any], Sequence[TrackingRequest]], list[TrackingResult]],
    ) -> list[TrackingResult]:
        items = list(tracking_requests)
        if not items:
            return []
        if not all(isinstance(item, TrackingRequest) for item in items):
            raise TypeError("all tracking requests must be TrackingRequest instances")

        results: list[TrackingResult] = []
        for batch in _chunks(items, self.batch_size):
            try:
                body = self._post(
                    endpoint,
                    [_request_payload(item) for item in batch],
                    tracking_numbers=[item.number for item in batch],
                )
                results.extend(parser(body, batch))
            except LogisticsProviderError as exc:
                # Keep the established typed exception contract for one
                # batch, but never discard successful independent batches.
                if len(items) <= self.batch_size:
                    raise
                message = _sanitize_error(
                    str(exc), api_key=self._api_key,
                    tracking_numbers=[item.number for item in batch],
                )
                results.extend(
                    TrackingResult(
                        number=item.number,
                        carrier=_carrier_text(item.carrier),
                        sync_status=TrackingSyncStatus.API_ERROR,
                        error_message=message,
                    )
                    for item in batch
                )
        return results

    def _response_items(self, body: Mapping[str, Any]) -> tuple[list[Any], list[Any]]:
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise LogisticsProviderError(
                "17TRACK response is missing its data object",
                retryable=False,
            )
        accepted = data.get("accepted") or []
        rejected = data.get("rejected") or []
        if not isinstance(accepted, list) or not isinstance(rejected, list):
            raise LogisticsProviderError(
                "17TRACK response has invalid accepted/rejected collections",
                retryable=False,
            )
        return accepted, rejected

    def _parse_rejected(
        self,
        rejected: Sequence[Any],
        requested_numbers: Sequence[str],
    ) -> list[TrackingResult]:
        results: list[TrackingResult] = []
        for raw in rejected:
            if not isinstance(raw, Mapping):
                continue
            error = raw.get("error")
            error = error if isinstance(error, Mapping) else {}
            code = error.get("code", "")
            raw_message = error.get("message") or "17TRACK rejected this tracking number"
            results.append(
                TrackingResult(
                    number=str(raw.get("number") or ""),
                    carrier=_carrier_text(raw.get("carrier")),
                    latest_status=TrackingStatus.UNKNOWN,
                    sync_status=_rejection_status(code, raw_message),
                    error_code=str(code or ""),
                    error_message=_sanitize_error(
                        raw_message,
                        api_key=self._api_key,
                        tracking_numbers=requested_numbers,
                    ),
                )
            )
        return results

    def _parse_register(
        self,
        body: dict[str, Any],
        batch: Sequence[TrackingRequest],
    ) -> list[TrackingResult]:
        accepted, rejected = self._response_items(body)
        results: list[TrackingResult] = []
        for raw in accepted:
            if not isinstance(raw, Mapping):
                continue
            results.append(
                TrackingResult(
                    number=str(raw.get("number") or ""),
                    carrier=_carrier_text(raw.get("carrier")),
                    latest_status=TrackingStatus.PENDING_QUERY,
                    sync_status=TrackingSyncStatus.REGISTERED,
                )
            )
        results.extend(self._parse_rejected(rejected, [item.number for item in batch]))
        return results

    def _parse_track_info(
        self,
        body: dict[str, Any],
        batch: Sequence[TrackingRequest],
    ) -> list[TrackingResult]:
        accepted, rejected = self._response_items(body)
        results: list[TrackingResult] = []
        for raw in accepted:
            if not isinstance(raw, Mapping):
                continue
            track_info_raw = raw.get("track_info")
            track_info = track_info_raw if isinstance(track_info_raw, Mapping) else {}
            latest_status_raw = track_info.get("latest_status")
            latest_status = (
                latest_status_raw if isinstance(latest_status_raw, Mapping) else {}
            )
            raw_status = str(
                latest_status.get("status") or raw.get("package_status") or ""
            ).strip()
            raw_sub_status = str(latest_status.get("sub_status") or "").strip()
            sync_status, track_time = _source_sync_outcome(raw, track_info)
            error_message = (
                "17TRACK carrier-source synchronization failed"
                if sync_status is TrackingSyncStatus.SOURCE_FAILED
                else ""
            )
            results.append(
                TrackingResult(
                    number=str(raw.get("number") or ""),
                    carrier=_carrier_text(raw.get("carrier")),
                    latest_status=_normalized_status(raw_status, raw_sub_status),
                    latest_event=_latest_event(track_info),
                    track_time=track_time,
                    sync_status=sync_status,
                    raw_status=raw_status,
                    raw_sub_status=raw_sub_status,
                    error_message=error_message,
                )
            )
        results.extend(self._parse_rejected(rejected, [item.number for item in batch]))
        return results

    def register(
        self, tracking_requests: Sequence[TrackingRequest]
    ) -> list[TrackingResult]:
        """Register new tracking numbers, in batches of at most 40."""

        return self._call_batches("register", tracking_requests, self._parse_register)

    def get_track_info(
        self, tracking_requests: Sequence[TrackingRequest]
    ) -> list[TrackingResult]:
        """Read stored tracking details without initiating real-time tracking."""

        return self._call_batches(
            "gettrackinfo", tracking_requests, self._parse_track_info
        )

    def refresh(
        self,
        tracking_requests: Sequence[TrackingRequest],
        *,
        register_first: bool = False,
    ) -> list[TrackingResult]:
        """Optionally register, then read current stored tracking details.

        ``register_first`` defaults to ``False`` so the routine used for daily
        refreshes never consumes registration quota by accident.  A dry-run
        workflow should skip calling the provider altogether; if it elects to
        inspect already-registered provider state, it must keep this flag
        false.
        """

        items = list(tracking_requests)
        if register_first:
            registrations = self.register(items)
            failures = [
                result for result in registrations
                if result.sync_status is not TrackingSyncStatus.REGISTERED
            ]
            failed_keys = {(result.number, result.carrier) for result in failures}
            query_items = [
                item for item in items
                if not any(
                    number == item.number and (
                        not carrier or not item.carrier
                        or carrier == _carrier_text(item.carrier)
                    )
                    for number, carrier in failed_keys
                )
            ]
            try:
                return failures + self.get_track_info(query_items)
            except LogisticsProviderError as exc:
                if not failures:
                    raise
                return failures + [
                    TrackingResult(
                        number=item.number, carrier=_carrier_text(item.carrier),
                        sync_status=TrackingSyncStatus.API_ERROR,
                        error_message=_sanitize_error(
                            str(exc), api_key=self._api_key,
                            tracking_numbers=[request.number for request in query_items],
                        ),
                    )
                    for item in query_items
                ]
        return self.get_track_info(items)
