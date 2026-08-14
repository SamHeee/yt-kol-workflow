#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Provider-neutral models for sample shipment tracking.

The workflow deliberately keeps provider wire values out of Feishu.  A
provider client converts its response into these small, stable models and the
workflow owns the final mapping to the Chinese Base field values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol, Sequence, Union


CarrierCode = Union[str, int]


class TrackingStatus(str, Enum):
    """Normalized package statuses used by every logistics provider."""

    PENDING_QUERY = "pending_query"
    NOT_FOUND = "not_found"
    INFO_RECEIVED = "info_received"
    IN_TRANSIT = "in_transit"
    AVAILABLE_FOR_PICKUP = "available_for_pickup"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERY_FAILURE = "delivery_failure"
    DELIVERED = "delivered"
    RETURNED = "returned"
    EXPIRED = "expired"
    EXCEPTION = "exception"
    UNKNOWN = "unknown"


# A descriptive alias is useful to workflow code without tying it to the
# terminology used by 17TRACK.
LogisticsStatus = TrackingStatus


class TrackingSyncStatus(str, Enum):
    """Outcome of the provider operation for one tracking number.

    ``SUCCESS`` means that 17TRACK reports a successful carrier-source sync,
    not merely that our HTTP request returned 200.  This distinction prevents
    a stale provider response from being mistaken for fresh carrier data.
    """

    SUCCESS = "success"
    REGISTERED = "registered"
    NO_TRACK_INFO = "no_track_info"
    SOURCE_PENDING = "source_pending"
    SOURCE_FAILED = "source_failed"
    NOT_REGISTERED = "not_registered"
    INVALID_NUMBER = "invalid_number"
    UNSUPPORTED_CARRIER = "unsupported_carrier"
    ADDITIONAL_PARAMETER_REQUIRED = "additional_parameter_required"
    REJECTED = "rejected"
    API_ERROR = "api_error"


@dataclass(frozen=True)
class TrackingRequest:
    """The minimal data sent to a logistics provider.

    Recipient names, addresses, e-mail addresses and phone numbers are
    intentionally absent.  Some carriers require extra parameters, but the
    MVP reports that condition for manual handling instead of uploading PII.
    """

    number: str
    carrier: Optional[CarrierCode] = None
    tag: str = ""

    def __post_init__(self) -> None:
        number = str(self.number or "").strip()
        if not number:
            raise ValueError("tracking number must not be empty")
        object.__setattr__(self, "number", number)
        object.__setattr__(self, "tag", str(self.tag or "").strip())


@dataclass(frozen=True)
class TrackingEvent:
    """Latest carrier event with an aware UTC timestamp when available."""

    description: str
    event_time: Optional[datetime]
    location: str = ""
    sub_status: str = ""

    def __post_init__(self) -> None:
        if self.event_time is not None:
            if self.event_time.tzinfo is None or self.event_time.utcoffset() is None:
                raise ValueError("event_time must be timezone-aware")


@dataclass(frozen=True)
class TrackingResult:
    """Normalized result for one provider/carrier tracking number."""

    number: str
    carrier: str = ""
    latest_status: TrackingStatus = TrackingStatus.UNKNOWN
    latest_event: Optional[TrackingEvent] = None
    track_time: Optional[datetime] = None
    sync_status: TrackingSyncStatus = TrackingSyncStatus.NO_TRACK_INFO
    raw_status: str = ""
    raw_sub_status: str = ""
    error_code: str = ""
    error_message: str = ""

    def __post_init__(self) -> None:
        if self.track_time is not None:
            if self.track_time.tzinfo is None or self.track_time.utcoffset() is None:
                raise ValueError("track_time must be timezone-aware")

    @property
    def tracking_number(self) -> str:
        """Compatibility/readability alias for ``number``."""

        return self.number

    @property
    def source_sync_succeeded(self) -> bool:
        """Whether the latest carrier-source synchronization succeeded."""

        return self.sync_status is TrackingSyncStatus.SUCCESS

    @property
    def has_error(self) -> bool:
        return bool(self.error_code or self.error_message) or self.sync_status in {
            TrackingSyncStatus.SOURCE_FAILED,
            TrackingSyncStatus.INVALID_NUMBER,
            TrackingSyncStatus.UNSUPPORTED_CARRIER,
            TrackingSyncStatus.ADDITIONAL_PARAMETER_REQUIRED,
            TrackingSyncStatus.REJECTED,
            TrackingSyncStatus.API_ERROR,
        }


class LogisticsProviderError(RuntimeError):
    """A sanitized provider-level failure safe to persist in operational logs."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class LogisticsRateLimitError(LogisticsProviderError):
    """Raised when the provider keeps returning HTTP 429 after retries."""


class TrackingProvider(Protocol):
    """Small interface consumed by the daily sample logistics workflow."""

    def register(self, requests: Sequence[TrackingRequest]) -> list[TrackingResult]:
        ...

    def get_track_info(
        self, requests: Sequence[TrackingRequest]
    ) -> list[TrackingResult]:
        ...

    def refresh(
        self,
        requests: Sequence[TrackingRequest],
        *,
        register_first: bool = False,
    ) -> list[TrackingResult]:
        ...
