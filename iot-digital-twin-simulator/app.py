"""AWS Lambda entrypoints; construction is lazy and makes no calls during import."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from runtime import Conflict, DynamoStore, session_id


def clients() -> tuple[DynamoStore, Any]:
    import boto3
    from botocore.config import Config

    model_commit = os.environ["MODEL_COMMIT"]
    provenance = json.loads(
        Path(__file__).with_name("model-provenance.json").read_text()
    )
    if provenance["model_commit"] != model_commit:
        raise ValueError("artifact/model revision mismatch")
    config = Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1})
    return DynamoStore(
        boto3.client("dynamodb", config=config), os.environ["STATE_TABLE"], model_commit
    ), boto3.client(
        "iot-data",
        config=Config(
            connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}
        ),
    )


def reset_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if set(event) != {
        "session_id",
        "command_id",
        "expected_epoch",
        "principal",
        "topic",
    }:
        raise ValueError("invalid trusted rule projection")
    if (
        event["session_id"] != os.environ["ACTIVE_SESSION"]
        or event["topic"] != os.environ["REQUEST_TOPIC"]
    ):
        raise ValueError("inactive session/topic")
    store, publisher = clients()
    now = int(time.time())
    command = {
        key: event[key] for key in ("session_id", "command_id", "expected_epoch")
    }
    store.reset(command, now, event["principal"], os.environ["EXPECTED_PRINCIPAL"])
    return store.publish_ack(
        event["session_id"],
        event["command_id"],
        now,
        publisher,
        os.environ["ACK_TOPIC"],
    )


def start_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """IAM-only supervised initializer; deployment must not expose it publicly."""
    if event != {"session_id": os.environ["ACTIVE_SESSION"]}:
        raise ValueError("invalid supervised start")
    store, _ = clients()
    state = store.start(session_id(event["session_id"]), int(time.time()))
    return {"session_id": state["session"], "deadline": state["deadline"]}


def runner_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Bounded runner; no automatic new session, lease takeover or indefinite retry."""
    session = session_id(os.environ["ACTIVE_SESSION"])
    if event == {"session_id": session, "operation": "start"}:
        store, _ = clients()
        store.start(session, int(time.time()))
        event = {"session_id": session}
    if event != {"session_id": session}:
        raise ValueError("invalid supervised invocation")
    store, _ = clients()
    owner = context.aws_request_id
    start = time.monotonic()
    state = store.acquire(session, owner, int(time.time()))
    fence = state["fence"]
    while (
        time.monotonic() - start < 180 and context.get_remaining_time_in_millis() > 5000
    ):
        now = int(time.time())
        state = store.read(session)
        if (
            state is None
            or now >= state["deadline"]
            or state["fence"] != fence
            or state["owner"] != owner
        ):
            break
        if now >= state["lease_until"]:
            break  # Expired owner must not silently reacquire.
        try:
            if state["lease_until"] - now < 5:
                state = store.renew(state, now)
            if state["tick"] < 6 and now >= state["next_tick"]:
                store.tick(state, now)
        except Conflict:
            pass  # Reread on next bounded iteration; never replay a blind write.
        time.sleep(1)
    return {"status": "stopped", "session_id": session}
