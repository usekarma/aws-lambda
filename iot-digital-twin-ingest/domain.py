"""Configuration and deterministic latest-state normalization."""

import math
import os
from dataclasses import dataclass, field

from validation import DEVICE_ID


@dataclass(frozen=True)
class Config:
    device_id: str
    principal: str = field(repr=False)
    table_name: str
    max_clock_skew: float = 10.0
    accel_threshold: float = 0.15
    gyro_threshold: float = 5.0

    def __post_init__(self):
        if not isinstance(self.device_id, str) or not DEVICE_ID.fullmatch(
            self.device_id
        ):
            raise ValueError("invalid_configuration")
        if not self.principal or not self.table_name:
            raise ValueError("invalid_configuration")
        for value in (self.max_clock_skew, self.accel_threshold, self.gyro_threshold):
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid_configuration")

    @classmethod
    def from_env(cls):
        return cls(
            device_id=os.environ["EXPECTED_DEVICE_ID"],
            principal=os.environ["EXPECTED_PRINCIPAL"],
            table_name=os.environ["LATEST_STATE_TABLE"],
            max_clock_skew=float(os.environ.get("MAX_CLOCK_SKEW_SECONDS", "10")),
            accel_threshold=float(os.environ.get("ACCEL_MOTION_THRESHOLD_G", "0.15")),
            gyro_threshold=float(os.environ.get("GYRO_MOTION_THRESHOLD_DPS", "5")),
        )


def normalize(telemetry, config, now):
    acceleration = math.sqrt(sum(telemetry[f"accel_{axis}"] ** 2 for axis in "xyz"))
    rotation = math.sqrt(sum(telemetry[f"gyro_{axis}"] ** 2 for axis in "xyz"))
    moving = (
        abs(acceleration - 1.0) > config.accel_threshold
        or rotation > config.gyro_threshold
    )
    return {
        **telemetry,
        "condition": "MOVING" if moving else "STATIONARY",
        "received_at": now.isoformat().replace("+00:00", "Z"),
        "connectivity": {"status": "ONLINE", "source": "accepted_telemetry"},
    }
