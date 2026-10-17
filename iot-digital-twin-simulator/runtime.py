"""Executable AWS adapters for the bounded synthetic demo; clients are injected."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from typing import Any

from business_app.grid_simulator import GridSimulator, SimulatorConfig

MODEL_VERSION = "karma-ring-v1"
MAX_BYTES = 65536


class Conflict(Exception):
    """Conditional write lost; caller must reread rather than blind retry."""


def integer(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {name}")
    return value


def session_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value):
        raise ValueError("invalid session_id")
    return value


def projection(epoch: int, tick: int) -> dict[str, Any]:
    """Recover at most six steps using the existing model, including all trace epochs."""
    simulator = GridSimulator(SimulatorConfig())
    trace = None
    for _ in range(tick):
        trace = simulator.step_traced()
    events = tuple(replace(e, epoch=epoch) for e in simulator.snapshot())
    if trace is not None:
        trace = replace(
            trace,
            before=tuple(replace(e, epoch=epoch) for e in trace.before),
            disturbance=replace(trace.disturbance, epoch=epoch),
            effects=tuple(
                replace(e, predecessor=replace(e.predecessor, epoch=epoch))
                for e in trace.effects
            ),
            transitions=tuple(
                replace(
                    t,
                    before=replace(t.before, epoch=epoch),
                    after=replace(t.after, epoch=epoch),
                )
                for t in trace.transitions
            ),
            observations=events,
        )
    return {
        "observations": [asdict(e) for e in events],
        "trace": asdict(trace) if trace else None,
    }


def expression(condition: str, update: str | None = None) -> dict[str, Any]:
    """Alias application attributes to avoid DynamoDB reserved-word ambiguity."""
    names = {}
    for field in (
        "revision",
        "epoch",
        "fence",
        "lease_until",
        "deadline",
        "next_tick",
        "attempts",
        "ack_until",
        "ack_sent",
    ):
        if re.search(r"\b" + field + r"\b", condition + (update or "")):
            names["#" + field] = field
            condition = re.sub(r"(?<![:#])\b" + field + r"\b", "#" + field, condition)
            if update is not None:
                update = re.sub(r"(?<![:#])\b" + field + r"\b", "#" + field, update)
    result: dict[str, Any] = {"ConditionExpression": condition}
    if names:
        result["ExpressionAttributeNames"] = names
    if update is not None:
        result["UpdateExpression"] = update
    return result


class DynamoStore:
    def __init__(self, client: Any, table: str, model_commit: str) -> None:
        if not re.fullmatch("[0-9a-f]{40}", model_commit):
            raise ValueError("invalid model_commit")
        self.model_commit = model_commit
        self.client, self.table = client, table

    def key(self, session: str, suffix: str = "STATE") -> dict[str, Any]:
        return {"PK": {"S": "SESSION#" + session_id(session)}, "SK": {"S": suffix}}

    def read(self, session: str, suffix: str = "STATE") -> dict[str, Any] | None:
        response = self.client.get_item(
            TableName=self.table, Key=self.key(session, suffix), ConsistentRead=True
        )
        item = response.get("Item")
        if not item:
            return None
        value = json.loads(item["document"]["S"])
        if suffix == "STATE":
            self.validate(value)
        return value

    def validate(self, state: dict[str, Any]) -> None:
        if (
            state.get("model") != MODEL_VERSION
            or state.get("schema") != 1
            or state.get("model_commit") != self.model_commit
        ):
            raise ValueError("incompatible persisted model/schema")
        session_id(state["session"])
        for field, low, high in [
            ("epoch", 1, 1000000),
            ("tick", 0, 6),
            ("revision", 0, 1000000),
            ("fence", 0, 1000000),
            ("reset_count", 0, 3),
            ("deadline", 1, 2**53),
            ("lease_until", 0, 2**53),
            ("next_tick", 0, 2**53),
        ]:
            integer(state[field], field, low, high)

    def item(self, value: dict[str, Any], suffix: str = "STATE") -> dict[str, Any]:
        document = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(document.encode()) > (MAX_BYTES if suffix == "STATE" else 2048):
            raise ValueError("item size limit")
        item = self.key(value["session"], suffix) | {"document": {"S": document}}
        fields = (
            ("epoch", "revision", "fence", "lease_until", "deadline", "next_tick")
            if suffix == "STATE"
            else ()
        )
        return item | {key: {"N": str(value[key])} for key in fields}

    def put(
        self, state: dict[str, Any], condition: str, values: dict[str, Any]
    ) -> None:
        try:
            self.client.put_item(
                TableName=self.table,
                Item=self.item(state),
                **expression(condition),
                ExpressionAttributeValues=values,
            )
        except Exception as error:
            if (
                getattr(error, "response", {}).get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                raise Conflict from None
            raise

    def start(self, session: str, now: int) -> dict[str, Any]:
        integer(now, "now", 1, 2**53 - 180)
        state = {
            "schema": 1,
            "model": MODEL_VERSION,
            "model_commit": self.model_commit,
            "session": session_id(session),
            "epoch": 1,
            "tick": 0,
            "revision": 0,
            "fence": 0,
            "owner": "",
            "lease_until": 0,
            "deadline": now + 180,
            "next_tick": now,
            "reset_count": 0,
            "history": [projection(1, 0)["observations"]],
            **projection(1, 0),
        }
        self.client.put_item(
            TableName=self.table,
            Item=self.item(state),
            ConditionExpression="attribute_not_exists(PK)",
        )
        return state

    def acquire(self, session: str, owner: str, now: int) -> dict[str, Any]:
        state = self.read(session)
        if state is None or now >= state["deadline"] or now < state["lease_until"]:
            raise Conflict("lease unavailable")
        updated = state | {
            "owner": owner,
            "lease_until": min(now + 10, state["deadline"]),
            "fence": state["fence"] + 1,
            "revision": state["revision"] + 1,
        }
        self.put(
            updated,
            "revision = :revision AND lease_until <= :now AND deadline > :now",
            {":revision": {"N": str(state["revision"])}, ":now": {"N": str(now)}},
        )
        return updated

    def renew(self, state: dict[str, Any], now: int) -> dict[str, Any]:
        if now >= state["lease_until"] or now >= state["deadline"]:
            raise Conflict("expired lease")
        updated = state | {
            "lease_until": min(now + 10, state["deadline"]),
            "revision": state["revision"] + 1,
        }
        self.put(
            updated,
            "revision = :revision AND fence = :fence AND lease_until > :now AND deadline > :now",
            {
                ":revision": {"N": str(state["revision"])},
                ":fence": {"N": str(state["fence"])},
                ":now": {"N": str(now)},
            },
        )
        return updated

    def tick(self, state: dict[str, Any], now: int) -> dict[str, Any]:
        self.validate(state)
        if (
            now >= state["deadline"]
            or now >= state["lease_until"]
            or now < state["next_tick"]
            or state["tick"] >= 6
        ):
            raise Conflict("tick not eligible")
        tick = state["tick"] + 1
        result = projection(state["epoch"], tick)
        updated = (
            state
            | result
            | {
                "tick": tick,
                "revision": state["revision"] + 1,
                "next_tick": now + 3,
                "history": (state["history"] + [result["observations"]])[-12:],
            }
        )
        self.put(
            updated,
            "revision = :revision AND epoch = :epoch AND fence = :fence AND lease_until > :now AND deadline > :now AND next_tick <= :now",
            {
                ":revision": {"N": str(state["revision"])},
                ":epoch": {"N": str(state["epoch"])},
                ":fence": {"N": str(state["fence"])},
                ":now": {"N": str(now)},
            },
        )
        return updated

    def reset(
        self, command: dict[str, Any], now: int, principal: str, expected_principal: str
    ) -> dict[str, Any]:
        if set(command) != {"session_id", "command_id", "expected_epoch"}:
            raise ValueError("invalid command fields")
        session = session_id(command["session_id"])
        command_id = command["command_id"]
        if not isinstance(command_id, str) or not re.fullmatch(
            "[A-Za-z0-9_-]{1,64}", command_id
        ):
            raise ValueError("invalid command_id")
        integer(command["expected_epoch"], "expected_epoch", 1, 999999)
        state = self.read(session)
        if (
            not expected_principal
            or principal != expected_principal
            or state is None
            or now >= state["deadline"]
        ):
            raise ValueError("inactive or unauthorized session")
        receipt = self.read(session, "CMD#" + command_id)
        if receipt:
            if receipt["command"] != command:
                raise ValueError("conflicting command identity")
            return receipt
        if state["epoch"] != command["expected_epoch"] or state["reset_count"] >= 3:
            raise ValueError("stale epoch or reset limit")
        epoch = state["epoch"] + 1
        baseline = projection(epoch, 0)
        updated = (
            state
            | baseline
            | {
                "epoch": epoch,
                "tick": 0,
                "revision": state["revision"] + 1,
                "next_tick": now + 3,
                "reset_count": state["reset_count"] + 1,
                "history": [baseline["observations"]],
            }
        )
        receipt = {
            "session": session,
            "command": command,
            "epoch": epoch,
            "ack": {
                "version": 1,
                **command,
                "committed_epoch": epoch,
                "status": "reset_committed",
            },
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": self.item(updated),
                            **expression(
                                "revision = :revision AND epoch = :epoch AND deadline > :now"
                            ),
                            "ExpressionAttributeValues": {
                                ":revision": {"N": str(state["revision"])},
                                ":epoch": {"N": str(state["epoch"])},
                                ":now": {"N": str(now)},
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": self.item(receipt, "CMD#" + command_id),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") in (
                "TransactionCanceledException",
                "TransactionConflictException",
            ):
                raise Conflict from None
            raise
        return receipt

    def publish_ack(
        self, session: str, command_id: str, now: int, publisher: Any, topic: str
    ) -> dict[str, Any]:
        state = self.read(session)
        receipt = self.read(session, "CMD#" + command_id)
        if not state or now >= state["deadline"] or not receipt:
            raise ValueError("no active committed receipt")
        # Conditional counter is separate from document; persist attempts BEFORE publication.
        self.client.update_item(
            TableName=self.table,
            Key=self.key(session, "CMD#" + command_id),
            **expression(
                "attribute_exists(PK) AND (attribute_not_exists(attempts) OR attempts < :limit) AND (attribute_not_exists(ack_until) OR ack_until <= :now)",
                "SET attempts = if_not_exists(attempts, :zero) + :one, ack_until = :claim",
            ),
            ExpressionAttributeValues={
                ":zero": {"N": "0"},
                ":one": {"N": "1"},
                ":limit": {"N": "3"},
                ":now": {"N": str(now)},
                ":claim": {"N": str(now + 5)},
            },
        )
        publisher.publish(
            topic=topic,
            qos=1,
            retain=False,
            payload=json.dumps(receipt["ack"]).encode(),
        )
        self.client.update_item(
            TableName=self.table,
            Key=self.key(session, "CMD#" + command_id),
            **expression(
                "ack_until = :claim", "SET ack_sent = :sent, ack_until = :zero"
            ),
            ExpressionAttributeValues={
                ":sent": {"BOOL": True},
                ":zero": {"N": "0"},
                ":claim": {"N": str(now + 5)},
            },
        )
        return receipt["ack"]
