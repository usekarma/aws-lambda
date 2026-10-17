"""Operator-only DynamoDB integration against EXISTING sandbox resources; never MQTT."""

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "iot-digital-twin-simulator")
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--model-commit", required=True)
    parser.add_argument("--runtime-zip", type=Path, required=True)
    parser.add_argument("--expected-artifact-sha256", required=True)
    parser.add_argument("--execute-approved-writes", action="store_true")
    args = parser.parse_args()
    if not args.execute_approved_writes:
        parser.error(
            "requires separate human authorization of sandbox writes; no dry-run AWS calls"
        )
    from zipfile import ZipFile

    artifact = args.runtime_zip.read_bytes()
    if hashlib.sha256(artifact).hexdigest() != args.expected_artifact_sha256:
        raise RuntimeError("artifact digest mismatch")
    with ZipFile(args.runtime_zip) as archive:
        provenance = json.loads(archive.read("model-provenance.json"))
    if provenance["model_commit"] != args.model_commit:
        raise RuntimeError("model artifact mismatch")
    sys.path.insert(0, str(args.runtime_zip.resolve()))
    from runtime import Conflict, DynamoStore
    import boto3
    from botocore.config import Config

    sdk = boto3.Session(profile_name=args.profile, region_name=args.region)
    config = Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1})
    account = sdk.client("sts", config=config).get_caller_identity()["Account"]
    if account != args.expected_account:
        raise RuntimeError("account mismatch")
    client = sdk.client("dynamodb", config=config)
    table = client.describe_table(TableName=args.table)["Table"]
    tags = sdk.client("dynamodb", config=config).list_tags_of_resource(
        ResourceArn=table["TableArn"]
    )["Tags"]
    if {"Key": "Environment", "Value": "sandbox"} not in tags:
        raise RuntimeError("sandbox tag required")
    if table["KeySchema"] != [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]:
        raise RuntimeError("table schema mismatch")
    store = DynamoStore(client, args.table, args.model_commit)
    session = uuid.uuid4().hex
    now = int(time.time())
    store.start(session, now)
    old = store.acquire(session, "integration", now)
    store.tick(old, now)
    try:
        store.tick(old, now)
    except Conflict:
        pass
    else:
        raise RuntimeError("obsolete tick unexpectedly accepted")
    command = {
        "session_id": session,
        "command_id": "integration-reset",
        "expected_epoch": 1,
    }
    receipt = store.reset(command, now, "synthetic-principal", "synthetic-principal")
    if (
        store.reset(command, now, "synthetic-principal", "synthetic-principal")
        != receipt
    ):
        raise RuntimeError("duplicate changed receipt")
    try:
        store.tick(old, now)
    except Conflict:
        pass
    else:
        raise RuntimeError("pre-reset tick unexpectedly accepted")
    if store.read(session)["epoch"] != 2:
        raise RuntimeError("reset not committed")

    class Recorder:
        def publish(self, **kwargs):
            self.payload = json.loads(kwargs["payload"])

    recorder = Recorder()
    store.publish_ack(
        session, command["command_id"], now, recorder, "synthetic/no-network"
    )
    if recorder.payload != receipt["ack"]:
        raise RuntimeError("ACK mismatch")
    print(
        json.dumps(
            {
                "status": "passed",
                "session_id": session,
                "ack_sha256": hashlib.sha256(
                    json.dumps(recorder.payload, sort_keys=True).encode()
                ).hexdigest(),
                "mqtt": "not executed",
                "cleanup": "session items retained for separately approved cleanup",
            }
        )
    )


if __name__ == "__main__":
    main()
