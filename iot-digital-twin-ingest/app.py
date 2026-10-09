"""Lambda entry point; SDK initialization is lazy for credential-free tests."""

import json
import logging
from datetime import datetime, timezone

from domain import Config, normalize
from repository import DynamoRepository, StaleSequence
from validation import Rejection, validate

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
_RUNTIME = None


def ingest(event, config, repository, clock=lambda: datetime.now(timezone.utc)):
    now = clock()
    try:
        telemetry = validate(event, config, now)
        state = normalize(telemetry, config, now)
        repository.put_latest(state)
    except Rejection as error:
        return {"status": "rejected", "reason": str(error)}
    except StaleSequence:
        return {"status": "rejected", "reason": "stale_sequence"}
    return {
        "status": "accepted",
        "device_id": state["device_id"],
        "sequence": state["sequence"],
        "condition": state["condition"],
    }


def lambda_handler(event, context):
    global _RUNTIME
    try:
        if _RUNTIME is None:
            config = Config.from_env()
            import boto3

            _RUNTIME = (
                config,
                DynamoRepository(boto3.resource("dynamodb").Table(config.table_name)),
            )
        result = ingest(event, *_RUNTIME)
    except Exception:
        # Never emit exception text: SDK/config errors may contain identity material.
        LOGGER.error('{"status":"error","reason":"runtime_failure"}')
        raise RuntimeError("ingestion_runtime_failure") from None
    LOGGER.info(json.dumps(result, sort_keys=True))
    return result
