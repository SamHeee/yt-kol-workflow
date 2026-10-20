"""Stable parcel identities shared by logistics refresh and reminders."""

import hashlib
import re


def normalize_carrier(value) -> str:
    return str(value or "").strip().casefold()


def normalize_tracking_number(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def shipment_key(provider_name: str, carrier_code: str, tracking_number: str) -> str:
    raw = "\0".join((
        str(provider_name or "").strip().lower(),
        normalize_carrier(carrier_code),
        normalize_tracking_number(tracking_number),
    ))
    return "v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def shipment_is_current(carrier_code: str, tracking_number: str, synced_key: str) -> bool:
    """Only a confirmed identity may lend its cached data to a reminder."""
    return bool(normalize_tracking_number(tracking_number)) and str(synced_key or "") == shipment_key(
        "17track", carrier_code, tracking_number
    )
