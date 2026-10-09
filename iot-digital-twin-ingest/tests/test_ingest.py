import copy
import io
import json
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import app
from domain import Config
from repository import DynamoRepository, StaleSequence
from validation import REQUIRED

NOW = datetime(2026, 10, 8, 19, 45, tzinfo=timezone.utc)
CONFIG = Config("core2-aws-001", "trusted-test-principal", "test-table")
EVENT = {
    "device_id": CONFIG.device_id,
    "principal": CONFIG.principal,
    "topic": f"devices/{CONFIG.device_id}/telemetry",
    "timestamp": "2026-10-08T19:45:00Z",
    "accel_x": 0.03,
    "accel_y": -0.02,
    "accel_z": 1.01,
    "gyro_x": 0.4,
    "gyro_y": 0.1,
    "gyro_z": -0.2,
    "operating_state": "NORMAL",
    "sequence": 42,
}


class MemoryRepository:
    def __init__(self):
        self.state = None
        self.calls = 0

    def put_latest(self, state):
        self.calls += 1
        if self.state and self.state["sequence"] >= state["sequence"]:
            raise StaleSequence
        self.state = copy.deepcopy(state)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()

    def run_event(self, event=None, now=NOW, config=CONFIG):
        return app.ingest(
            EVENT if event is None else event, config, self.repo, lambda: now
        )

    def reject(self, event, reason):
        self.assertEqual(
            self.run_event(event), {"status": "rejected", "reason": reason}
        )
        self.assertEqual(self.repo.calls, 0)

    def test_valid_and_normalized_shape_without_sdk(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.run_event()
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["condition"], "STATIONARY")
        expected = {
            key: value
            for key, value in EVENT.items()
            if key not in ("principal", "topic")
        }
        expected.update(
            condition="STATIONARY",
            received_at="2026-10-08T19:45:00Z",
            connectivity={"status": "ONLINE", "source": "accepted_telemetry"},
        )
        self.assertEqual(self.repo.state, expected)

    def test_missing_every_required_field(self):
        for field in REQUIRED:
            with self.subTest(field=field):
                event = dict(EVENT)
                del event[field]
                self.reject(event, "missing_field")

    def test_invalid_event(self):
        for event in ([], None, "secret"):
            result = app.ingest(event, CONFIG, self.repo, lambda: NOW)
            self.assertEqual(result["reason"], "invalid_event")

    def test_device_id(self):
        for value in ("", "../bad", "a/b", 12, "a" * 129):
            self.reject({**EVENT, "device_id": value}, "invalid_device_id")

    def test_identity_mismatch(self):
        for key, value in (
            ("device_id", "other"),
            ("principal", "forged"),
            ("topic", "devices/other/telemetry"),
        ):
            self.reject({**EVENT, key: value}, "identity_mismatch")

    def test_timestamps(self):
        for value in (
            "bad",
            "2026-02-30T19:45:00Z",
            "2026-10-08T19:45:00",
            "2026-10-08T19:45:00+01:00",
            1,
        ):
            self.reject({**EVENT, "timestamp": value}, "invalid_timestamp")
        for seconds in (-11, 11):
            self.assertEqual(
                self.run_event(now=NOW + timedelta(seconds=seconds))["reason"],
                "clock_skew",
            )
        for seconds in (-10, 10):
            self.repo = MemoryRepository()
            self.assertEqual(
                self.run_event(now=NOW + timedelta(seconds=seconds))["status"],
                "accepted",
            )
        self.repo = MemoryRepository()
        self.assertEqual(
            self.run_event(
                now=NOW + timedelta(seconds=11),
                config=Config(CONFIG.device_id, CONFIG.principal, "table", 12),
            )["status"],
            "accepted",
        )

    def test_imu_invalid_and_bounds(self):
        for key in ("accel_x", "gyro_z"):
            for value in (
                float("nan"),
                float("inf"),
                -float("inf"),
                "0.1",
                True,
                None,
                10**400,
            ):
                self.reject({**EVENT, key: value}, "invalid_imu")
        for key, value in (
            ("accel_z", 16.01),
            ("accel_x", -16.01),
            ("gyro_y", 2000.01),
        ):
            self.reject({**EVENT, key: value}, "invalid_imu")

    def test_device_offline_and_states(self):
        self.reject({**EVENT, "operating_state": "OFFLINE"}, "device_offline_forbidden")
        self.reject({**EVENT, "operating_state": "unknown"}, "invalid_operating_state")
        self.run_event(
            {**EVENT, "connectivity": {"status": "OFFLINE"}, "condition": "FORGED"}
        )
        self.assertEqual(self.repo.state["connectivity"]["status"], "ONLINE")
        self.assertEqual(self.repo.state["condition"], "STATIONARY")

    def test_sequence_progression(self):
        for sequence, status in (
            (0, "accepted"),
            (42, "accepted"),
            (42, "rejected"),
            (41, "rejected"),
            (43, "accepted"),
        ):
            result = self.run_event({**EVENT, "sequence": sequence})
            self.assertEqual(result["status"], status)
            if status == "rejected":
                self.assertEqual(result["reason"], "stale_sequence")
        self.assertEqual(self.repo.state["sequence"], 43)

    def test_invalid_sequence(self):
        for value in (-1, True, 1.0, "42", 2**63):
            self.reject({**EVENT, "sequence": value}, "invalid_sequence")

    def test_motion(self):
        for change in ({"accel_z": 1.2}, {"gyro_x": 6}, {"accel_z": 0}):
            self.repo = MemoryRepository()
            self.assertEqual(self.run_event({**EVENT, **change})["condition"], "MOVING")
        self.repo = MemoryRepository()
        self.assertEqual(
            self.run_event({**EVENT, "gyro_x": 5, "gyro_y": 0, "gyro_z": 0})[
                "condition"
            ],
            "STATIONARY",
        )

    def test_config(self):
        env = {
            "EXPECTED_DEVICE_ID": CONFIG.device_id,
            "EXPECTED_PRINCIPAL": CONFIG.principal,
            "LATEST_STATE_TABLE": "table",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config.from_env().max_clock_skew, 10)
        for value in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Config(CONFIG.device_id, CONFIG.principal, "table", value)
        self.assertNotIn(CONFIG.principal, repr(CONFIG))

    def test_safe_handler_logging_and_runtime_retry(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        app.LOGGER.addHandler(handler)
        try:
            with patch.object(app, "_RUNTIME", (CONFIG, self.repo)):
                result = app.lambda_handler(
                    {**EVENT, "principal": "sensitive-token"}, None
                )
                self.assertEqual(result["reason"], "identity_mismatch")
            self.assertNotIn("sensitive-token", stream.getvalue())
            with (
                patch.object(app, "_RUNTIME", (CONFIG, self.repo)),
                patch.object(app, "ingest", side_effect=Exception("sensitive-token")),
            ):
                with self.assertRaisesRegex(RuntimeError, "ingestion_runtime_failure"):
                    app.lambda_handler(EVENT, None)
            self.assertNotIn("sensitive-token", stream.getvalue())
        finally:
            app.LOGGER.removeHandler(handler)


class FakeAwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeTable:
    def __init__(self, code=None):
        self.code = code
        self.request = None

    def put_item(self, **kwargs):
        self.request = kwargs
        if self.code:
            raise FakeAwsError(self.code)


class RepositoryTests(unittest.TestCase):
    def test_conditional_write_and_decimal_serialization(self):
        table = FakeTable()
        result = app.ingest(EVENT, CONFIG, DynamoRepository(table), lambda: NOW)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(
            table.request["ConditionExpression"],
            "attribute_not_exists(#device) OR #sequence < :sequence",
        )
        self.assertEqual(
            table.request["ExpressionAttributeNames"],
            {"#device": "device_id", "#sequence": "sequence"},
        )
        self.assertEqual(table.request["ExpressionAttributeValues"], {":sequence": 42})
        self.assertEqual(table.request["Item"]["accel_x"], Decimal("0.03"))
        self.assertNotIn("principal", table.request["Item"])

    def test_conditional_failure_is_rejection(self):
        result = app.ingest(
            EVENT,
            CONFIG,
            DynamoRepository(FakeTable("ConditionalCheckFailedException")),
            lambda: NOW,
        )
        self.assertEqual(result, {"status": "rejected", "reason": "stale_sequence"})

    def test_storage_failure_propagates_for_retry(self):
        with self.assertRaises(FakeAwsError):
            app.ingest(
                EVENT,
                CONFIG,
                DynamoRepository(FakeTable("ProvisionedThroughputExceededException")),
                lambda: NOW,
            )

    def test_result_is_json_serializable(self):
        json.dumps(
            app.ingest(EVENT, CONFIG, MemoryRepository(), lambda: NOW), allow_nan=False
        )


if __name__ == "__main__":
    unittest.main()
