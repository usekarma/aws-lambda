"""Telemetry validation; no AWS dependencies or identity logging."""

import math
import re
from datetime import datetime, timezone

DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z"
)
IMU_FIELDS = ("accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z")
REQUIRED = (
    "device_id",
    "timestamp",
    "operating_state",
    "sequence",
    "principal",
    "topic",
    *IMU_FIELDS,
)


class Rejection(ValueError):
    """Carries only a fixed, safe reason code."""


def validate(event, config, now):
    if not isinstance(event, dict):
        raise Rejection("invalid_event")
    if any(field not in event for field in REQUIRED):
        raise Rejection("missing_field")
    device = event["device_id"]
    if not isinstance(device, str) or not DEVICE_ID.fullmatch(device):
        raise Rejection("invalid_device_id")
    if (
        device != config.device_id
        or event["principal"] != config.principal
        or event["topic"] != f"devices/{config.device_id}/telemetry"
    ):
        raise Rejection("identity_mismatch")
    timestamp = event["timestamp"]
    if not isinstance(timestamp, str) or not UTC_TIMESTAMP.fullmatch(timestamp):
        raise Rejection("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise Rejection("invalid_timestamp") from None
    if abs((now - parsed).total_seconds()) > config.max_clock_skew:
        raise Rejection("clock_skew")
    values = {}
    for field in IMU_FIELDS:
        value = event[field]
        if type(value) not in (int, float):
            raise Rejection("invalid_imu")
        bound = 16 if field.startswith("accel") else 2000
        # Compare before float conversion to safely reject arbitrarily large integers.
        if not -bound <= value <= bound:
            raise Rejection("invalid_imu")
        if not math.isfinite(value):
            raise Rejection("invalid_imu")
        values[field] = float(value)
    state = event["operating_state"]
    if state == "OFFLINE":
        raise Rejection("device_offline_forbidden")
    if state != "NORMAL":
        raise Rejection("invalid_operating_state")
    sequence = event["sequence"]
    if type(sequence) is not int or not 0 <= sequence <= 2**63 - 1:
        raise Rejection("invalid_sequence")
    return {
        "device_id": device,
        "timestamp": parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sequence": sequence,
        "operating_state": state,
        **values,
    }
